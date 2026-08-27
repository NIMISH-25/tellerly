"""The replay engine run against the REAL recorded artifact and the live mock
console: the happy path, every declared business outcome, the policy gates
that refuse before anything touches a page, drift telemetry from ladder
fallback, and evidence hygiene. No planner, no model — economics must read 0.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.config import REPO_ROOT
from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
from tellerly.replay import ReplayEngine
from tellerly.schema import (
    ActStep,
    Capability,
    Code,
    StepStatus,
    SurfaceFeature,
    Tier,
)
from tellerly.surface.base import Surface

PORT = 8771
CHAOS_PORT = 8772  # transient in-test server; never 8000 — a live target may own it
BASE = f"http://127.0.0.1:{PORT}"
ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"

#: Chaos off: replay behaviour first, recovery behaviour in its own test.
CALM = {"TESTING": True, "INTERSTITIAL_EVERY": 0, "SLOW_SECONDS": 0.0, "SESSION_TTL_S": 100_000}

MUTATING_STEP = "s12-click-the-confirm-post-transfer"


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def server():
    app = create_app(dict(CALM))
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


@pytest.fixture(autouse=True)
def fresh_data():
    """Seed balances/ledger per test — several cases post real transfers."""
    data.reset()
    yield


@pytest.fixture(scope="module")
def surface():
    # One Chromium for the module: every engine.run() starts by opening the
    # entry URL, so tests stay independent while saving ~10s of launches.
    from tellerly.surface.web import PlaywrightWebSurface

    web = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    yield web
    web.close()


# -------------------------------------------------------------------- helpers


def load_capability() -> Capability:
    """The real recorded artifact, untouched — replay must honour it as-is."""
    return Capability.from_json(ARTIFACT.read_text(encoding="utf-8"))


def gate_for(port: int) -> PolicyGate:
    # The recorded artifact pins allowed_hosts to the discovery-time host
    # (127.0.0.1:8000). Building the deployment∩capability intersection is the
    # CLI's job; these tests build the deployment gate directly so the engine
    # runs against the test port without rewriting the read-only artifact.
    return PolicyGate(
        DeploymentPolicy(
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_actions=["navigate", "click", "fill", "select", "press"],
            require_confirmation=True,
        )
    )


def params(**overrides: str | None) -> dict[str, str]:
    """Standard inputs; override per case, or pass None to drop a key."""
    values: dict[str, str | None] = {
        "operator_id": "op-replay",
        "access_key": "demo",
        "member_id": "101556",
        "from_share": "S00",
        "to_share": "S01",
        "amount": "15.00",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def run_replay(
    surface: Surface,
    tmp_path: Path,
    *,
    capability: Capability | None = None,
    values: dict[str, str] | None = None,
    base: str = BASE,
    port: int = PORT,
    approve: bool = False,
):
    engine = ReplayEngine(
        surface=surface,
        gate=gate_for(port),
        evidence_root=tmp_path / "evidence",
        approve_mutations=approve,
    )
    return engine.run(capability or load_capability(), values or params(), base)


def balances(member_id: str) -> dict[str, float]:
    return {s["share_id"]: s["balance"] for s in data.get_member(member_id)["shares"]}


def artifact_dict() -> dict:
    """A mutable deep copy for the tests that deliberately break the artifact."""
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


class SpySurface(Surface):
    """Records calls so refuse-before-touch contracts are provable.

    Not a behavioural fake: any attempt to actually drive a page raises,
    because the tests using it assert the engine never gets that far.
    """

    #: Reads of current_path/current_url and screenshots are tolerated — a real
    #: browser answers those before open() too, and failure evidence may
    #: legitimately be attempted; opening or driving a page is what must not happen.
    _PAGE_TOUCHING = (
        "open",
        "navigate",
        "observe",
        "act",
        "probe",
        "resolve",
        "read_text",
        "locate_value_cell",
        "find_text",
    )

    def __init__(self, features: frozenset[SurfaceFeature] | None = None) -> None:
        self.calls: list[str] = []
        self._features = frozenset(SurfaceFeature) if features is None else frozenset(features)

    @property
    def page_touched(self) -> bool:
        return any(call.split(":", 1)[0] in self._PAGE_TOUCHING for call in self.calls)

    def _touch(self, name: str, detail: str = "") -> None:
        self.calls.append(f"{name}:{detail}" if detail else name)
        raise AssertionError(f"engine touched the page ({name}) before pre-flight checks passed")

    def features(self) -> frozenset[SurfaceFeature]:
        return self._features

    def open(self, url: str) -> None:
        self._touch("open", url)

    def navigate(self, path: str) -> None:
        self._touch("navigate", path)

    def observe(self):
        self._touch("observe")

    def act(self, uid: str, action, value: str | None = None) -> None:
        self._touch("act", uid)

    def probe(self, rung, frame, uid: str | None = None):
        self._touch("probe")

    def resolve(self, target):  # new seam method from the replay spec
        self._touch("resolve")

    def read_text(self, uid: str) -> str:
        self._touch("read_text", uid)
        return ""

    def locate_value_cell(self, anchor_text: str):
        self._touch("locate_value_cell", anchor_text)

    def find_text(self, text: str, timeout_s: float = 2.0) -> bool:
        self._touch("find_text", text)
        return False

    def current_path(self) -> str:
        self.calls.append("current_path")
        return "/"

    def current_url(self) -> str:
        self.calls.append("current_url")
        return "http://spy.invalid/"

    def screenshot(self, path: Path) -> None:
        self.calls.append(f"screenshot:{path}")

    def close(self) -> None:
        self.calls.append("close")


# ---------------------------------------------------------------------- tests


def test_happy_path_posts_the_transfer_and_reads_the_confirmation(server, surface, tmp_path):
    """A member the discovery run never saw (101556): the artifact is a
    parameterized capability, not a recording of one member's transfer."""
    result = run_replay(surface, tmp_path, approve=True)

    assert result.status is Tier.SUCCESS, result
    assert result.run_id.startswith("replay-")
    assert result.capability_id == "transfer_between_shares"
    assert result.outputs is not None
    assert str(result.outputs["confirmation_no"]).startswith("TL-")
    assert result.economics.llm_calls == 0

    # Drift telemetry: every act step that resolved a control says which rung.
    control_steps = {
        step.id
        for step in load_capability().steps
        if isinstance(step, ActStep) and step.target is not None
    }
    recorded = {outcome.step_id: outcome for outcome in result.steps}
    assert control_steps <= set(recorded), "telemetry missing for some act steps"
    for step_id in control_steps:
        assert recorded[step_id].resolved_via is not None, f"{step_id} lacks resolved_via"

    # The money actually moved for THIS member.
    after = balances("101556")
    assert after["S00"] == pytest.approx(1200.50 - 15.00)
    assert after["S01"] == pytest.approx(310.00 + 15.00)


