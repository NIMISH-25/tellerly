from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Iterator, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tellerly.schema import bindings
from tellerly.schema.economics import Economics
from tellerly.schema.locators import AmbiguityPolicy, SurfaceFeature, Target
from tellerly.schema.taxonomy import DEFAULT_TIER, Code, Tier

SCHEMA_VERSION = "1"

_SLUG = r"^[a-z][a-z0-9_]*$"
_STEP_ID = r"^[a-z][a-z0-9-]*$"
_SEMVER = r"^\d+\.\d+\.\d+$"


# --------------------------------------------------------------- inputs / outputs


class Sensitivity(str, Enum):
    NONE = "none"
    PII = "pii"        # redact in logs/evidence; never persist raw
    SECRET = "secret"  # supplied from a vault/env at invocation; never persisted anywhere


class InputType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: InputType = InputType.STRING
    description: str
    required: bool = True
    pattern: str | None = None       # regex the (string form of the) value must match
    example: str | None = None       # illustrative only — forbidden for pii/secret
    sensitivity: Sensitivity = Sensitivity.NONE

    @model_validator(mode="after")
    def _valid(self) -> "InputDecl":
        if self.sensitivity is not Sensitivity.NONE and self.example is not None:
            raise ValueError("pii/secret inputs must not carry an example value")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"input pattern does not compile: {exc}") from exc
        return self


class OutputType(str, Enum):
    STRING = "string"
    MONEY = "money"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"


class OutputDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: OutputType = OutputType.STRING
    description: str


# ------------------------------------------------------------------- conditions


class StateCondition(BaseModel):
    """A checkable claim about the application's current state.

    Every set field must hold. Used for checkpoints, the success condition,
    and declared-outcome detection.
    """

    model_config = ConfigDict(extra="forbid")

    url_path_matches: str | None = None   # regex over the current path; substituted
    #                                       binding values are escaped before compile
    text_visible: str | None = None       # visible somewhere on the page; bindings legal
    text_absent: str | None = None        # must NOT be visible anywhere
    element_visible: Target | None = None

    @model_validator(mode="after")
    def _valid(self) -> "StateCondition":
        if not any(
            (self.url_path_matches, self.text_visible, self.text_absent, self.element_visible)
        ):
            raise ValueError("state condition must assert at least one property")
        if self.url_path_matches is not None:
            try:
                re.compile(bindings.mask_bindings(self.url_path_matches))
            except re.error as exc:
                raise ValueError(f"url_path_matches does not compile: {exc}") from exc
        return self


# ------------------------------------------------------------------------ steps


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"


class Risk(str, Enum):
    SAFE = "safe"          # read-only or trivially reversible
    MUTATING = "mutating"  # commits state in the target app


class ActStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["act"] = "act"
    id: str = Field(pattern=_STEP_ID)
    action: ActionType
    target: Target | None = None
    value: str | None = None       # navigate: path; fill/select: value; press: key name
    risk: Risk = Risk.SAFE         # derived from what the step does, not what it's called
    timeout_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _shape(self) -> "ActStep":
        needs_target = self.action in (ActionType.CLICK, ActionType.FILL, ActionType.SELECT)
        needs_value = self.action in (
            ActionType.NAVIGATE,
            ActionType.FILL,
            ActionType.SELECT,
            ActionType.PRESS,
        )
        if needs_target and self.target is None:
            raise ValueError(f"step '{self.id}': {self.action.value} requires a target")
        if self.action is ActionType.NAVIGATE and self.target is not None:
            raise ValueError(f"step '{self.id}': navigate takes a path, not a target")
        if needs_value and self.value is None:
            raise ValueError(f"step '{self.id}': {self.action.value} requires a value")
        if self.action is ActionType.CLICK and self.value is not None:
            raise ValueError(f"step '{self.id}': click takes no value")
        if (
            self.risk is Risk.MUTATING
            and self.target is not None
            and self.target.on_ambiguous is not AmbiguityPolicy.FAIL
        ):
            raise ValueError(
                f"step '{self.id}': a mutating step must fail on ambiguity, never guess"
            )
        return self


