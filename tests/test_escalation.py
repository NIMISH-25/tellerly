"""Human escalation & handoff: when replay is stuck on something a person can
fix, control moves to an operator THROUGH the same Surface and PolicyGate as
automation — that is the audit story — and comes back as an explicit decision
(CONTINUE / RETRY_STEP / SKIP_STEP / ABORT), never a bare signal.

Covered here: the abort path, skip-to-success, a real human action driven
through the surface, the unattended timeout, unchanged behaviour when no
handler is configured, the one-intervention-per-step loop guard, and the
terminal console with an injected input_fn. All engine cases run against the
REAL recorded artifact (mutated copies where a case needs a broken step) and
the live mock console.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.config import REPO_ROOT
from tellerly.kernel.control import ControlEvent, ControlState, ControlToken
from tellerly.kernel.evidence import RunLog
from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
from tellerly.kernel.operator import (
    EscalationTimeout,
    OperatorSession,
    ScriptedOperator,
    TerminalOperatorConsole,
)
from tellerly.kernel.redaction import Redactor
from tellerly.replay import ReplayEngine
from tellerly.schema import (
    ActionType,
    Capability,
    Code,
    InterventionRequest,
    ResumeDecision,
    StepStatus,
    SurfaceFeature,
    Tier,
)
from tellerly.surface.base import (
    ControlFacts,
    PageObservation,
    ProbeResult,
    Resolution,
    Surface,
)

PORT = 8773  # this file's own server; 8774 stays free for a second one; never 8000
BASE = f"http://127.0.0.1:{PORT}"
ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"

#: Chaos off: escalation behaviour must be triggered by the CASE (a restricted
#: member, a broken step), never by background flakiness.
CALM = {"TESTING": True, "INTERSTITIAL_EVERY": 0, "SLOW_SECONDS": 0.0, "SESSION_TTL_S": 100_000}

MID_CHECKPOINT = "s04-checkpoint"
CONFIRM_STEP = "s12-click-the-confirm-post-transfer"
CONFIRM_BUTTON = "Confirm & Post Transfer"
OPERATOR_NAME = "esc-tester"


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
    """Seed balances/ledger per test — the resume cases post real transfers."""
    data.reset()
    yield


@pytest.fixture(scope="module")
def surface():
    # One Chromium for the module: every engine.run() opens the entry URL, so
    # tests stay independent while saving ~10s of launches.
    from tellerly.surface.web import PlaywrightWebSurface

    web = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    yield web
    web.close()


# -------------------------------------------------------------------- helpers


def load_capability() -> Capability:
    return Capability.from_json(ARTIFACT.read_text(encoding="utf-8"))


def artifact_dict() -> dict:
    """A mutable deep copy for the cases that deliberately break one step."""
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def gate_for(port: int) -> PolicyGate:
    # The recorded artifact pins allowed_hosts to the discovery-time host; the
    # tests build the deployment gate directly so the engine runs against the
    # test port without rewriting the read-only artifact.
    return PolicyGate(
        DeploymentPolicy(
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_actions=["navigate", "click", "fill", "select", "press"],
            require_confirmation=True,
        )
    )


def params(**overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        "operator_id": "op-esc",
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
    handler,
    capability: Capability | None = None,
    values: dict[str, str] | None = None,
    approve: bool = True,
):
    engine = ReplayEngine(
        surface=surface,
        gate=gate_for(PORT),
        evidence_root=tmp_path / "evidence",
        approve_mutations=approve,
        escalation=handler,
        operator_name=OPERATOR_NAME,
    )
    return engine.run(capability or load_capability(), values or params(), BASE)


def balances(member_id: str) -> dict[str, float]:
    return {s["share_id"]: s["balance"] for s in data.get_member(member_id)["shares"]}


def events_text(result) -> str:
    assert result.evidence_dir is not None, result
    return (_evidence_file(result.evidence_dir) / "events.jsonl").read_text(encoding="utf-8")


def _evidence_file(recorded: str | None) -> Path:
    """Evidence paths are stored repo-relative when they live inside the repo
    and absolute otherwise (these tests use tmp dirs) — resolve either form."""
    assert recorded, "expected an evidence path on the record"
    path = Path(recorded)
    return path if path.is_absolute() else REPO_ROOT / path


def broken_checkpoint_capability() -> Capability:
    """s04 asserts a banner that never renders -> CHECKPOINT_FAILED once the
    retry budget is spent. The short timeout is test economy only: three
    doomed 10s waits would add half a minute to every case using this."""
    raw = artifact_dict()
    step = next(s for s in raw["steps"] if s["id"] == MID_CHECKPOINT)
    step["condition"]["text_visible"] = "A BANNER THAT NEVER RENDERS c4e11d"
    step["timeout_s"] = 0.5
    return Capability.model_validate(raw)


def broken_confirm_capability() -> Capability:
    """Every rung of s12's ladder matches nothing (order still durability-legal:
    role ahead of css) -> TARGET_NOT_FOUND, while the real confirm button sits
    on the page waiting for the human."""
    raw = artifact_dict()
    step = next(s for s in raw["steps"] if s["id"] == CONFIRM_STEP)
    step["target"]["ladder"] = [
        {
            "strategy": "role",
            "role": "button",
            "name": "A Button That Never Existed",
            "confidence": 0.5,
        },
        {"strategy": "css", "css": 'input[value="never-a-real-control-c4e11d"]', "confidence": 0.5},
    ]
    return Capability.model_validate(raw)


class _CapturingScript:
    """fn for ScriptedOperator that records what it was handed. Assertions on
    the request stay in the test BODY: an AssertionError raised inside the
    handler would be swallowed by the engine's never-raise boundary and morph
    into EXECUTION_ERROR, masking the real diagnosis."""

    def __init__(self, drive) -> None:
        self.requests: list[InterventionRequest] = []
        self.act_messages: list[str] = []
        self._drive = drive

    def __call__(self, request: InterventionRequest, session: OperatorSession) -> ResumeDecision:
        self.requests.append(request)
        return self._drive(request, session)


# ------------------------------------------------------- engine escalation (a)


def test_operator_abort_on_restricted_member_is_audited(server, surface, tmp_path):
    """Member 55555 hits the NOT AUTHORIZED page: permission_denied escalates,
    the human looks, leaves a note, and aborts — and the whole engagement is
    preserved as an InterventionRecord with real evidence files."""

    def drive(request: InterventionRequest, session: OperatorSession) -> ResumeDecision:
        session.look()
        session.note("restricted record — not ours to touch, aborting")
        return ResumeDecision.ABORT

    script = _CapturingScript(drive)
    result = run_replay(
        surface, tmp_path, handler=ScriptedOperator(script), values=params(member_id="55555")
    )

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.ABORTED_BY_OPERATOR

    assert len(result.escalations) == 1, result.escalations
    record = result.escalations[0]
    assert record.request.reason_code is Code.PERMISSION_DENIED
    assert record.request.screenshot_path, "the operator was owed a screenshot"
    snapshot = _evidence_file(record.request.dom_snapshot_path)
    assert snapshot.is_file(), "dom snapshot file must exist"
    assert snapshot.stat().st_size > 0, "dom snapshot must not be empty"
    assert record.actions, "look/note must land in the audit trail"
    assert record.decision is ResumeDecision.ABORT
    assert record.operator == OPERATOR_NAME

    events = events_text(result)
    assert "escalation_raised" in events
    assert "escalation_resolved" in events


# ------------------------------------------------------------ skip-to-success (b)


def test_skip_step_on_broken_checkpoint_resumes_to_success(server, surface, tmp_path):
    """A checkpoint asserting text that never appears escalates as
    checkpoint_failed; the operator declares the step moot (SKIP_STEP) and the
    run proceeds all the way to SUCCESS with outputs."""
    script = _CapturingScript(lambda request, session: ResumeDecision.SKIP_STEP)

    result = run_replay(
        surface,
        tmp_path,
        handler=ScriptedOperator(script),
        capability=broken_checkpoint_capability(),
    )

    assert result.status is Tier.SUCCESS, result
    assert result.outputs is not None
    assert str(result.outputs["confirmation_no"]).startswith("TL-")

    assert len(result.escalations) == 1, result.escalations
    assert len(script.requests) == 1
    assert script.requests[0].reason_code is Code.CHECKPOINT_FAILED

    outcome = next(step for step in result.steps if step.step_id == MID_CHECKPOINT)
    assert outcome.status is StepStatus.SKIPPED, outcome


# --------------------------------------- human action through the surface (c)


def test_continue_after_human_click_through_the_surface(server, surface, tmp_path):
    """s12's ladder is dead, so automation cannot find the confirm button —
    but the human can, and clicks it THROUGH the same Surface/PolicyGate. The
    click lands in the InterventionRecord and the evidence, and CONTINUE lets
    the engine collect the confirmation the human's click produced."""

    def drive(request: InterventionRequest, session: OperatorSession) -> ResumeDecision:
        observation = session.look()
        control = next(
            c for c in observation.controls if c.accessible_name == CONFIRM_BUTTON
        )
        message = session.act(control.uid, ActionType.CLICK)
        script.act_messages.append(message)
        return ResumeDecision.CONTINUE

    script = _CapturingScript(drive)
    result = run_replay(
        surface,
        tmp_path,
        handler=ScriptedOperator(script),
        capability=broken_confirm_capability(),
    )

    assert result.status is Tier.SUCCESS, result
    assert result.outputs is not None
    assert str(result.outputs["confirmation_no"]).startswith("TL-")

    assert len(result.escalations) == 1, result.escalations
    record = result.escalations[0]
    assert record.request.reason_code is Code.TARGET_NOT_FOUND
    assert record.actions, "the click must be preserved as a HumanAction"
    assert any("click" in action.description.lower() for action in record.actions), record.actions
    assert script.act_messages and all(isinstance(m, str) for m in script.act_messages), (
        "act() answers with a message, never a crash"
    )
    assert "human_action" in events_text(result)

    # The transfer posted exactly ONCE — by the human. CONTINUE must not make
    # the engine re-run the mutating step it was told is already done.
    after = balances("101556")
    assert after["S00"] == pytest.approx(1200.50 - 15.00)
    assert after["S01"] == pytest.approx(310.00 + 15.00)


