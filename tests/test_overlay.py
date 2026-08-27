"""Multi-tenant overlays: the grammar is CLOSED and the dangerous change is
inexpressible, not detected.

A tenant overlay may re-point WHERE a control lives (retarget), ADD strictness
(insert checkpoints/steps, add outcomes), and re-bind WHERE the tenant instance
runs (hosts). It structurally cannot remove steps, weaken outcomes, change the
success condition, or touch allowed_actions / require_confirmation / limits —
those attacks must die as ValidationErrors at parse time or OverlayErrors at
apply time, never reach a browser. No browser or model is needed here: the
overlay layer is pure data + a pure function.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from capability_fixture import make_capability
from tellerly.config import REPO_ROOT
from tellerly.kernel.store import CapabilityStore
from tellerly.schema import (
    ActionType,
    ActStep,
    AddOutcome,
    AmbiguityPolicy,
    Capability,
    CheckpointStep,
    Code,
    CssRung,
    DeclaredOutcome,
    InsertSteps,
    OverlayError,
    RetargetStep,
    Risk,
    RoleRung,
    SetEntry,
    StateCondition,
    Target,
    TenantOverlay,
    Tier,
    VerifyPredicate,
    apply_overlay,
)

ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"
REPO_OVERLAY = (
    REPO_ROOT / "capabilities" / "transfer_between_shares" / "overlays" / "bluepeak.json"
)

# The REAL artifact's step ids (read from v1.0.0.json, not guessed).
REAL_SIGN_IN = "s03-click-the-sign-in-button"
REAL_CONTINUE = "s11-click-the-continue-button"
REAL_CONFIRM = "s12-click-the-confirm-post-transfer"


# -------------------------------------------------------------------- helpers


def make_overlay(**overrides) -> TenantOverlay:
    """A valid overlay against the hand-authored fixture capability."""
    fields = dict(
        schema_version="1",
        tenant_id="bluepeak",
        capability_id="transfer_between_shares",
        base_version="1.0.0",
        description="Bluepeak-branded skin of the same vendor console.",
        operations=[],
    )
    fields.update(overrides)
    return TenantOverlay(**fields)


def overlay_dict(**overrides) -> dict:
    """Raw-dict form for the grammar tests — what a hostile author would send."""
    payload = {
        "schema_version": "1",
        "tenant_id": "bluepeak",
        "capability_id": "transfer_between_shares",
        "base_version": "1.0.0",
        "description": "raw payload for grammar-closure tests",
        "operations": [],
    }
    payload.update(overrides)
    return payload


def log_on_target(on_ambiguous: AmbiguityPolicy = AmbiguityPolicy.FAIL) -> Target:
    return Target(
        description="the Log On button",
        ladder=[
            RoleRung(role="button", name="Log On", confidence=1.0),
            CssRung(css="input[value='Log On']", confidence=1.0),
        ],
        verify=VerifyPredicate(control="input", text_contains="Log On"),
        on_ambiguous=on_ambiguous,
    )


def verify_checkpoint() -> CheckpointStep:
    return CheckpointStep(
        id="tenant-verify-screen",
        description="Bluepeak's extra verify interstitial is shown",
        condition=StateCondition(text_visible="VERIFY TRANSFER"),
    )


def acknowledge_click() -> ActStep:
    return ActStep(
        id="tenant-acknowledge",
        action=ActionType.CLICK,
        target=Target(
            description="the Acknowledge & Continue button",
            ladder=[RoleRung(role="button", name="Acknowledge & Continue", confidence=1.0)],
            verify=VerifyPredicate(control="input", text_contains="Acknowledge"),
        ),
    )


def step_by_id(capability: Capability, step_id: str):
    return next(step for step in capability.steps if step.id == step_id)


# ----------------------------------------------------- the grammar is closed


#: Every dangerous verb a tenant admin might reach for. None of these exist:
#: the union's discriminator refuses the tag itself, so removal/weakening is
#: inexpressible rather than merely detected.
FORBIDDEN_OPS = [
    "remove_step",
    "delete_step",
    "replace_step",
    "remove_outcome",
    "weaken_outcome",
    "set_success",
    "set_safety",
    "set_allowed_actions",
    "set_require_confirmation",
    "set_limits",
    "set_inputs",
    "set_outputs",
]


@pytest.mark.parametrize("op_name", FORBIDDEN_OPS)
def test_dangerous_ops_are_structurally_inexpressible(op_name):
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate(
            overlay_dict(operations=[{"op": op_name, "step_id": "sign-in"}])
        )


@pytest.mark.parametrize(
    "extra_field",
    [
        {"remove_steps": ["post-transfer"]},
        {"safety": {"require_confirmation": False}},
        {"require_confirmation": False},
        {"limits": {"max_retries_per_step": 99}},
        {"success": {"text_visible": "anything"}},
    ],
)
def test_unknown_overlay_fields_are_refused(extra_field):
    """extra='forbid' at the top level: safety/limits/success cannot sneak in
    as fields either — there is no back door around the op vocabulary."""
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate(overlay_dict(**extra_field))


def test_unknown_op_fields_are_refused():
    op = {
        "op": "retarget_step",
        "step_id": "sign-in",
        "target": log_on_target().model_dump(mode="json"),
        "also_remove_step": "post-transfer",
    }
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate(overlay_dict(operations=[op]))


def test_tenant_id_must_be_a_slug():
    with pytest.raises(ValidationError):
        make_overlay(tenant_id="Blue Peak!")


def test_overlay_json_round_trip():
    overlay = make_overlay(
        hosts=["127.0.0.1:8010"],
        operations=[RetargetStep(step_id="sign-in", target=log_on_target())],
    )
    assert TenantOverlay.from_json(overlay.to_json()) == overlay


# -------------------------------------------------------------------- retarget


def test_retarget_swaps_an_act_step_target_and_nothing_else():
    base = make_capability()
    resolved = apply_overlay(
        base, make_overlay(operations=[RetargetStep(step_id="sign-in", target=log_on_target())])
    )

    step = step_by_id(resolved, "sign-in")
    assert step.target.verify.text_contains == "Log On"
    assert step.action is ActionType.CLICK
    assert step.risk is Risk.SAFE
    # The resolved capability keeps the base identity — versioning stays with
    # the base product, tenants do not fork it.
    assert (resolved.id, resolved.version) == (base.id, base.version)
    # The base object is never mutated: apply_overlay deep-copies.
    assert step_by_id(base, "sign-in").target.verify.text_contains == "Sign In"


def test_retarget_keeps_the_mutating_risk_of_the_confirm_step():
    """Only the target swaps; the step object (and its MUTATING classification,
    which drives the replay confirmation gate) is untouched."""
    base = make_capability()
    target = Target(
        description="the Authorize Posting button",
        ladder=[RoleRung(role="button", name="Authorize Posting", confidence=1.0)],
        verify=VerifyPredicate(control="input", text_contains="Authorize Posting"),
    )
    resolved = apply_overlay(
        base, make_overlay(operations=[RetargetStep(step_id="post-transfer", target=target)])
    )

    step = step_by_id(resolved, "post-transfer")
    assert step.risk is Risk.MUTATING
    assert step.target.verify.text_contains == "Authorize Posting"


def test_retarget_cannot_relax_ambiguity_on_a_mutating_step():
    """Revalidation re-runs every load-time refusal: a retarget that would let
    a mutating step guess between matches dies as an OverlayError."""
    guessy = log_on_target(on_ambiguous=AmbiguityPolicy.FIRST)
    with pytest.raises(OverlayError):
        apply_overlay(
            make_capability(),
            make_overlay(operations=[RetargetStep(step_id="post-transfer", target=guessy)]),
        )


def test_retargeting_a_checkpoint_is_refused():
    # "at-login" is a checkpoint in the fixture: it has no target to swap.
    with pytest.raises(OverlayError):
        apply_overlay(
            make_capability(),
            make_overlay(operations=[RetargetStep(step_id="at-login", target=log_on_target())]),
        )


def test_retargeting_an_unknown_step_is_refused():
    with pytest.raises(OverlayError):
        apply_overlay(
            make_capability(),
            make_overlay(
                operations=[RetargetStep(step_id="no-such-step", target=log_on_target())]
            ),
        )


# -------------------------------------------------------------------- insert


def test_insert_steps_after_a_named_step():
    base = make_capability()
    resolved = apply_overlay(
        base,
        make_overlay(
            operations=[
                InsertSteps(
                    after_step="continue-to-confirm",
                    steps=[verify_checkpoint(), acknowledge_click()],
                )
            ]
        ),
    )

    ids = [step.id for step in resolved.steps]
    anchor = ids.index("continue-to-confirm")
    assert ids[anchor + 1 : anchor + 3] == ["tenant-verify-screen", "tenant-acknowledge"]
    assert len(resolved.steps) == len(base.steps) + 2


def test_insert_steps_before_the_first_step():
    resolved = apply_overlay(
        make_capability(),
        make_overlay(operations=[InsertSteps(after_step=None, steps=[verify_checkpoint()])]),
    )
    assert resolved.steps[0].id == "tenant-verify-screen"


def test_insert_after_unknown_step_is_refused():
    with pytest.raises(OverlayError):
        apply_overlay(
            make_capability(),
            make_overlay(
                operations=[InsertSteps(after_step="no-such-step", steps=[verify_checkpoint()])]
            ),
        )


def test_insert_requires_at_least_one_step():
    with pytest.raises(ValidationError):
        InsertSteps(after_step=None, steps=[])


def test_inserted_duplicate_step_id_is_refused_by_revalidation():
    clone = CheckpointStep(
        id="at-login",  # collides with the fixture's own checkpoint
        description="a colliding checkpoint",
        condition=StateCondition(text_visible="Operator Sign-In"),
    )
    with pytest.raises(OverlayError, match="duplicate step id"):
        apply_overlay(
            make_capability(),
            make_overlay(operations=[InsertSteps(after_step=None, steps=[clone])]),
        )


def test_inserted_mutating_act_is_legal_and_confirmation_still_gates():
    """Added strictness AND added tenant steps are legal — a mutating insert
    is not a loophole because require_confirmation cannot be expressed in the
    grammar, so the replay gate still covers it."""
    fee_step = ActStep(
        id="tenant-post-fee",
        action=ActionType.CLICK,
        risk=Risk.MUTATING,
        target=Target(
            description="the Post Fee button",
            ladder=[RoleRung(role="button", name="Post Fee", confidence=1.0)],
            verify=VerifyPredicate(control="input", text_contains="Post Fee"),
        ),
    )
    resolved = apply_overlay(
        make_capability(),
        make_overlay(operations=[InsertSteps(after_step="sign-in", steps=[fee_step])]),
    )

    assert step_by_id(resolved, "tenant-post-fee").risk is Risk.MUTATING
    assert resolved.safety.require_confirmation is True


def test_operations_apply_in_order():
    """An op may build on an earlier op's result; reversed, the reference does
    not exist yet and the apply is refused — order is semantics, not styling."""
    ops = [
        InsertSteps(after_step="sign-in", steps=[acknowledge_click()]),
        RetargetStep(step_id="tenant-acknowledge", target=log_on_target()),
    ]
    resolved = apply_overlay(make_capability(), make_overlay(operations=list(ops)))
    assert step_by_id(resolved, "tenant-acknowledge").target.verify.text_contains == "Log On"

    with pytest.raises(OverlayError):
        apply_overlay(make_capability(), make_overlay(operations=list(reversed(ops))))


# ------------------------------------------------------------------- outcomes


def test_add_outcome_appends_to_the_catalogue():
    base = make_capability()
    outcome = DeclaredOutcome(
        id="bluepeak_daily_limit",
        code=Code.OPERATION_REFUSED,
        disposition=Tier.BUSINESS_OUTCOME,
        message="Bluepeak refuses transfers above the tenant's daily limit.",
        detect=StateCondition(text_visible="DAILY LIMIT EXCEEDED"),
    )
    resolved = apply_overlay(base, make_overlay(operations=[AddOutcome(outcome=outcome)]))

    assert len(resolved.outcomes) == len(base.outcomes) + 1
    assert resolved.outcomes[-1].id == "bluepeak_daily_limit"


def test_add_outcome_cannot_relabel_a_tier():
    """The fleet taxonomy holds through the overlay path too: DeclaredOutcome
    itself refuses a permission denial dressed up as a polite business no."""
    relabelled = {
        "op": "add_outcome",
        "outcome": {
            "id": "quiet_lockout",
            "code": "permission_denied",
            "disposition": "business_outcome",
            "message": "pretend the lockout is an answer, not a fault",
            "detect": {"text_visible": "NOT AUTHORIZED"},
        },
    }
    with pytest.raises(ValidationError, match="fleet-classified"):
        TenantOverlay.model_validate(overlay_dict(operations=[relabelled]))


# ------------------------------------------------------------- entry / hosts


def test_set_entry_changes_only_the_entry_path():
    base = make_capability()
    resolved = apply_overlay(base, make_overlay(operations=[SetEntry(entry="/signon")]))
    assert resolved.entry == "/signon"
    assert base.entry == "/login"


def test_hosts_replacement_changes_only_allowed_hosts():
    """The one nuanced widening: hosts re-bind WHERE the tenant instance lives.
    WHAT automation may do there (actions, confirmation) must be bytewise the
    base's — and the operator's deployment policy still gates every host at
    the CLI intersection."""
    base = make_capability()
    resolved = apply_overlay(
        base, make_overlay(hosts=["127.0.0.1:8010", "localhost:8010"])
    )

    assert resolved.safety.allowed_hosts == ["127.0.0.1:8010", "localhost:8010"]
    assert resolved.safety.allowed_actions == base.safety.allowed_actions
    assert resolved.safety.require_confirmation is base.safety.require_confirmation is True


# ------------------------------------------------------------------ refusals


def test_capability_id_mismatch_is_refused():
    with pytest.raises(OverlayError):
        apply_overlay(make_capability(), make_overlay(capability_id="close_share_account"))


def test_base_version_mismatch_is_refused():
    """An overlay is authored against ONE base version; silently applying it
    to another would replay unreviewed combinations."""
    with pytest.raises(OverlayError):
        apply_overlay(make_capability(), make_overlay(base_version="1.0.1"))


def test_provenance_carries_the_overlay_note():
    resolved = apply_overlay(make_capability(), make_overlay())
    assert "overlay bluepeak applied to base 1.0.0" in (resolved.provenance.notes or "")


# ------------------------------------------------------------------- store


def test_store_saves_loads_and_lists_overlays(tmp_path):
    store = CapabilityStore(tmp_path)
    overlay = make_overlay(
        operations=[RetargetStep(step_id="sign-in", target=log_on_target())]
    )

    path = store.save_overlay(overlay)
    assert path == tmp_path / "transfer_between_shares" / "overlays" / "bluepeak.json"
    assert store.load_overlay("transfer_between_shares", "bluepeak") == overlay
    assert store.list_overlays("transfer_between_shares") == ["bluepeak"]
    assert store.list_overlays("some_other_capability") == []


def test_store_missing_overlay_names_the_known_tenants(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save_overlay(make_overlay())
    with pytest.raises(FileNotFoundError, match="bluepeak"):
        store.load_overlay("transfer_between_shares", "ridgeline_east")


# ------------------------------------------- load_resolved: the pin as default


def _store_with_two_bases_and_a_pin(tmp_path) -> CapabilityStore:
    """v1.0.0 and a newer v1.1.0 recording, overlay pinned to 1.0.0 — the
    exact situation a tester creates by running discovery after cloning."""
    store = CapabilityStore(tmp_path)
    store.save(make_capability())
    store.save(make_capability(version="1.1.0"))
    store.save_overlay(
        make_overlay(operations=[RetargetStep(step_id="sign-in", target=log_on_target())])
    )
    return store


def test_tenant_without_version_replays_the_overlays_pinned_base(tmp_path):
    """A fresh recording must NOT strand every tenant command on a version
    error: with no explicit version, the pin selects the reviewed base — and
    the caller is told the overlay is due a re-review."""
    store = _store_with_two_bases_and_a_pin(tmp_path)

    resolved, info = store.load_resolved("transfer_between_shares", None, "bluepeak")

    assert resolved.version == "1.0.0"
    # The overlay really applied, not just the base load:
    assert step_by_id(resolved, "sign-in").target.ladder[0].name == "Log On"
    assert info is not None and "1.0.0" in info and "1.1.0" in info


def test_no_tenant_still_means_latest(tmp_path):
    store = _store_with_two_bases_and_a_pin(tmp_path)
    resolved, info = store.load_resolved("transfer_between_shares")
    assert resolved.version == "1.1.0"
    assert info is None


def test_explicit_version_conflicting_with_the_pin_still_refuses(tmp_path):
    """The safety guard survives the convenience: an EXPLICIT version that
    contradicts the pin is a conflicting instruction, not a default."""
    store = _store_with_two_bases_and_a_pin(tmp_path)
    with pytest.raises(OverlayError, match="re-review"):
        store.load_resolved("transfer_between_shares", "1.1.0", "bluepeak")


def test_pin_matching_latest_is_silent(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(make_capability())
    store.save_overlay(make_overlay())
    resolved, info = store.load_resolved("transfer_between_shares", None, "bluepeak")
    assert resolved.version == "1.0.0"
    assert info is None


# ----------------------------------------------- the REAL bluepeak overlay


def load_real_base() -> Capability:
    return Capability.from_json(ARTIFACT.read_text(encoding="utf-8"))


def load_repo_overlay() -> TenantOverlay:
    return TenantOverlay.from_json(REPO_OVERLAY.read_text(encoding="utf-8"))


def test_repo_bluepeak_overlay_applies_cleanly_to_the_real_artifact():
    """The shipped deliverable: base recorded once against ridgeline, bluepeak
    patched with exactly the three differences that exist."""
    base = load_real_base()
    overlay = load_repo_overlay()
    assert overlay.tenant_id == "bluepeak"
    assert overlay.capability_id == base.id
    assert overlay.base_version == base.version

    resolved = apply_overlay(base, overlay)

    # Identity and provenance: still the base capability, with an audit trail.
    assert (resolved.id, resolved.version) == (base.id, base.version)
    assert "overlay bluepeak applied to base 1.0.0" in (resolved.provenance.notes or "")

    # Hosts re-bound; everything else in the safety envelope bytewise the base.
    assert resolved.safety.allowed_hosts == ["127.0.0.1:8010", "localhost:8010"]
    assert resolved.safety.allowed_actions == base.safety.allowed_actions
    assert resolved.safety.require_confirmation is True

    # The retargeted sign-in now pins the bluepeak caption.
    sign_in = step_by_id(resolved, REAL_SIGN_IN)
    assert sign_in.target.verify.text_contains == "Log On"
    assert sign_in.target.ladder[0].strategy == "role"
    assert any(
        rung.strategy == "css" and "Log On" in rung.css
        for rung in sign_in.target.ladder
    ), "spec pins a css input[value='Log On'] fallback rung"

    # The retargeted confirm keeps its risk semantics; only the target swapped.
    confirm = step_by_id(resolved, REAL_CONFIRM)
    assert confirm.target.verify.text_contains == "Authorize Posting"
    assert confirm.risk is Risk.MUTATING

    # The extra verify screen: checkpoint + acknowledge click, directly after
    # the continue click, in that order.
    ids = [step.id for step in resolved.steps]
    anchor = ids.index(REAL_CONTINUE)
    inserted_checkpoint, inserted_click = resolved.steps[anchor + 1], resolved.steps[anchor + 2]
    assert isinstance(inserted_checkpoint, CheckpointStep)
    assert "VERIFY TRANSFER" in (inserted_checkpoint.condition.text_visible or "")
    assert isinstance(inserted_click, ActStep)
    assert inserted_click.action is ActionType.CLICK
    assert inserted_click.risk is Risk.SAFE  # acknowledging commits nothing
    assert len(resolved.steps) == len(base.steps) + 2


def test_repo_bluepeak_overlay_rungs_are_reviewable():
    """Hand-authored overlays are reviewable artifacts too: their rungs say
    they were authored (confidence 1.0, an explicit note), not probed."""
    overlay = load_repo_overlay()
    targets = []
    for op in overlay.operations:
        if isinstance(op, RetargetStep):
            targets.append(op.target)
        elif isinstance(op, InsertSteps):
            targets.extend(
                step.target
                for step in op.steps
                if isinstance(step, ActStep) and step.target is not None
            )
    assert targets, "the bluepeak overlay must author at least one target"
    for target in targets:
        for rung in target.ladder:
            assert rung.confidence == 1.0, (target.description, rung)
            assert "authored for tenant bluepeak" in (rung.note or ""), (
                target.description,
                rung,
            )


def test_repo_store_serves_the_bluepeak_overlay():
    store = CapabilityStore(REPO_ROOT / "capabilities")
    assert "bluepeak" in store.list_overlays("transfer_between_shares")
    assert store.load_overlay("transfer_between_shares", "bluepeak").tenant_id == "bluepeak"


# --------------------------------------------- regression: review findings


def _retarget_confirm(verify):
    from tellerly.schema import RetargetStep, RoleRung, Target

    return RetargetStep(
        step_id="post-transfer",
        target=Target(
            description="the tenant's confirm button",
            ladder=[RoleRung(role="button", name="Authorize", confidence=1.0)],
            verify=verify,
        ),
    )


def test_mutating_retarget_cannot_weaken_the_verify_predicate():
    """Relabelling a posting control is the overlay's job; loosening what
    proves it is not — a weaker verify on a MUTATING step is refused."""
    from tellerly.schema import OverlayError, TenantOverlay, VerifyPredicate, apply_overlay

    base = make_capability()
    overlay = TenantOverlay(
        tenant_id="weakener",
        capability_id=base.id,
        base_version=base.version,
        description="drops the text pin from the posting click",
        operations=[_retarget_confirm(VerifyPredicate(control="input"))],
    )
    with pytest.raises(OverlayError, match="identity proof"):
        apply_overlay(base, overlay)


def test_mutating_retarget_with_equal_strength_verify_is_legal():
    from tellerly.schema import TenantOverlay, VerifyPredicate, apply_overlay

    base = make_capability()
    overlay = TenantOverlay(
        tenant_id="relabel",
        capability_id=base.id,
        base_version=base.version,
        description="tenant caption, same proof strength",
        operations=[
            _retarget_confirm(VerifyPredicate(control="input", text_contains="Authorize"))
        ],
    )
    resolved = apply_overlay(base, overlay)
    confirm = next(step for step in resolved.steps if step.id == "post-transfer")
    assert confirm.target.verify.text_contains == "Authorize"


def test_load_overlay_refuses_path_traversal(tmp_path):
    store = CapabilityStore(tmp_path)
    with pytest.raises(FileNotFoundError, match="not a valid slug"):
        store.load_overlay("transfer_between_shares", "../overlays/bluepeak")
    with pytest.raises(FileNotFoundError, match="not a valid slug"):
        store.load_overlay("..", "bluepeak")