class CheckpointStep(BaseModel):
    """An asserted waypoint: proof the flow reached the state it expected,
    rather than an assumption that the click worked."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["checkpoint"] = "checkpoint"
    id: str = Field(pattern=_STEP_ID)
    description: str
    condition: StateCondition
    timeout_s: float | None = Field(default=None, gt=0)


class ExtractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["text", "attribute"] = "text"
    attribute: str | None = None   # required iff source == "attribute"
    pattern: str | None = None     # optional regex with one capture group to narrow

    @model_validator(mode="after")
    def _shape(self) -> "ExtractSpec":
        if self.source == "attribute" and not self.attribute:
            raise ValueError("attribute extraction needs an attribute name")
        if self.source == "text" and self.attribute:
            raise ValueError("text extraction takes no attribute name")
        if self.pattern is not None:
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"extract pattern does not compile: {exc}") from exc
            if compiled.groups != 1:
                raise ValueError("extract pattern must have exactly one capture group")
        return self


class ReadStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["read"] = "read"
    id: str = Field(pattern=_STEP_ID)
    output: str                    # name of the declared output this captures
    target: Target
    extract: ExtractSpec = Field(default_factory=ExtractSpec)
    timeout_s: float | None = Field(default=None, gt=0)


Step = Annotated[Union[ActStep, CheckpointStep, ReadStep], Field(discriminator="kind")]


# --------------------------------------------------------------------- outcomes


class DeclaredOutcome(BaseModel):
    """A named condition the app can legitimately land in, declared in the
    artifact with a detection condition and a disposition.

    The disposition must agree with the fleet-wide taxonomy for its code —
    an artifact cannot relabel a failure tier (and SUCCESS is not a legal
    disposition at all, structurally).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_SLUG)
    code: Code
    disposition: Tier
    message: str                   # what the caller is told; reviewable English
    detect: StateCondition
    recovery: list[ActStep] = Field(
        default_factory=list,
        description="Bounded dismiss/clear actions; legal only for RECOVERABLE codes.",
    )

    @model_validator(mode="after")
    def _coherent(self) -> "DeclaredOutcome":
        if self.disposition is Tier.SUCCESS:
            raise ValueError(f"outcome '{self.id}': SUCCESS is not a declarable disposition")
        expected = DEFAULT_TIER[self.code]
        if self.disposition is not expected:
            raise ValueError(
                f"outcome '{self.id}': code {self.code.value} is fleet-classified as "
                f"{expected.value}; relabelling it {self.disposition.value} is refused"
            )
        if self.recovery and self.disposition is not Tier.RECOVERABLE:
            raise ValueError(
                f"outcome '{self.id}': only recoverable outcomes may carry recovery steps"
            )
        for step in self.recovery:
            if step.risk is not Risk.SAFE:
                raise ValueError(
                    f"outcome '{self.id}': recovery step '{step.id}' must be safe — "
                    "recovery never mutates"
                )
        return self


# ----------------------------------------------------------------------- safety


