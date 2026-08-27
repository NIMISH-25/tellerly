"""A complete, valid capability for the Tellerly console's transfer flow.

Hand-authored reference artifact: it exercises every schema feature (ladders
that degrade, frames, bindings inside locator names, declared outcomes with
recovery, a mutating confirm step, typed outputs) and doubles as the base
each refusal test mutates. The compiler will produce artifacts of this shape
from real discovery traces.
"""
from __future__ import annotations

from tellerly.schema import (
    ActionType,
    ActStep,
    AnchorRung,
    Capability,
    CheckpointStep,
    Code,
    CssRung,
    DeclaredOutcome,
    ExtractSpec,
    FrameRef,
    InputDecl,
    InputType,
    LabelRung,
    NameRung,
    OutputDecl,
    Provenance,
    ReadStep,
    Risk,
    RoleRung,
    SafetyPolicy,
    Sensitivity,
    StateCondition,
    Target,
    TextRung,
    Tier,
    VerifyPredicate,
)

PANEL = [FrameRef(name="actionpanel", url_path="/panel")]


def _target(description, ladder, verify, frame=()):
    return Target(description=description, ladder=ladder, verify=verify, frame=list(frame))


def make_capability(**overrides) -> Capability:
    """The share-to-share transfer capability; keyword overrides for tests."""
    fields = dict(
        id="transfer_between_shares",
        version="1.0.0",
        title="Transfer between a member's shares",
        description=(
            "Sign in, open the member record, and post a share-to-share transfer "
            "through the action panel, returning the confirmation number."
        ),
        app_id="tellerly_console",
        entry="/login",
        inputs={
            "operator_id": InputDecl(description="Teller operator id."),
            "access_key": InputDecl(
                description="Operator access key.", sensitivity=Sensitivity.SECRET
            ),
            "member_id": InputDecl(
                description="Member number to operate on.", pattern=r"^\d{5,6}$"
            ),
            "from_share": InputDecl(description="Source share id, e.g. S00."),
            "to_share": InputDecl(description="Destination share id, e.g. S01."),
            "amount": InputDecl(type=InputType.NUMBER, description="Dollar amount."),
        },
        outputs={
            "confirmation_no": OutputDecl(
                description="Posting confirmation number, e.g. TL-004211."
            )
        },
        steps=[
            ActStep(id="open-login", action=ActionType.NAVIGATE, value="/login"),
            CheckpointStep(
                id="at-login",
                description="Sign-in form is shown",
                condition=StateCondition(text_visible="Operator Sign-In"),
            ),
            ActStep(
                id="enter-operator-id",
                action=ActionType.FILL,
                target=_target(
                    "the Operator ID field",
                    [
                        LabelRung(label="Operator ID:", confidence=1.0),
                        NameRung(name="opid", confidence=1.0),
                        CssRung(css="input[name=opid]", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", name_attr="opid"),
                ),
                value="{{input.operator_id}}",
            ),
            ActStep(
                id="enter-access-key",
                action=ActionType.FILL,
                target=_target(
                    "the Access Key field",
                    [
                        # No <label for=> on this control — the ladder degrades.
                        NameRung(name="opkey", confidence=1.0),
                        AnchorRung(anchor_text="Access Key:", control="input", confidence=0.9),
                        CssRung(css="input[name=opkey]", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", name_attr="opkey"),
                ),
                value="{{input.access_key}}",
            ),
            ActStep(
                id="sign-in",
                action=ActionType.CLICK,
                target=_target(
                    "the Sign In button",
                    [
                        RoleRung(role="button", name="Sign In", confidence=1.0),
                        CssRung(css="input[value='Sign In']", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", text_contains="Sign In"),
                ),
            ),
            CheckpointStep(
                id="at-search",
                description="Member search is shown",
                condition=StateCondition(text_visible="Member Search"),
            ),
            ActStep(
                id="enter-member-no",
                action=ActionType.FILL,
                target=_target(
                    "the member number search field",
                    [
                        NameRung(name="mbr_no", confidence=1.0),
                        AnchorRung(
                            anchor_text="Member No. or Last Name:",
                            control="input",
                            confidence=0.9,
                        ),
                    ],
                    VerifyPredicate(control="input", name_attr="mbr_no"),
                ),
                value="{{input.member_id}}",
            ),
            ActStep(
                id="run-search",
                action=ActionType.CLICK,
                target=_target(
                    "the Search button",
                    [
                        RoleRung(role="button", name="Search", confidence=1.0),
                        CssRung(css="input[value=Search]", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", text_contains="Search"),
                ),
            ),
            CheckpointStep(
                id="at-member-record",
                description="The member's record is open",
                condition=StateCondition(
                    url_path_matches="/member/{{input.member_id}}",
                    text_visible="Member Record",
                ),
            ),
            ActStep(
                id="pick-source-share",
                action=ActionType.SELECT,
                target=_target(
                    "the From Share dropdown in the action panel",
                    [
                        NameRung(name="src_share", confidence=1.0),
                        AnchorRung(anchor_text="From Share:", control="select", confidence=0.9),
                    ],
                    VerifyPredicate(control="select", name_attr="src_share"),
                    frame=PANEL,
                ),
                value="{{input.from_share}}",
            ),
            ActStep(
                id="pick-destination-share",
                action=ActionType.SELECT,
                target=_target(
                    "the To Share dropdown in the action panel",
                    [
                        NameRung(name="dst_share", confidence=1.0),
                        AnchorRung(anchor_text="To Share:", control="select", confidence=0.9),
                    ],
                    VerifyPredicate(control="select", name_attr="dst_share"),
                    frame=PANEL,
                ),
                value="{{input.to_share}}",
            ),
            ActStep(
                id="enter-amount",
                action=ActionType.FILL,
                target=_target(
                    "the Amount field in the action panel",
                    [
                        LabelRung(label="Amount (USD):", confidence=1.0),
                        NameRung(name="amt", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", name_attr="amt"),
                    frame=PANEL,
                ),
                value="{{input.amount}}",
            ),
            ActStep(
                id="continue-to-confirm",
                action=ActionType.CLICK,
                target=_target(
                    "the Continue button in the action panel",
                    [
                        RoleRung(role="button", name="Continue", confidence=1.0),
                        CssRung(css="input[value=Continue]", confidence=1.0),
                    ],
                    VerifyPredicate(control="input", text_contains="Continue"),
                    frame=PANEL,
                ),
            ),
            CheckpointStep(
                id="at-confirm-screen",
                description="The review screen shows the staged transfer",
                condition=StateCondition(text_visible="CONFIRM TRANSFER"),
            ),
            ActStep(
                id="post-transfer",
                action=ActionType.CLICK,
                risk=Risk.MUTATING,
                target=_target(
                    "the Confirm & Post Transfer button",
                    [
                        RoleRung(role="button", name="Confirm & Post Transfer", confidence=1.0),
                        CssRung(css="input[name=txn_ref] ~ input[type=submit]", confidence=0.8),
                    ],
                    VerifyPredicate(control="input", text_contains="Post Transfer"),
                    frame=PANEL,
                ),
            ),
            ReadStep(
                id="read-confirmation-no",
                output="confirmation_no",
                target=_target(
                    "the Confirmation No. value on the receipt",
                    [
                        AnchorRung(anchor_text="Confirmation No.", control="td", confidence=0.9),
                        CssRung(css="table tr:first-child td:last-child", confidence=0.5),
                    ],
                    VerifyPredicate(text_contains="TL-"),
                    frame=PANEL,
                ),
                extract=ExtractSpec(pattern=r"(TL-\d+)"),
            ),
        ],
        success=StateCondition(text_visible="TRANSFER POSTED"),
        outcomes=[
            DeclaredOutcome(
                id="no_such_member",
                code=Code.NO_SUCH_RECORD,
                disposition=Tier.BUSINESS_OUTCOME,
                message="No member exists with that member number.",
                detect=StateCondition(text_visible="No records found"),
            ),
            DeclaredOutcome(
                id="source_share_on_hold",
                code=Code.OPERATION_REFUSED,
                disposition=Tier.BUSINESS_OUTCOME,
                message="The source share is on administrative hold.",
                detect=StateCondition(text_visible="administrative hold"),
            ),
            DeclaredOutcome(
                id="insufficient_funds",
                code=Code.INSUFFICIENT_FUNDS,
                disposition=Tier.BUSINESS_OUTCOME,
                message="The source share lacks available funds for this amount.",
                detect=StateCondition(text_visible="Insufficient available funds"),
            ),
            DeclaredOutcome(
                id="duplicate_submission",
                code=Code.ALREADY_PROCESSED,
                disposition=Tier.BUSINESS_OUTCOME,
                message="This transfer reference was already posted.",
                detect=StateCondition(text_visible="ALREADY PROCESSED"),
            ),
            DeclaredOutcome(
                id="maintenance_interstitial",
                code=Code.INTERSTITIAL_PRESENT,
                disposition=Tier.RECOVERABLE,
                message="The scheduled-maintenance notice interrupted the flow.",
                detect=StateCondition(text_visible="SCHEDULED MAINTENANCE NOTICE"),
                recovery=[
                    ActStep(
                        id="dismiss-maintenance-notice",
                        action=ActionType.CLICK,
                        target=_target(
                            "the Continue to Console button",
                            [
                                RoleRung(
                                    role="button", name="Continue to Console", confidence=1.0
                                ),
                                CssRung(css="input[value='Continue to Console']", confidence=1.0),
                            ],
                            VerifyPredicate(control="input", text_contains="Continue"),
                        ),
                    )
                ],
            ),
            DeclaredOutcome(
                id="session_expired",
                code=Code.SESSION_EXPIRED,
                disposition=Tier.RECOVERABLE,
                message="The operator session expired mid-flow.",
                detect=StateCondition(text_visible="session has expired"),
            ),
            DeclaredOutcome(
                id="record_restricted",
                code=Code.PERMISSION_DENIED,
                disposition=Tier.HARD_FAILURE,
                message="The operator profile is not authorized for this record.",
                detect=StateCondition(text_visible="NOT AUTHORIZED"),
            ),
            DeclaredOutcome(
                id="ledger_fault",
                code=Code.APP_FAULT,
                disposition=Tier.HARD_FAILURE,
                message="The console reported an internal posting fault.",
                detect=StateCondition(text_visible="TELLERLY INTERNAL FAULT"),
            ),
        ],
        safety=SafetyPolicy(
            allowed_hosts=["127.0.0.1:8000", "localhost:8000"],
            allowed_actions=[
                ActionType.NAVIGATE,
                ActionType.CLICK,
                ActionType.FILL,
                ActionType.SELECT,
            ],
        ),
        provenance=Provenance(
            discovery_run_id="manual-reference-fixture",
            recorded_at="2026-08-27T00:00:00Z",
            model=None,
            notes="Hand-authored reference artifact; the compiler emits this shape.",
        ),
    )
    fields.update(overrides)
    return Capability(**fields)
