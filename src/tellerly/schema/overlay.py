"""Tenant overlays: one capability recorded against the base product, patched
per tenant — pure data + a pure resolver, no browser, no model.

Hundreds of tenants run the same vendor product with different branding,
relabelled buttons, and occasionally an extra screen. Re-recording (and
re-reviewing) the whole capability per tenant would multiply the audit surface
by the fleet size, so an overlay patches ONLY what differs.

The grammar is deliberately CLOSED: there is no vocabulary for removing steps,
removing or weakening outcomes, changing the success condition, touching
allowed_actions or require_confirmation, or altering limits/inputs/outputs.
The dangerous tenant-local change is inexpressible, not detected — the same
design move as the missing element-id locator strategy. What an overlay CAN
say is limited to things that are safe by construction:

- retarget a step (the step object — its action, risk, value — is untouched);
- insert steps (added checkpoints are added strictness; an inserted mutating
  act still hits the replay confirmation gate like any other);
- add an outcome (DeclaredOutcome's own validators refuse tier relabelling);
- move the entry path.

The one nuanced exception: an overlay MAY carry ``hosts`` that REPLACE the
artifact's ``safety.allowed_hosts``. That is re-binding WHERE the tenant
instance lives, not what automation may do there — the operator-owned
deployment policy still gates every host at the CLI intersection, so a rogue
overlay host is refused there, not here.

The resolved capability is re-validated through ``Capability.model_validate``
so every load-time refusal (duplicate ids, ladder order, binding coherence,
action allowlist...) re-runs against the patched whole — an overlay cannot
smuggle in what a recorded artifact could not carry.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The slug/semver vocabularies are the artifact's own — one source of truth,
# so an id legal in an overlay is exactly an id legal in a capability.
from tellerly.schema.artifact import (
    _SEMVER,
    _SLUG,
    ActStep,
    Capability,
    CheckpointStep,
    DeclaredOutcome,
    Risk,
    Step,
)
from tellerly.schema.locators import Target, VerifyPredicate

OVERLAY_SCHEMA_VERSION = "1"


class OverlayError(Exception):
    """An overlay that does not apply to the base it names — a review-time
    answer, never a mid-run surprise."""


# ------------------------------------------------------------------ operations


class RetargetStep(BaseModel):
    """Swap an act/read step's target for the tenant's relabelled control.

    Only the target moves: the step's action, value, risk, and id stay the
    recorded ones, so a retarget can never turn a safe step mutating or dodge
    the confirmation gate.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["retarget_step"] = "retarget_step"
    step_id: str
    target: Target


class InsertSteps(BaseModel):
    """Insert steps after ``after_step`` (None = before the first step).

    Inserted checkpoints are added strictness — always legal. An inserted
    mutating act is legal too: replay's confirmation gate covers every
    mutating step regardless of where it came from.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["insert_steps"] = "insert_steps"
    after_step: str | None = None
    steps: list[Step] = Field(min_length=1)


class AddOutcome(BaseModel):
    """Append a declared outcome. DeclaredOutcome's own validators refuse
    tier relabelling, so a tenant can add detection but never soften it."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add_outcome"] = "add_outcome"
    outcome: DeclaredOutcome


class SetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["set_entry"] = "set_entry"
    entry: str


OverlayOp = Annotated[
    Union[RetargetStep, InsertSteps, AddOutcome, SetEntry],
    Field(discriminator="op"),
]


# --------------------------------------------------------------------- overlay


class TenantOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = OVERLAY_SCHEMA_VERSION
    tenant_id: str = Field(pattern=_SLUG)
    capability_id: str = Field(pattern=_SLUG)
    base_version: str = Field(
        pattern=_SEMVER,
        description="The exact base the overlay was authored against — a new "
        "base version means the overlay must be re-reviewed, so it is pinned.",
    )
    description: str
    hosts: list[str] | None = Field(
        default=None,
        description="REPLACES the base safety.allowed_hosts: where the tenant "
        "instance lives. The deployment policy still gates every host.",
    )
    operations: list[OverlayOp] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "TenantOverlay":
        return cls.model_validate_json(raw)


