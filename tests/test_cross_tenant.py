"""Cross-tenant replay against the live bluepeak instance: the fleet story.

Same vendor console, tenant-relabelled ("Teller ID:", "Log On", "Authorize
Posting") plus one extra verify screen. The BASE artifact must fail hard on
bluepeak (the relabelled control is gone, and verify would catch a lookalike);
the base + the repo bluepeak overlay must replay to SUCCESS through the extra
screen; the ladder telemetry must expose the relabelling as rung degradation
(the fleet drift signal); and the resolved capability must still refuse an
unapproved mutation — an overlay cannot launder away confirmation.

Server on 8776 (never 8000 — a live target may own it). No model calls.
"""
from __future__ import annotations

import threading

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
    CheckpointStep,
    Code,
    InsertSteps,
    LocatorStrategy,
    StepStatus,
    TenantOverlay,
    Tier,
    apply_overlay,
)

PORT = 8776
BASE = f"http://127.0.0.1:{PORT}"
ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"
OVERLAY = (
    REPO_ROOT / "capabilities" / "transfer_between_shares" / "overlays" / "bluepeak.json"
)

#: Chaos off — cross-tenant behaviour only; recovery has its own tests.
CALM = {"TESTING": True, "INTERSTITIAL_EVERY": 0, "SLOW_SECONDS": 0.0, "SESSION_TTL_S": 100_000}

# Real step ids from v1.0.0.json — the tenant-relabelled regions.
OPERATOR_ID_STEP = "s01-fill-the-operator-id-field"
SIGN_IN_STEP = "s03-click-the-sign-in-button"
CONFIRM_STEP = "s12-click-the-confirm-post-transfer"


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def server():
    app = create_app({**CALM, "TENANT": "bluepeak"})
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


@pytest.fixture(scope="module")
def surface():
    # One Chromium for the module: every engine.run() re-opens the entry URL,
    # so tests stay independent while saving launch time.
    from tellerly.surface.web import PlaywrightWebSurface

    web = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    yield web
    web.close()


# -------------------------------------------------------------------- helpers


def load_base() -> Capability:
    """The real recorded artifact, untouched — recorded once against ridgeline."""
    return Capability.from_json(ARTIFACT.read_text(encoding="utf-8"))


def load_overlay() -> TenantOverlay:
    return TenantOverlay.from_json(OVERLAY.read_text(encoding="utf-8"))


def load_resolved() -> Capability:
    """base + the repo bluepeak overlay: what `replay --tenant bluepeak` runs."""
    return apply_overlay(load_base(), load_overlay())


def inserted_steps():
    """The steps the repo overlay inserts (the verify screen), read from the
    overlay itself so the assertions track the authored ids, never guesses."""
    steps = []
    for op in load_overlay().operations:
        if isinstance(op, InsertSteps):
            steps.extend(op.steps)
    return steps


def gate_for(port: int) -> PolicyGate:
    # The artifact (and the overlay's hosts) pin the recorded instances
    # (8000 / 8010). Building the deployment∩capability intersection is the
    # CLI's job; these tests build the deployment gate directly so the engine
    # drives the in-test port without rewriting the read-only artifacts.
    return PolicyGate(
        DeploymentPolicy(
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_actions=["navigate", "click", "fill", "select", "press"],
            require_confirmation=True,
        )
    )


def params() -> dict[str, str]:
    return {
        "operator_id": "op-tenant",
        "access_key": "demo",
        "member_id": "101556",
        "from_share": "S00",
        "to_share": "S01",
        "amount": "10.00",
    }


def run_replay(surface, evidence_root, capability: Capability, *, approve: bool):
    engine = ReplayEngine(
        surface=surface,
        gate=gate_for(PORT),
        evidence_root=evidence_root,
        approve_mutations=approve,
    )
    return engine.run(capability, params(), BASE)


def balances(member_id: str) -> dict[str, float]:
    return {s["share_id"]: s["balance"] for s in data.get_member(member_id)["shares"]}


@pytest.fixture(scope="module")
def bluepeak_success(server, surface, tmp_path_factory):
    """Run (b): the resolved capability driven ONCE against live bluepeak;
    the success test and the drift-telemetry test read the same run."""
    data.reset()
    return run_replay(
        surface, tmp_path_factory.mktemp("evidence"), load_resolved(), approve=True
    )


# ---------------------------------------------------------------------- tests