class SafetyPolicy(BaseModel):
    """Per-capability safety envelope, derived from what the discovery run
    actually did. At execution time it can only NARROW the deployment policy
    (intersection); it can never widen it, and confirmation can only be
    turned on by this path, never off.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(min_length=1)
    allowed_actions: list[ActionType] = Field(min_length=1)
    require_confirmation: bool = True  # ships on; a test locks this default in


class ReplayLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_timeout_s: float = Field(default=10.0, gt=0)
    max_retries_per_step: int = Field(default=2, ge=0)
    escalation_timeout_s: float = Field(default=300.0, gt=0)


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovery_run_id: str
    recorded_at: datetime
    model: str | None = None            # the planner model, e.g. "gemini-3.5-flash"
    discovery_economics: Economics | None = None
    notes: str | None = None


# ------------------------------------------------------------------- capability


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    id: str = Field(pattern=_SLUG)
    version: str = Field(pattern=_SEMVER, description="Semver; major = contract change.")
    title: str
    description: str
    app_id: str = Field(
        pattern=_SLUG,
        description="The application this capability drives; outcome catalogues are per-app.",
    )
    entry: str = Field(description="App-relative entry path; bindings legal.")
    inputs: dict[str, InputDecl] = Field(default_factory=dict)
    outputs: dict[str, OutputDecl] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    success: StateCondition = Field(
        description="The final checkpoint. A capability with no checkpoint does not load."
    )
    outcomes: list[DeclaredOutcome] = Field(default_factory=list)
    safety: SafetyPolicy
    limits: ReplayLimits = Field(default_factory=ReplayLimits)
    provenance: Provenance

    # ---------------------------------------------------------------- helpers

    def all_targets(self) -> Iterator[Target]:
        for step in self.steps:
            if isinstance(step, (ActStep, ReadStep)) and step.target is not None:
                yield step.target
            if isinstance(step, CheckpointStep) and step.condition.element_visible:
                yield step.condition.element_visible
        if self.success.element_visible:
            yield self.success.element_visible
        for outcome in self.outcomes:
            if outcome.detect.element_visible:
                yield outcome.detect.element_visible
            for recovery_step in outcome.recovery:
                if recovery_step.target is not None:
                    yield recovery_step.target

    def required_features(self) -> set[SurfaceFeature]:
        """Surface features this capability needs — computed over every ladder
        rung of every target, so fallbacks are never silently stripped."""
        features: set[SurfaceFeature] = set()
        for target in self.all_targets():
            features |= target.required_features()
        return features

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Capability":
        return cls.model_validate_json(raw)

    @classmethod
    def contract_schema(cls) -> dict:
        """JSON Schema for the artifact format — the reviewable wire contract."""
        return cls.model_json_schema()

    def _recovery_steps(self) -> Iterator[tuple[DeclaredOutcome, ActStep]]:
        for outcome in self.outcomes:
            for step in outcome.recovery:
                yield outcome, step

    def _executable_strings(self) -> Iterator[str]:
        """Strings replay actually substitutes into or matches against.

        Prose fields (titles, descriptions, messages, notes) are deliberately
        excluded: a binding mentioned in documentation is not a use.
        """
        yield self.entry
        conditions: list[StateCondition] = [self.success]
        for step in self.steps:
            if isinstance(step, ActStep) and step.value is not None:
                yield step.value
            if isinstance(step, CheckpointStep):
                conditions.append(step.condition)
        for outcome in self.outcomes:
            conditions.append(outcome.detect)
            for step in outcome.recovery:
                if step.value is not None:
                    yield step.value
        for condition in conditions:
            for text in (
                condition.url_path_matches,
                condition.text_visible,
                condition.text_absent,
            ):
                if text is not None:
                    yield text
        for target in self.all_targets():
            yield from _target_strings(target)

    # ------------------------------------------------------------- validators

    @model_validator(mode="after")
    def _coherent(self) -> "Capability":
        problems: list[str] = []

        # One id namespace: main steps AND recovery steps — telemetry and
        # intervention records reference either kind.
        all_ids = [step.id for step in self.steps] + [
            step.id for _, step in self._recovery_steps()
        ]
        for sid in sorted({sid for sid in all_ids if all_ids.count(sid) > 1}):
            problems.append(f"duplicate step id '{sid}'")

        captured: list[str] = [
            step.output for step in self.steps if isinstance(step, ReadStep)
        ]
        for name in captured:
            if name not in self.outputs:
                problems.append(f"read step captures undeclared output '{name}'")
        for name in sorted({n for n in captured if captured.count(n) > 1}):
            problems.append(f"output '{name}' captured by more than one read step")
        for name in self.outputs:
            if name not in captured:
                problems.append(f"declared output '{name}' is never captured by a read step")

        referenced: set[str] = set()
        for text in self._executable_strings():
            for fragment in bindings.malformed_bindings(text):
                problems.append(f"malformed binding {fragment!r}")
            referenced |= bindings.referenced_inputs(text)
        for name in sorted(referenced - set(self.inputs)):
            problems.append(f"binding references undeclared input '{name}'")
        for name in sorted(set(self.inputs) - referenced):
            problems.append(f"declared input '{name}' is never referenced by any binding")

        # Recovery steps run at replay too — the allowlist covers every
        # executable step, not just the main flow.
        allowed = set(self.safety.allowed_actions)
        for step in self.steps:
            if isinstance(step, ActStep) and step.action not in allowed:
                problems.append(
                    f"step '{step.id}' uses action '{step.action.value}' outside the "
                    "capability's own safety allowlist"
                )
        for outcome, step in self._recovery_steps():
            if step.action not in allowed:
                problems.append(
                    f"recovery step '{step.id}' of outcome '{outcome.id}' uses action "
                    f"'{step.action.value}' outside the capability's own safety allowlist"
                )

        outcome_ids = [outcome.id for outcome in self.outcomes]
        for oid in sorted({o for o in outcome_ids if outcome_ids.count(o) > 1}):
            problems.append(f"duplicate outcome id '{oid}'")

        if problems:
            raise ValueError("incoherent capability: " + "; ".join(problems))
        return self


def _target_strings(target: Target) -> Iterator[str]:
    for rung in target.ladder:
        for attr in ("role", "name", "label", "text", "anchor_text", "css", "control"):
            value = getattr(rung, attr, None)
            if isinstance(value, str):
                yield value
    for text in (target.verify.control, target.verify.name_attr, target.verify.text_contains):
        if text is not None:
            yield text
    for frame in target.frame:
        for text in (frame.name, frame.url_path):
            if text is not None:
                yield text