# -------------------------------------------------------------------- resolver


def _verify_strength(verify: VerifyPredicate) -> int:
    return sum(
        1 for prop in (verify.control, verify.name_attr, verify.text_contains) if prop is not None
    )


def _refuse_weaker_verify(step: ActStep, new_target: Target, tenant_id: str) -> None:
    """A retarget of a MUTATING step must not loosen its identity proof: the
    new verify asserts at least as many properties as the base's, and a base
    text pin stays a text pin (with the tenant's caption). Relabelling a
    posting control is the overlay's job; weakening what proves it is not."""
    if step.target is None:
        return
    base_verify = step.target.verify
    new_verify = new_target.verify
    if _verify_strength(new_verify) < _verify_strength(base_verify) or (
        base_verify.text_contains is not None and new_verify.text_contains is None
    ):
        raise OverlayError(
            f"overlay '{tenant_id}': retarget_step '{step.id}' is a mutating step "
            "and the new verify predicate is weaker than the base's — a tenant "
            "overlay may relabel a posting control, never loosen its identity proof"
        )


def _find_step_index(capability: Capability, step_id: str, what: str) -> int:
    for index, step in enumerate(capability.steps):
        if step.id == step_id:
            return index
    known = ", ".join(step.id for step in capability.steps)
    raise OverlayError(f"{what} references unknown step '{step_id}' (steps: {known})")


def apply_overlay(base: Capability, overlay: TenantOverlay) -> Capability:
    """Resolve base + overlay into the capability replay actually runs.

    The resolved capability keeps the base id and version: the tenant is
    provenance, not identity — telemetry across the fleet aggregates by the
    one recorded capability.
    """
    if overlay.capability_id != base.id:
        raise OverlayError(
            f"overlay '{overlay.tenant_id}' targets capability "
            f"'{overlay.capability_id}', not '{base.id}'"
        )
    if overlay.base_version != base.version:
        raise OverlayError(
            f"overlay '{overlay.tenant_id}' was authored against base "
            f"{overlay.base_version}, but this is {base.version} — re-review "
            "the overlay against the new base"
        )

    resolved = base.model_copy(deep=True)
    for operation in overlay.operations:
        if isinstance(operation, RetargetStep):
            index = _find_step_index(resolved, operation.step_id, "retarget_step")
            step = resolved.steps[index]
            if isinstance(step, CheckpointStep):
                raise OverlayError(
                    f"retarget_step '{operation.step_id}' is a checkpoint — "
                    "a checkpoint has no target; use insert_steps to add one"
                )
            if isinstance(step, ActStep) and step.risk is Risk.MUTATING:
                _refuse_weaker_verify(step, operation.target, overlay.tenant_id)
            step.target = operation.target.model_copy(deep=True)
        elif isinstance(operation, InsertSteps):
            if operation.after_step is None:
                at = 0
            else:
                at = _find_step_index(resolved, operation.after_step, "insert_steps") + 1
            resolved.steps[at:at] = [step.model_copy(deep=True) for step in operation.steps]
        elif isinstance(operation, AddOutcome):
            resolved.outcomes.append(operation.outcome.model_copy(deep=True))
        elif isinstance(operation, SetEntry):
            resolved.entry = operation.entry

    # Hosts last: re-binding WHERE the tenant lives, after every structural op.
    if overlay.hosts is not None:
        resolved.safety.allowed_hosts = list(overlay.hosts)

    note = f"overlay {overlay.tenant_id} applied to base {base.version}"
    resolved.provenance.notes = (
        f"{resolved.provenance.notes} | {note}" if resolved.provenance.notes else note
    )

    # Revalidate the WHOLE: mutation above bypassed pydantic, so every
    # load-time refusal re-runs here — a resolved capability is held to
    # exactly the standard a recorded one is.
    try:
        return Capability.model_validate(resolved.model_dump(mode="json"))
    except ValidationError as exc:
        raise OverlayError(
            f"overlay '{overlay.tenant_id}' resolves to an invalid capability: {exc}"
        ) from exc