def test_unknown_member_is_a_business_outcome(server, surface, tmp_path):
    result = run_replay(surface, tmp_path, values=params(member_id="99999"), approve=True)

    assert result.status is Tier.BUSINESS_OUTCOME, result
    assert result.outcome is not None
    assert result.outcome.code is Code.NO_SUCH_RECORD


def test_held_source_share_is_refused_not_crashed(server, surface, tmp_path):
    result = run_replay(
        surface,
        tmp_path,
        values=params(member_id="101555", from_share="S02", to_share="S00", amount="10.00"),
        approve=True,
    )

    assert result.status is Tier.BUSINESS_OUTCOME, result
    assert result.outcome is not None
    assert result.outcome.code is Code.OPERATION_REFUSED


def test_insufficient_funds_is_a_business_outcome(server, surface, tmp_path):
    result = run_replay(surface, tmp_path, values=params(amount="99999.00"), approve=True)

    assert result.status is Tier.BUSINESS_OUTCOME, result
    assert result.outcome is not None
    assert result.outcome.code is Code.INSUFFICIENT_FUNDS


def test_restricted_member_is_a_permission_hard_failure(server, surface, tmp_path):
    result = run_replay(surface, tmp_path, values=params(member_id="55555"), approve=True)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.PERMISSION_DENIED


