"""Control transfer: who owns the live session, and how ownership moves.

State machine::

    AUTOMATION_RUNNING --escalate--> PAUSED --take_control--> HUMAN_CONTROL
            ^                          |                          |
            |----- resume(decision) ---+--------------------------|
                                       | timeout            abort |
                                       v                          v
                                    ABORTED                    ABORTED

Rules the machine enforces:

- Detecting "stuck" is not a heuristic: escalation fires only for a condition
  whose code is in the ESCALATABLE set — at a step whose retry budget is
  spent, or immediately when a declared HARD_FAILURE outcome is detected.
  That check lives in the replay engine; this machine only accepts the
  resulting `escalate`.
- Resume is a decision (CONTINUE / RETRY_STEP / SKIP_STEP), never a bare
  signal — after a human touched the page, the engine cannot infer intent.
  ABORT is its own transition, not a resume flavour.
- After any resume, the engine re-establishes state from scratch: both
  CONTINUE and RETRY_STEP re-resolve their target and re-check preconditions.
- An escalation nobody picks up times out into ABORTED: an unattended run
  fails loudly rather than holding a live banking session open.
"""
from __future__ import annotations

from enum import Enum

from tellerly.schema.escalation import ResumeDecision


class ControlState(str, Enum):
    AUTOMATION_RUNNING = "automation_running"
    PAUSED = "paused"              # escalation raised; nobody has taken control yet
    HUMAN_CONTROL = "human_control"
    ABORTED = "aborted"


class ControlEvent(str, Enum):
    ESCALATE = "escalate"
    TAKE_CONTROL = "take_control"
    RESUME = "resume"
    TIMEOUT = "timeout"
    ABORT = "abort"


_LEGAL: dict[tuple[ControlState, ControlEvent], ControlState] = {
    (ControlState.AUTOMATION_RUNNING, ControlEvent.ESCALATE): ControlState.PAUSED,
    (ControlState.PAUSED, ControlEvent.TAKE_CONTROL): ControlState.HUMAN_CONTROL,
    (ControlState.PAUSED, ControlEvent.TIMEOUT): ControlState.ABORTED,
    (ControlState.HUMAN_CONTROL, ControlEvent.RESUME): ControlState.AUTOMATION_RUNNING,
    (ControlState.HUMAN_CONTROL, ControlEvent.ABORT): ControlState.ABORTED,
}

#: Decisions that legally accompany RESUME. ABORT travels as its own event.
_RESUME_DECISIONS = frozenset(
    {ResumeDecision.CONTINUE, ResumeDecision.RETRY_STEP, ResumeDecision.SKIP_STEP}
)


class IllegalTransition(Exception):
    pass


class ControlToken:
    """The single ownership token for one live session.

    Pure state machine: it validates and records transitions. Wiring it to a
    real browser session and an operator surface is the execution layer's job.
    """

    def __init__(self) -> None:
        self.state = ControlState.AUTOMATION_RUNNING
        self.history: list[tuple[ControlState, ControlEvent, ControlState, ResumeDecision | None]] = []

    @property
    def holder(self) -> str:
        return {
            ControlState.AUTOMATION_RUNNING: "automation",
            ControlState.PAUSED: "nobody",
            ControlState.HUMAN_CONTROL: "human",
            ControlState.ABORTED: "nobody",
        }[self.state]

    def fire(
        self, event: ControlEvent, decision: ResumeDecision | str | None = None
    ) -> ControlState:
        if event is ControlEvent.RESUME:
            if decision is None:
                raise IllegalTransition(
                    "resume is a decision, not a signal — pass CONTINUE, RETRY_STEP or SKIP_STEP"
                )
            try:
                decision = ResumeDecision(decision)
            except ValueError as exc:
                raise IllegalTransition(f"unknown resume decision {decision!r}") from exc
            if decision not in _RESUME_DECISIONS:
                raise IllegalTransition(
                    f"{decision.value} does not resume; fire ABORT as its own event"
                )
        elif decision is not None:
            raise IllegalTransition(f"{event.value} takes no decision")

        key = (self.state, event)
        if key not in _LEGAL:
            raise IllegalTransition(
                f"no transition for event '{event.value}' in state '{self.state.value}'"
            )
        new_state = _LEGAL[key]
        self.history.append((self.state, event, new_state, decision))
        self.state = new_state
        return new_state
