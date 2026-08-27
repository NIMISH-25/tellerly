"""Structural proof that replay is model-free.

Two independent angles, because each can miss what the other catches:

- the import graph: loading the whole replay stack must not pull a model SDK,
  a bare HTTP client, or the discovery package into the process — checked in a
  clean subprocess so this pytest process's own imports cannot mask the result;
- runtime: a full happy-path replay must SUCCEED while the model SDK modules
  are replaced with poison objects that fail on any attribute access — proving
  replay does not merely avoid importing the SDK but never touches it at all.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import types

import pytest
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.config import REPO_ROOT

PORT = 8772  # 8771 belongs to test_replay_engine's module server; never 8000 (live target)
BASE = f"http://127.0.0.1:{PORT}"
ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"

BANNED_MODULES = (
    "anthropic",
    "openai",
    "google.genai",
    "google",
    "httpx",
    "requests",
    "aiohttp",
    "tellerly.discovery",
)

# `tellerly.kernel`'s __init__ is empty and `tellerly.surface`'s skips web.py,
# so the submodules replay actually uses are imported explicitly — otherwise
# the graph check would pass vacuously on near-empty packages.
_IMPORT_PROBE = textwrap.dedent(
    """
    import sys
    import tellerly.replay
    import tellerly.schema
    import tellerly.surface
    import tellerly.surface.web
    import tellerly.kernel
    import tellerly.kernel.evidence
    import tellerly.kernel.guardrails
    import tellerly.kernel.redaction
    import tellerly.kernel.store
    banned = %r
    offenders = sorted(set(banned) & set(sys.modules))
    if offenders:
        print("replay import graph reached banned modules:", offenders)
        raise SystemExit(1)
    print("import graph clean")
    """
) % (BANNED_MODULES,)


def test_replay_import_graph_carries_no_model_sdk() -> None:
    # A subprocess, not this process: pytest and sibling test modules may have
    # already imported requests/google/discovery here, which would either mask
    # a real leak or falsely trip the check.
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"replay import isolation broken\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


class _PoisonModule(types.ModuleType):
    """A module stand-in whose every attribute access fails the test.

    Replay must work with the SDK unusable — not just un-imported — so any
    touch at all is an immediate, attributable failure.
    """

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            # The import machinery probes dunders (__path__, __spec__) on
            # arbitrary sys.modules entries; only real SDK usage detonates.
            raise AttributeError(name)
        raise AssertionError(
            f"replay touched the model SDK: poisoned module attribute {name!r}"
        )


@pytest.fixture()
def server():
    data.reset()
    app = create_app(
        {"TESTING": True, "INTERSTITIAL_EVERY": 0, "SLOW_SECONDS": 0.0, "SESSION_TTL_S": 100_000}
    )
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


def test_full_replay_succeeds_with_the_model_sdk_poisoned(server, tmp_path, monkeypatch) -> None:
    for name in ("google", "google.genai"):
        monkeypatch.setitem(sys.modules, name, _PoisonModule(name))

    # Imports live inside the test so a solo run of this file performs them
    # after the poison lands; import-time cleanliness across a full suite is
    # what the subprocess test above proves.
    from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
    from tellerly.replay import ReplayEngine
    from tellerly.schema import Capability, Tier
    from tellerly.surface.web import PlaywrightWebSurface

    capability = Capability.from_json(ARTIFACT.read_text(encoding="utf-8"))
    surface = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    try:
        engine = ReplayEngine(
            surface=surface,
            # The recorded artifact pins hosts to the discovery-time port 8000;
            # the deployment∩capability intersection is the CLI's job, so here
            # the deployment gate itself admits the test port.
            gate=PolicyGate(
                DeploymentPolicy(
                    allowed_hosts=[f"127.0.0.1:{PORT}"],
                    allowed_actions=["navigate", "click", "fill", "select", "press"],
                )
            ),
            evidence_root=tmp_path / "evidence",
            approve_mutations=True,
        )
        result = engine.run(
            capability,
            {
                "operator_id": "op-replay",
                "access_key": "demo",
                "member_id": "101556",
                "from_share": "S00",
                "to_share": "S01",
                "amount": "15.00",
            },
            server,
        )
    finally:
        surface.close()

    # An engine that touched the SDK would have hit the poison's AssertionError
    # and (by its never-raise contract) surfaced it as a hard failure — so
    # SUCCESS here is the proof, not just a happy-path smoke check.
    assert result.status is Tier.SUCCESS, result
    assert result.economics.llm_calls == 0
    assert result.outputs is not None
    assert str(result.outputs["confirmation_no"]).startswith("TL-")