# ------------------------------------------------------------------ timeout (d)


def test_unanswered_escalation_times_out_into_a_loud_abort(server, surface, tmp_path):
    """Nobody picks up: the run must fail loudly (ESCALATION_TIMEOUT) rather
    than hold a live banking session open, and the control token must end
    ABORTED via TIMEOUT — visible in the evidence trail."""

    def drive(request: InterventionRequest, session: OperatorSession) -> ResumeDecision:
        raise EscalationTimeout("nobody picked up before the deadline")

    result = run_replay(
        surface, tmp_path, handler=ScriptedOperator(drive), values=params(member_id="55555")
    )

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.ESCALATION_TIMEOUT
    assert "deadline" in result.failure.expected

    events = events_text(result).lower()
    assert "escalation_raised" in events
    # The token history (PAUSED --timeout--> ABORTED) is exposed through the
    # escalation timeout/resolution event payload.
    assert "timeout" in events
    assert "aborted" in events


# --------------------------------------------------------------- no handler (e)


def test_without_a_handler_behaviour_is_unchanged(server, surface, tmp_path):
    """No handler configured: the restricted member still returns the plain
    permission_denied hard failure, the seam note still marks where the real
    mechanism would plug in, and no escalation is recorded."""
    result = run_replay(surface, tmp_path, handler=None, values=params(member_id="55555"))

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.PERMISSION_DENIED
    assert "escalation seam" in result.failure.observed
    assert result.escalations == []


