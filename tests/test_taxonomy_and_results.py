"""Invariants of the outcome taxonomy and the result contracts."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tellerly.schema import (
    DEFAULT_TIER,
    ESCALATABLE,
    RETRYABLE,
    TERMINAL,
    Code,
    Economics,
    FailureDetail,
    OutcomeReport,
    ReplayResult,
    Tier,
)
from tellerly.schema.bindings import malformed_bindings, referenced_inputs, substitute


# ------------------------------------------------------------------- taxonomy


def test_every_code_has_a_tier():
    assert set(DEFAULT_TIER) == set(Code)


def test_every_code_has_a_handling_path():
    assert RETRYABLE | ESCALATABLE | TERMINAL == set(Code)


def test_retryable_codes_are_recoverable_tier():
    assert all(DEFAULT_TIER[code] is Tier.RECOVERABLE for code in RETRYABLE)


def test_terminal_never_overlaps_retry_or_escalate():
    assert not TERMINAL & RETRYABLE
    assert not TERMINAL & ESCALATABLE


def test_business_outcomes_are_all_terminal():
    business = {code for code, tier in DEFAULT_TIER.items() if tier is Tier.BUSINESS_OUTCOME}
    assert business <= TERMINAL


def test_session_expired_sits_in_both_camps():
    """The documented dual membership: retry wins while budget lasts, then
    escalation applies."""
    assert Code.SESSION_EXPIRED in RETRYABLE
    assert Code.SESSION_EXPIRED in ESCALATABLE


def test_no_code_is_success_tier():
    assert Tier.SUCCESS not in DEFAULT_TIER.values()


# ------------------------------------------------------------ result contract


def replay_result(**overrides):
    fields = dict(
        run_id="replay-001",
        capability_id="transfer_between_shares",
        capability_version="1.0.0",
        status=Tier.SUCCESS,
        outputs={"confirmation_no": "TL-004211"},
        economics=Economics(wall_time_s=3.2),
    )
    fields.update(overrides)
    return ReplayResult(**fields)


def test_success_carries_outputs():
    assert replay_result().outputs == {"confirmation_no": "TL-004211"}


def test_success_without_outputs_refused():
    with pytest.raises(ValidationError, match="must carry outputs"):
        replay_result(outputs=None)


def test_business_outcome_carries_the_report_and_nothing_else():
    result = replay_result(
        status=Tier.BUSINESS_OUTCOME,
        outputs=None,
        outcome=OutcomeReport(
            outcome_id="no_such_member",
            code=Code.NO_SUCH_RECORD,
            message="No member exists with that member number.",
        ),
    )
    assert result.outcome.code is Code.NO_SUCH_RECORD
    with pytest.raises(ValidationError, match="BUSINESS_OUTCOME payload only"):
        replay_result(
            outcome=OutcomeReport(
                outcome_id="x", code=Code.NO_SUCH_RECORD, message="on a SUCCESS result"
            )
        )


def test_hard_failure_carries_debuggable_detail():
    result = replay_result(
        status=Tier.HARD_FAILURE,
        outputs=None,
        failure=FailureDetail(
            step_id="run-search",
            code=Code.TARGET_NOT_FOUND,
            expected="a control matching the Search button ladder",
            observed="no rung matched; page shows 'Operator Sign-In'",
            evidence=["evidence/replay-001/step-08.png"],
        ),
    )
    assert result.failure.code is Code.TARGET_NOT_FOUND


def test_recoverable_is_never_a_terminal_status():
    with pytest.raises(ValidationError, match="never a terminal"):
        replay_result(status=Tier.RECOVERABLE, outputs=None)


def test_replay_refuses_nonzero_llm_calls():
    """Structural enforcement: 'no model in the loop' is part of the result
    contract, not a docstring promise."""
    with pytest.raises(ValidationError, match="llm_calls=0"):
        replay_result(economics=Economics(llm_calls=1, wall_time_s=3.2))


# ------------------------------------------------------------------- bindings


def test_bindings_resolve_by_lookup():
    assert substitute("/member/{{input.member_id}}", {"member_id": "101555"}) == "/member/101555"


def test_bindings_missing_input_raises():
    with pytest.raises(KeyError, match="member_id"):
        substitute("/member/{{input.member_id}}", {})


def test_bindings_are_single_pass():
    """A value containing mustache stays literal — the grammar is closed."""
    out = substitute("{{input.a}}", {"a": "{{input.b}}", "b": "nope"})
    assert out == "{{input.b}}"


def test_malformed_mustache_is_detected():
    assert malformed_bindings("x {{inputs.member_id}} y") == ["{{inputs.member_id}}"]
    assert malformed_bindings("x {{ input.ok }} y") == []
    assert referenced_inputs("{{ input.ok }}") == {"ok"}


def test_unbalanced_mustache_is_detected():
    assert malformed_bindings("/login/{{input.member_id")  # unclosed
    assert malformed_bindings("/login/{{input.member_id}")  # half-closed
    assert malformed_bindings("stray }} here")
    assert malformed_bindings("{{inputs.\nmember}}")  # newline inside


# ------------------------------------------- regression: review findings


def test_replay_refuses_any_model_usage_not_just_calls():
    """llm_calls=0 with smuggled tokens/cost is still model usage."""
    with pytest.raises(ValidationError, match="llm_calls=0"):
        replay_result(
            economics=Economics(input_tokens=90_000, cost_usd=4.20, wall_time_s=3.2)
        )


def test_outcome_report_code_must_be_business_tier():
    """A 500 cannot be dressed up as 'the app said no' at the caller boundary."""
    with pytest.raises(ValidationError, match="not fleet-classified"):
        replay_result(
            status=Tier.BUSINESS_OUTCOME,
            outputs=None,
            outcome=OutcomeReport(outcome_id="x", code=Code.APP_FAULT, message="nope"),
        )


def test_failure_code_must_not_be_business_tier():
    with pytest.raises(ValidationError, match="report it as one"):
        replay_result(
            status=Tier.HARD_FAILURE,
            outputs=None,
            failure=FailureDetail(
                step_id=None, code=Code.NO_SUCH_RECORD, expected="e", observed="o"
            ),
        )


def test_retry_exhausted_recoverable_surfaces_inside_a_failure():
    """The documented post-budget path: HARD_FAILURE status, recoverable code
    preserved in the detail."""
    result = replay_result(
        status=Tier.HARD_FAILURE,
        outputs=None,
        failure=FailureDetail(
            step_id="at-member-record",
            code=Code.SLOW_RESPONSE,
            expected="member record within the step timeout",
            observed="3 attempts exceeded the budget",
        ),
    )
    assert result.failure.code is Code.SLOW_RESPONSE


def test_drift_telemetry_travels_as_a_pair():
    from tellerly.schema import StepOutcome, StepStatus

    with pytest.raises(ValidationError, match="travel together"):
        StepOutcome(step_id="s", status=StepStatus.OK, rung_index=2)
    with pytest.raises(ValidationError, match="travel together"):
        StepOutcome(step_id="s", status=StepStatus.OK, resolved_via="name")


def test_discovery_artifact_path_iff_goal_met():
    from tellerly.schema import DiscoveryResult, DiscoveryStatus

    with pytest.raises(ValidationError, match="exactly when"):
        DiscoveryResult(
            run_id="d1", goal="g", status=DiscoveryStatus.GAVE_UP,
            artifact_path="capabilities/x.json", economics=Economics(),
        )
    with pytest.raises(ValidationError, match="exactly when"):
        DiscoveryResult(
            run_id="d1", goal="g", status=DiscoveryStatus.GOAL_MET, economics=Economics()
        )
