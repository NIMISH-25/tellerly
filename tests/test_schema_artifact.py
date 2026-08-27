"""The artifact schema's coherence validators: a capability that lies about
itself must not load."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from capability_fixture import make_capability
from tellerly.schema import (
    ActionType,
    ActStep,
    AmbiguityPolicy,
    AnchorRung,
    Capability,
    Code,
    CssRung,
    DeclaredOutcome,
    InputDecl,
    LocatorStrategy,
    NameRung,
    Risk,
    RoleRung,
    Sensitivity,
    StateCondition,
    SurfaceFeature,
    Target,
    Tier,
    VerifyPredicate,
)


def steps_with(base, replace=None, extra=None):
    steps = [s for s in base.steps]
    if replace:
        steps = [replace.get(s.id, s) for s in steps]
    if extra:
        steps += extra
    return steps


# ------------------------------------------------------------------ happy path


def test_reference_capability_loads():
    capability = make_capability()
    assert capability.id == "transfer_between_shares"
    assert len(capability.steps) == 16


def test_json_roundtrip_is_lossless():
    capability = make_capability()
    assert Capability.from_json(capability.to_json()) == capability


def test_json_schema_export():
    schema = Capability.contract_schema()
    assert schema["title"] == "Capability"
    assert "steps" in schema["properties"]


def test_required_features_cover_all_rungs_and_frames():
    features = make_capability().required_features()
    # Ladders span every strategy in use, and the panel steps need frames.
    assert SurfaceFeature.FRAMES in features
    assert SurfaceFeature.NAME_QUERY in features
    assert SurfaceFeature.DOM_QUERY in features  # CSS fallbacks count too
    assert SurfaceFeature.ANCHOR_QUERY in features


def test_element_id_is_not_a_representable_strategy():
    """ids rotate per render on legacy surfaces; the mistake must be
    unrepresentable, not discouraged."""
    assert "id" not in {strategy.value for strategy in LocatorStrategy}


def test_confirmation_defaults_on():
    assert make_capability().safety.require_confirmation is True


# ------------------------------------------------------------- load-time refusals


def refuses(match, **overrides):
    with pytest.raises(ValidationError, match=match):
        make_capability(**overrides)


def test_refuses_undeclared_binding():
    base = make_capability()
    bad = ActStep(id="open-login", action=ActionType.NAVIGATE, value="/login/{{input.branch}}")
    refuses("undeclared input 'branch'", steps=steps_with(base, replace={"open-login": bad}))


def test_refuses_malformed_binding():
    base = make_capability()
    bad = ActStep(id="open-login", action=ActionType.NAVIGATE, value="/login/{{inputs.member_id}}")
    refuses("malformed binding", steps=steps_with(base, replace={"open-login": bad}))


def test_refuses_unused_input():
    inputs = dict(make_capability().inputs)
    inputs["dead_weight"] = InputDecl(description="Declared but never bound.")
    refuses("never referenced", inputs=inputs)


def test_refuses_out_of_order_ladder():
    with pytest.raises(ValidationError, match="durability order"):
        Target(
            description="a field",
            ladder=[
                CssRung(css="input[name=x]", confidence=1.0),   # CSS before name:
                NameRung(name="x", confidence=1.0),             # least durable first
            ],
            verify=VerifyPredicate(control="input"),
        )


def test_refuses_duplicate_step_ids():
    base = make_capability()
    dupe = ActStep(id="open-login", action=ActionType.NAVIGATE, value="/login")
    refuses("duplicate step id", steps=steps_with(base, extra=[dupe]))


def test_refuses_uncaptured_declared_output():
    base = make_capability()
    steps = [s for s in base.steps if s.id != "read-confirmation-no"]
    refuses("never captured", steps=steps)


def test_refuses_read_of_undeclared_output():
    refuses("undeclared output 'confirmation_no'", outputs={})


def test_refuses_action_outside_own_safety_allowlist():
    safety = make_capability().safety.model_copy(
        update={"allowed_actions": [ActionType.NAVIGATE, ActionType.CLICK, ActionType.FILL]}
    )
    refuses("outside the", safety=safety)  # the SELECT steps are no longer allowed


def test_refuses_outcome_relabelling_a_failure_tier():
    with pytest.raises(ValidationError, match="relabelling"):
        DeclaredOutcome(
            id="softened_fault",
            code=Code.APP_FAULT,
            disposition=Tier.BUSINESS_OUTCOME,  # a 500 is not an answer
            message="pretend this is fine",
            detect=StateCondition(text_visible="TELLERLY INTERNAL FAULT"),
        )


def test_success_is_not_a_declarable_disposition():
    with pytest.raises(ValidationError):
        DeclaredOutcome(
            id="fake_success",
            code=Code.NO_SUCH_RECORD,
            disposition=Tier.SUCCESS,
            message="not found means done, right?",
            detect=StateCondition(text_visible="No records found"),
        )


def test_refuses_recovery_on_a_non_recoverable_outcome():
    recovery = [
        ActStep(
            id="dismiss",
            action=ActionType.CLICK,
            target=Target(
                description="anything",
                ladder=[RoleRung(role="button", name="OK", confidence=1.0)],
                verify=VerifyPredicate(control="input"),
            ),
        )
    ]
    with pytest.raises(ValidationError, match="recoverable outcomes"):
        DeclaredOutcome(
            id="not_found_with_recovery",
            code=Code.NO_SUCH_RECORD,
            disposition=Tier.BUSINESS_OUTCOME,
            message="no such record",
            detect=StateCondition(text_visible="No records found"),
            recovery=recovery,
        )


def test_refuses_mutating_recovery_step():
    recovery = [
        ActStep(
            id="dismiss",
            action=ActionType.CLICK,
            risk=Risk.MUTATING,
            target=Target(
                description="anything",
                ladder=[RoleRung(role="button", name="OK", confidence=1.0)],
                verify=VerifyPredicate(control="input"),
            ),
        )
    ]
    with pytest.raises(ValidationError, match="recovery never mutates"):
        DeclaredOutcome(
            id="interstitial",
            code=Code.INTERSTITIAL_PRESENT,
            disposition=Tier.RECOVERABLE,
            message="maintenance notice",
            detect=StateCondition(text_visible="MAINTENANCE"),
            recovery=recovery,
        )


def test_refuses_mutating_step_that_tolerates_ambiguity():
    with pytest.raises(ValidationError, match="fail on ambiguity"):
        ActStep(
            id="post",
            action=ActionType.CLICK,
            risk=Risk.MUTATING,
            target=Target(
                description="the confirm button",
                ladder=[RoleRung(role="button", name="Confirm", confidence=1.0)],
                verify=VerifyPredicate(control="input"),
                on_ambiguous=AmbiguityPolicy.FIRST,
            ),
        )


def test_refuses_sensitive_input_with_example():
    with pytest.raises(ValidationError, match="example"):
        InputDecl(description="SSN", sensitivity=Sensitivity.PII, example="123-45-6789")


def test_success_condition_is_required():
    fields = {"success": None}
    with pytest.raises(ValidationError):
        make_capability(**fields)


def test_refuses_empty_state_condition():
    with pytest.raises(ValidationError, match="at least one"):
        StateCondition()


def test_refuses_confidence_out_of_range():
    with pytest.raises(ValidationError):
        NameRung(name="x", confidence=1.5)
    with pytest.raises(ValidationError):
        NameRung(name="x", confidence=0.0)


def test_refuses_bad_semver():
    refuses("version", version="v1")


# ----------------------------------------------- regression: review findings


def _safe_click(step_id, button_name):
    return ActStep(
        id=step_id,
        action=ActionType.CLICK,
        target=Target(
            description=f"the {button_name} button",
            ladder=[RoleRung(role="button", name=button_name, confidence=1.0)],
            verify=VerifyPredicate(control="input"),
        ),
    )


def test_refuses_recovery_action_outside_allowlist():
    """Recovery steps execute at replay too — the allowlist covers them."""
    base = make_capability()
    bad = DeclaredOutcome(
        id="odd_notice",
        code=Code.INTERSTITIAL_PRESENT,
        disposition=Tier.RECOVERABLE,
        message="a notice",
        detect=StateCondition(text_visible="NOTICE"),
        recovery=[ActStep(id="dismiss-with-key", action=ActionType.PRESS, value="Escape")],
    )
    refuses("outside the", outcomes=[*base.outcomes, bad])  # PRESS is not allowlisted


def test_refuses_recovery_id_colliding_with_main_step():
    base = make_capability()
    bad = DeclaredOutcome(
        id="odd_notice",
        code=Code.INTERSTITIAL_PRESENT,
        disposition=Tier.RECOVERABLE,
        message="a notice",
        detect=StateCondition(text_visible="NOTICE"),
        recovery=[_safe_click("post-transfer", "OK")],  # collides with the main flow
    )
    refuses("duplicate step id", outcomes=[*base.outcomes, bad])


def test_refuses_unclosed_binding():
    base = make_capability()
    bad = ActStep(id="open-login", action=ActionType.NAVIGATE, value="/login/{{input.member_id")
    refuses("malformed binding", steps=steps_with(base, replace={"open-login": bad}))


def test_css_rung_cannot_smuggle_an_id_selector_back_in():
    for css in ("#member-row", "tr [id='row']", "input[id^=fld_]"):
        with pytest.raises(ValidationError, match="ids rotate"):
            CssRung(css=css, confidence=0.5)


def test_binding_in_prose_does_not_count_as_usage():
    """Only executable strings use inputs; a mention in documentation doesn't."""
    inputs = dict(make_capability().inputs)
    inputs["ghost"] = InputDecl(description="Referenced only in prose.")
    refuses(
        "never referenced",
        inputs=inputs,
        description="This capability definitely uses {{input.ghost}}, honest.",
    )


def test_refuses_uncompilable_regex_fields():
    from tellerly.schema import ExtractSpec

    with pytest.raises(ValidationError, match="compile"):
        InputDecl(description="x", pattern="(unclosed")
    with pytest.raises(ValidationError, match="compile"):
        ExtractSpec(pattern="(unclosed")
    with pytest.raises(ValidationError, match="capture group"):
        ExtractSpec(pattern=r"TL-\d+")  # narrows nothing without a group
    with pytest.raises(ValidationError, match="compile"):
        StateCondition(url_path_matches="(unclosed")


def test_url_regex_with_bindings_compiles_after_masking():
    StateCondition(url_path_matches="/member/{{input.member_id}}")