# ------------------------------------------------- one intervention per step (f)


def test_retry_step_never_escalates_the_same_step_twice(server, surface, tmp_path):
    """RETRY_STEP against a PERMANENTLY broken checkpoint: the retried failure
    must fail hard without paging the operator again — one intervention per
    step is the loop guard."""
    script = _CapturingScript(lambda request, session: ResumeDecision.RETRY_STEP)

    result = run_replay(
        surface,
        tmp_path,
        handler=ScriptedOperator(script),
        capability=broken_checkpoint_capability(),
    )

    assert result.status is Tier.HARD_FAILURE, result
    assert result.failure is not None
    assert result.failure.code is Code.CHECKPOINT_FAILED
    assert len(result.escalations) == 1, "the retried failure must NOT re-escalate"
    assert len(script.requests) == 1, "the handler must have been engaged exactly once"


# --------------------------------------------------- terminal console unit (g)


class FakeSurface(Surface):
    """Just enough surface for the console unit tests: observe() answers with
    one canned control; anything that would DRIVE a page trips an assertion,
    because these console cases never authorize an action.

    Deliberately does NOT override dom_snapshot: instantiating this class
    proves the base-class default is concrete (non-abstract), per the spec.
    """

    def features(self) -> frozenset[SurfaceFeature]:
        return frozenset(SurfaceFeature)

    def open(self, url: str) -> None:
        raise AssertionError("console unit tests never open a page")

    def navigate(self, path: str) -> None:
        raise AssertionError("console unit tests never navigate")

    def observe(self) -> PageObservation:
        return PageObservation(
            url=f"{BASE}/member/101556",
            path="/member/101556",
            title="Mock Teller Console",
            text="Member Record 101556",
            controls=[
                ControlFacts(
                    uid="o1c0",
                    kind="input:submit",
                    role="button",
                    accessible_name=CONFIRM_BUTTON,
                )
            ],
        )

    def act(self, uid: str, action: ActionType, value: str | None = None) -> None:
        raise AssertionError("console unit tests never act on a control")

    def probe(self, rung, frame, uid: str | None = None) -> ProbeResult:
        raise AssertionError("console unit tests never probe")

    def resolve(self, target) -> Resolution:
        raise AssertionError("console unit tests never resolve")

    def read_text(self, uid: str) -> str:
        raise AssertionError("console unit tests never read")

    def locate_value_cell(self, anchor_text: str) -> ControlFacts | None:
        return None

    def find_text(self, text: str, timeout_s: float = 2.0) -> bool:
        return False

    def current_path(self) -> str:
        return "/member/101556"

    def current_url(self) -> str:
        return f"{BASE}/member/101556"

    def screenshot(self, path: Path) -> None:
        pass  # evidence capture is tolerated — it is never an operator action

    def close(self) -> None:
        pass


