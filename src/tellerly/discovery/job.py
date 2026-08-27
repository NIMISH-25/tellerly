"""The job spec: the declared contract a discovery run works against.

The planner is deliberately not trusted to invent the capability's contract —
inputs, outputs, and identity come from here (reviewed by a person), and the
compiler attaches them to whatever flow the run discovered.

Secret inputs never carry inline values: they resolve from the environment at
run time (``value_env``), are registered with the value-based redactor, and
reach the model only as ``{{input.name}}`` placeholders.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tellerly.schema import InputDecl, InputType, OutputDecl, Sensitivity


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    type: InputType = InputType.STRING
    sensitivity: Sensitivity = Sensitivity.NONE
    pattern: str | None = None
    value: str | None = None      # this run's concrete value
    value_env: str | None = None  # or: environment variable holding it

    @model_validator(mode="after")
    def _coherent(self) -> "JobInput":
        if (self.value is None) == (self.value_env is None):
            raise ValueError("exactly one of value / value_env must be set")
        if self.sensitivity is not Sensitivity.NONE and self.value is not None:
            raise ValueError(
                "pii/secret inputs must use value_env — sensitive values do not "
                "belong in a job file"
            )
        return self

    def resolve(self) -> str:
        if self.value is not None:
            return self.value
        resolved = os.environ.get(self.value_env or "")
        if not resolved:
            raise RuntimeError(
                f"environment variable '{self.value_env}' is not set — required by this job"
            )
        return resolved


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    title: str
    description: str
    goal: str                       # may reference {{input.*}}; rendered for the planner
    app_id: str
    entry: str
    inputs: dict[str, JobInput] = Field(default_factory=dict)
    outputs: dict[str, OutputDecl] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "JobSpec":
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def runtime_values(self) -> dict[str, str]:
        """Concrete values for this run — the executor substitutes these; the
        planner never sees the sensitive ones."""
        return {name: spec.resolve() for name, spec in self.inputs.items()}

    def input_decls(self) -> dict[str, InputDecl]:
        decls: dict[str, InputDecl] = {}
        for name, spec in self.inputs.items():
            decls[name] = InputDecl(
                type=spec.type,
                description=spec.description,
                pattern=spec.pattern,
                sensitivity=spec.sensitivity,
                example=spec.value if spec.sensitivity is Sensitivity.NONE else None,
            )
        return decls