def test_base_artifact_fails_hard_against_bluepeak(server, surface, tmp_path):
    """(a) The unoverlaid base must NOT limp through a differently-labelled
    tenant: the sign-in caption changed ("Sign In" -> "Log On"), so no rung
    resolves — or a lookalike fails verify. Either way: a hard, named failure
    at the relabelled region, never a wrong click."""
    data.reset()
    result = run_replay(surface, tmp_path / "evidence", load_base(), approve=True)

    assert result.status is not Tier.SUCCESS, result
    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code in (Code.TARGET_NOT_FOUND, Code.VERIFY_FAILED), result.failure
    assert result.failure.step_id == SIGN_IN_STEP, result.failure


def test_resolved_capability_succeeds_through_the_verify_screen(bluepeak_success):
    """(b) base + overlay replays to a TL- confirmation THROUGH bluepeak's
    extra verify screen — the inserted checkpoint and click show up in step
    telemetry like any recorded step."""
    result = bluepeak_success
    assert result.status is Tier.SUCCESS, result
    assert result.outputs is not None
    assert str(result.outputs["confirmation_no"]).startswith("TL-")

    inserted = inserted_steps()
    assert inserted, "the repo overlay must insert the verify-screen steps"
    telemetry = {outcome.step_id: outcome for outcome in result.steps}
    for step in inserted:
        assert step.id in telemetry, f"inserted step '{step.id}' missing from telemetry"
        assert telemetry[step.id].status is StepStatus.OK, telemetry[step.id]

    # The inserted checkpoint asserted the extra screen; the inserted click
    # resolved a real control on it.
    checkpoint = next(s for s in inserted if isinstance(s, CheckpointStep))
    assert "VERIFY TRANSFER" in (checkpoint.condition.text_visible or "")
    click = next(s for s in inserted if isinstance(s, ActStep))
    assert telemetry[click.id].resolved_via is not None, telemetry[click.id]

    # Exactly one posting: the acknowledge click committed nothing.
    after = balances("101556")
    assert after["S00"] == pytest.approx(1200.50 - 10.00)
    assert after["S01"] == pytest.approx(310.00 + 10.00)


def test_drift_telemetry_flags_the_relabelled_operator_field(bluepeak_success):
    """(c) The operator-id label reads "Teller ID:" on bluepeak, so the
    role/label rungs die and the form `name` rung carries the step. The run
    still succeeds — but the degradation is recorded per step, which is the
    fleet-wide drift signal an operator dashboards on."""
    outcome = next(s for s in bluepeak_success.steps if s.step_id == OPERATOR_ID_STEP)

    assert outcome.status is StepStatus.OK, outcome
    assert outcome.resolved_via is LocatorStrategy.NAME, outcome
    assert outcome.rung_index is not None and outcome.rung_index >= 1, outcome


def test_ridgeline_default_flow_is_unchanged():
    """(d) No TENANT config: byte-for-byte today's ridgeline behaviour —
    branding, captions, and no verify hop between the form and the confirm."""
    data.reset()
    app = create_app(dict(CALM))
    client = app.test_client()

    page = client.get("/login").get_data(as_text=True)
    assert "Ridgeline Credit Union" in page
    assert "RIDGELINE CU INTERNAL SYSTEM" in page
    assert "Operator ID:" in page
    assert "Teller ID:" not in page
    assert 'value="Sign In"' in page

    client.post("/login", data={"opid": "op-tenant", "opkey": "demo"})
    response = client.post(
        "/member/101556/panel/transfer",
        data={"src_share": "S00", "dst_share": "S01", "amt": "10.00", "memo": ""},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/member/101556/panel/confirm")
    assert "verify" not in response.headers["Location"]

    confirm = client.get("/member/101556/panel/confirm").get_data(as_text=True)
    assert "CONFIRM TRANSFER" in confirm
    assert "Confirm &amp; Post Transfer" in confirm
    assert "VERIFY TRANSFER" not in confirm


def test_resolved_capability_still_requires_confirmation(server, surface, tmp_path):
    """(e) The overlay grammar cannot express turning confirmation off, so the
    resolved capability replayed without approval must stop POLICY_BLOCKED at
    the (retargeted) confirm click — after the verify screen, before posting."""
    data.reset()
    before = balances("101556")
    result = run_replay(surface, tmp_path / "evidence", load_resolved(), approve=False)

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.POLICY_BLOCKED, result.failure
    assert result.failure.step_id == CONFIRM_STEP, result.failure
    detail = f"{result.failure.expected} {result.failure.observed}".lower()
    assert "approve" in detail  # the refusal must say how to authorize

    # The SAFE inserted acknowledge ran (the flow reached the confirm screen);
    # the mutation itself never posted.
    telemetry = {outcome.step_id: outcome for outcome in result.steps}
    click = next(s for s in inserted_steps() if isinstance(s, ActStep))
    assert telemetry[click.id].status is StepStatus.OK, telemetry.get(click.id)
    assert balances("101556") == before, "the blocked transfer must not have posted"