def _feed(commands: list[str]):
    """input_fn double: hands out the scripted commands, then end-of-stream —
    mirroring builtins.input (optional prompt argument, EOFError at EOF)."""
    queue = iter(commands)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError

    return fake_input


def _console_session(tmp_path: Path, deadline: datetime) -> OperatorSession:
    redactor = Redactor()
    log = RunLog(tmp_path / "evidence", "console-unit", redactor)
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)  # the engine escalates before any handler runs
    return OperatorSession(
        surface=FakeSurface(),
        gate=gate_for(PORT),
        log=log,
        token=token,
        redactor=redactor,
        deadline=deadline,
    )


def _intervention_request(deadline_at: datetime) -> InterventionRequest:
    now = datetime.now(timezone.utc)
    return InterventionRequest(
        id=uuid.uuid4().hex,
        run_id="replay-console-unit",
        capability_id="transfer_between_shares",
        step_id=CONFIRM_STEP,
        url=f"{BASE}/member/101556",
        reason_code=Code.TARGET_NOT_FOUND,
        message="exactly one control matching the confirm button | not_found",
        requested_at=now,
        deadline_at=deadline_at,
    )


def test_console_look_then_done_abort_records_the_note(tmp_path):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    session = _console_session(tmp_path, deadline)
    console = TerminalOperatorConsole(input_fn=_feed(["look", "done abort all good"]))

    decision = console.handle(_intervention_request(deadline_at=deadline), session)

    assert decision is ResumeDecision.ABORT
    assert any("all good" in action.description for action in session.actions), session.actions
    # The console fired TAKE_CONTROL when engagement started, and must NOT
    # have fired RESUME/ABORT itself — those transitions belong to the engine.
    assert session.token.state is ControlState.HUMAN_CONTROL


def test_console_expired_deadline_raises_escalation_timeout(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    session = _console_session(tmp_path, past)
    # A command IS available, so an EOF cannot be what trips the timeout: the
    # deadline check around each command must raise on its own.
    console = TerminalOperatorConsole(input_fn=_feed(["note should never matter"]))

    with pytest.raises(EscalationTimeout):
        console.handle(_intervention_request(deadline_at=past), session)
