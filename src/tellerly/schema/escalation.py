"""Intervention data contracts: what a human operator is handed, what they
decided, and what they did — preserved as evidence across the handoff.

The state machine that moves control lives in ``tellerly.kernel.control``;
these are the records it produces.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from tellerly.schema.taxonomy import Code


class ResumeDecision(str, Enum):
    """Resume is a decision, not a signal. After a human touches the page the
    engine cannot infer which of these is correct — the human says so."""

    CONTINUE = "continue"        # the human completed the blocked step; go on from the next
    RETRY_STEP = "retry_step"    # the human cleared the obstacle; run the same step again
    SKIP_STEP = "skip_step"      # the step is moot; go on without running it
    ABORT = "abort"              # stop the run


class InterventionRequest(BaseModel):
    """What someone who was NOT watching needs in order to act."""

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    capability_id: str
    step_id: str | None            # None: blocked outside any step (e.g. entry)
    url: str
    reason_code: Code
    message: str                   # why automation stopped, in reviewable English
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None
    requested_at: datetime
    deadline_at: datetime          # past this, the run aborts loudly rather than
    #                                holding a live banking session open


class HumanAction(BaseModel):
    """One thing the human did while in control — captured because they drive
    through the same Surface as automation, landing in the same audit trail."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    description: str


class InterventionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: InterventionRequest
    actions: list[HumanAction] = Field(default_factory=list)
    decision: ResumeDecision
    resolved_at: datetime
    operator: str | None = None
