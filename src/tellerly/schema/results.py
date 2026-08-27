"""Result contracts: what a run hands back at the caller boundary.

The engines never raise to the caller. Every run — success, refusal, or
wreck — returns one of these, with exactly the payload its status implies:

- SUCCESS           -> typed outputs
- BUSINESS_OUTCOME  -> outcome id + code + message
- HARD_FAILURE      -> step / expected / observed / evidence paths

RECOVERABLE is never a terminal status: a recoverable condition is either
cleared during the run or it becomes something else. The model refuses it.

A replay result also refuses ``economics.llm_calls != 0`` — "replay never
calls a model" is part of the contract itself, not a promise in a docstring.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tellerly.schema.economics import Economics
from tellerly.schema.escalation import InterventionRecord
from tellerly.schema.locators import LocatorStrategy
from tellerly.schema.taxonomy import DEFAULT_TIER, Code, Tier


class StepStatus(str, Enum):
    OK = "ok"
    RECOVERED = "recovered"   # a recoverable condition was cleared en route
    FAILED = "failed"
    SKIPPED = "skipped"       # human decided SKIP_STEP


class StepOutcome(BaseModel):
    """Per-step telemetry. ``resolved_via``/``rung_index`` record which ladder
    rung actually matched — the raw signal for per-tenant drift detection."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: StepStatus
    attempts: int = Field(default=1, ge=1)
    resolved_via: LocatorStrategy | None = None
    rung_index: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    note: str | None = None

    @model_validator(mode="after")
    def _telemetry_pairs(self) -> "StepOutcome":
        if (self.resolved_via is None) != (self.rung_index is None):
            raise ValueError(
                "resolved_via and rung_index travel together — both set (a target was "
                "resolved) or both unset (the step has no target)"
            )
        return self


class OutcomeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    code: Code
    message: str


class FailureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str | None
    code: Code
    expected: str
    observed: str
    evidence: list[str] = Field(default_factory=list)  # screenshot/DOM/log paths


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    capability_id: str
    capability_version: str
    status: Tier
    outputs: dict[str, str | int | float | bool] | None = None
    outcome: OutcomeReport | None = None
    failure: FailureDetail | None = None
    escalations: list[InterventionRecord] = Field(default_factory=list)
    steps: list[StepOutcome] = Field(default_factory=list)
    economics: Economics
    evidence_dir: str | None = None

    @model_validator(mode="after")
    def _contract(self) -> "ReplayResult":
        if self.status is Tier.RECOVERABLE:
            raise ValueError("RECOVERABLE is never a terminal replay status")
        if self.status is Tier.SUCCESS and self.outputs is None:
            raise ValueError("a SUCCESS result must carry outputs (empty dict if none declared)")
        if self.status is Tier.BUSINESS_OUTCOME and self.outcome is None:
            raise ValueError("a BUSINESS_OUTCOME result must carry the outcome report")
        if self.status is Tier.HARD_FAILURE and self.failure is None:
            raise ValueError("a HARD_FAILURE result must carry the failure detail")
        if self.status is not Tier.SUCCESS and self.outputs is not None:
            raise ValueError("outputs are a SUCCESS payload only")
        if self.status is not Tier.BUSINESS_OUTCOME and self.outcome is not None:
            raise ValueError("an outcome report is a BUSINESS_OUTCOME payload only")
        if self.status is not Tier.HARD_FAILURE and self.failure is not None:
            raise ValueError("failure detail is a HARD_FAILURE payload only")
        # The fleet classification holds at the caller boundary too: a fault is
        # never reported as "the app said no", and vice versa. Failure details
        # MAY carry a RECOVERABLE code — that is the retry-budget-exhausted path.
        if self.outcome is not None and DEFAULT_TIER[self.outcome.code] is not Tier.BUSINESS_OUTCOME:
            raise ValueError(
                f"outcome code '{self.outcome.code.value}' is not fleet-classified as a "
                "business outcome"
            )
        if self.failure is not None and DEFAULT_TIER[self.failure.code] is Tier.BUSINESS_OUTCOME:
            raise ValueError(
                f"failure code '{self.failure.code.value}' is fleet-classified as a "
                "business outcome — report it as one"
            )
        economics = self.economics
        if economics.llm_calls or economics.input_tokens or economics.output_tokens or economics.cost_usd:
            raise ValueError(
                "replay reports llm_calls=0 and $0.00 by contract; any model usage means "
                "the deterministic path called a model"
            )
        return self


class DiscoveryStatus(str, Enum):
    GOAL_MET = "goal_met"
    GAVE_UP = "gave_up"                  # the planner declared a dead end
    BUDGET_EXHAUSTED = "budget_exhausted"  # turn budget hit
    STUCK_REPEATING = "stuck_repeating"  # repeat guard tripped
    ESCALATED_ABORT = "escalated_abort"  # a human aborted during discovery


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    goal: str
    status: DiscoveryStatus
    artifact_path: str | None = None
    steps_taken: int = Field(default=0, ge=0)
    economics: Economics                 # discovery pays for the model; this is the bill
    evidence_dir: str | None = None

    @model_validator(mode="after")
    def _artifact_iff_goal_met(self) -> "DiscoveryResult":
        if (self.status is DiscoveryStatus.GOAL_MET) != (self.artifact_path is not None):
            raise ValueError(
                "artifact_path is set exactly when the goal was met and a capability "
                "was compiled"
            )
        return self
