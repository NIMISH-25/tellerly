"""Cost accounting attached to every run result.

The record-once / replay-many premise is an economic argument; this block is
how the system proves it. A discovery run reports what the model cost; a
replay result reports ``llm_calls=0`` and ``$0.00`` in its own contract (and
the replay result model refuses anything else).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Economics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    wall_time_s: float = Field(default=0.0, ge=0.0)