def test_interstitial_is_auto_recovered_without_a_human(surface, tmp_path):
    """INTERSTITIAL_EVERY=1: the maintenance notice blocks the member record on
    every load; the declared outcome's recovery must clear it, invisibly to
    the caller except in telemetry/evidence."""
    app = create_app({**CALM, "INTERSTITIAL_EVERY": 1})
    httpd = make_server("127.0.0.1", CHAOS_PORT, app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        result = run_replay(
            surface,
            tmp_path,
            base=f"http://127.0.0.1:{CHAOS_PORT}",
            port=CHAOS_PORT,
            approve=True,
        )
    finally:
        httpd.shutdown()

    assert result.status is Tier.SUCCESS, result
    step_recovered = any(step.status is StepStatus.RECOVERED for step in result.steps)
    assert result.evidence_dir is not None
    events = (Path(result.evidence_dir) / "events.jsonl").read_text(encoding="utf-8")
    # Either telemetry marks the step recovered or evidence recorded the
    # recovery — the spec allows the engine to attach it at either level.
    assert step_recovered or "recover" in events.lower()


def test_invalid_params_fail_before_the_browser_opens(tmp_path):
    spy = SpySurface()
    bad = params(member_id="abc", amount=None)  # pattern violation + missing required

    result = run_replay(spy, tmp_path, values=bad)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.INPUT_INVALID
    detail = f"{result.failure.expected} {result.failure.observed}"
    assert "member_id" in detail and "amount" in detail, "every problem must be listed"
    assert not spy.page_touched, spy.calls


def test_unapproved_mutation_is_policy_blocked_before_posting(server, surface, tmp_path):
    before = balances("101556")

    result = run_replay(surface, tmp_path, approve=False)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.POLICY_BLOCKED
    assert result.failure.step_id == MUTATING_STEP
    detail = f"{result.failure.expected} {result.failure.observed}".lower()
    assert "approve" in detail  # the message must say how to approve
    assert balances("101556") == before, "the blocked transfer must not have posted"


def test_surface_without_frames_is_refused_before_any_interaction(tmp_path):
    spy = SpySurface(features=frozenset(SurfaceFeature) - {SurfaceFeature.FRAMES})

    result = run_replay(spy, tmp_path)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.SURFACE_INCOMPATIBLE
    detail = f"{result.failure.expected} {result.failure.observed}".lower()
    assert "frames" in detail  # the missing feature is named
    assert not spy.page_touched, spy.calls


def test_ladder_fallback_shows_up_as_drift_telemetry(server, surface, tmp_path):
    """A dead rung 0 must not fail the run — the walk falls through and the
    telemetry records WHICH rung matched, the raw signal for drift detection."""
    raw = artifact_dict()
    step = next(s for s in raw["steps"] if s["id"] == "s05-fill-the-member-no-or")
    # A role rung (durability 5) keeps the ladder legally ordered ahead of the
    # name rung (3); its nonsense name guarantees zero matches.
    step["target"]["ladder"].insert(
        0,
        {"strategy": "role", "role": "textbox", "name": "Field That Does Not Exist", "confidence": 0.5},
    )
    drifted = Capability.model_validate(raw)

    result = run_replay(surface, tmp_path, capability=drifted, approve=True)

    assert result.status is Tier.SUCCESS, result
    outcome = next(s for s in result.steps if s.step_id == "s05-fill-the-member-no-or")
    assert outcome.rung_index is not None and outcome.rung_index >= 1, outcome


def test_failed_success_condition_is_checkpoint_failed_with_screenshot(server, surface, tmp_path):
    raw = artifact_dict()
    raw["success"]["text_visible"] = "A BANNER THAT NEVER RENDERS 7f3a9c"
    broken = Capability.model_validate(raw)

    result = run_replay(surface, tmp_path, capability=broken, approve=True)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.CHECKPOINT_FAILED
    assert any(path.endswith(".png") for path in result.failure.evidence), result.failure


def test_evidence_never_contains_the_access_key(server, surface, tmp_path):
    """Value-based redaction: the secret must be absent from every evidence
    byte, not merely from fields named like secrets."""
    result = run_replay(surface, tmp_path, approve=True)

    assert result.status is Tier.SUCCESS, result
    assert result.evidence_dir is not None
    evidence = Path(result.evidence_dir)
    events = (evidence / "events.jsonl").read_text(encoding="utf-8")
    result_json = (evidence / "result.json").read_text(encoding="utf-8")
    assert "demo" not in events
    assert "demo" not in result_json
