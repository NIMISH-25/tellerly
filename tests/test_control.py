"""The control-transfer state machine: ownership of a live session moves only
along the drawn edges, and resume always carries a decision."""
from __future__ import annotations

import pytest

from tellerly.kernel.control import (
    ControlEvent,
    ControlState,
    ControlToken,
    IllegalTransition,
)
from tellerly.schema import ResumeDecision


def test_the_full_handoff_cycle():
    token = ControlToken()
    assert token.state is ControlState.AUTOMATION_RUNNING
    assert token.holder == "automation"

    token.fire(ControlEvent.ESCALATE)
    assert token.state is ControlState.PAUSED
    assert token.holder == "nobody"

    token.fire(ControlEvent.TAKE_CONTROL)
    assert token.state is ControlState.HUMAN_CONTROL
    assert token.holder == "human"

    token.fire(ControlEvent.RESUME, ResumeDecision.RETRY_STEP)
    assert token.state is ControlState.AUTOMATION_RUNNING
    assert token.holder == "automation"

    # Every hop is on the record, decisions included.
    assert [event for _, event, _, _ in token.history] == [
        ControlEvent.ESCALATE,
        ControlEvent.TAKE_CONTROL,
        ControlEvent.RESUME,
    ]
    assert token.history[-1][3] is ResumeDecision.RETRY_STEP


def test_unattended_escalation_times_out_to_aborted():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TIMEOUT)
    assert token.state is ControlState.ABORTED


def test_human_can_abort():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TAKE_CONTROL)
    token.fire(ControlEvent.ABORT)
    assert token.state is ControlState.ABORTED


def test_resume_requires_a_decision():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TAKE_CONTROL)
    with pytest.raises(IllegalTransition, match="decision, not a signal"):
        token.fire(ControlEvent.RESUME)


def test_abort_is_not_a_resume_flavour():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TAKE_CONTROL)
    with pytest.raises(IllegalTransition, match="fire ABORT"):
        token.fire(ControlEvent.RESUME, ResumeDecision.ABORT)


@pytest.mark.parametrize(
    "event",
    [ControlEvent.TAKE_CONTROL, ControlEvent.RESUME, ControlEvent.TIMEOUT, ControlEvent.ABORT],
)
def test_automation_state_only_escalates(event):
    token = ControlToken()
    decision = ResumeDecision.CONTINUE if event is ControlEvent.RESUME else None
    with pytest.raises(IllegalTransition):
        token.fire(event, decision)


def test_aborted_is_terminal():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TIMEOUT)
    for event in ControlEvent:
        with pytest.raises(IllegalTransition):
            token.fire(
                event,
                ResumeDecision.CONTINUE if event is ControlEvent.RESUME else None,
            )


def test_events_other_than_resume_take_no_decision():
    token = ControlToken()
    with pytest.raises(IllegalTransition, match="takes no decision"):
        token.fire(ControlEvent.ESCALATE, ResumeDecision.CONTINUE)


def test_unknown_resume_decision_is_illegal_not_a_crash():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TAKE_CONTROL)
    with pytest.raises(IllegalTransition, match="unknown resume decision"):
        token.fire(ControlEvent.RESUME, "bogus")
    assert token.state is ControlState.HUMAN_CONTROL  # nothing moved


def test_string_decision_is_coerced_into_the_enum():
    token = ControlToken()
    token.fire(ControlEvent.ESCALATE)
    token.fire(ControlEvent.TAKE_CONTROL)
    token.fire(ControlEvent.RESUME, "continue")
    assert token.history[-1][3] is ResumeDecision.CONTINUE  # history stays typed
