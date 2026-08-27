"""The compiler: a trace is one run with one set of values; a capability is
the general job. This step is why the transcript is not the product.

Four things the planner is deliberately not trusted to do happen here:

1. Locators were chosen by measurement (recorder), not by the model — the
   compiler only carries them over.
2. Parameterization: every literal input value is rewritten to its
   ``{{input.name}}`` binding — including inside locator queries and
   checkpoint conditions. (Action values arrive as placeholders already, by
   construction; this pass catches values the page echoed back, e.g. a
   member number inside a URL or a row link's text.)
3. Contracts are attached from the job spec (inputs/outputs) and the per-APP
   outcome catalogue — every capability on an app answers identically.
4. Safety is derived from what the run actually did: the hosts it visited
   and the actions it performed, with confirmation required by default.

A run that recorded no checkpoint that held is refused outright.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from tellerly.discovery.job import JobSpec
from tellerly.schema import (
    ActionType,
    ActStep,
    Capability,
    CheckpointStep,
    DeclaredOutcome,
    Provenance,
    ReadStep,
    SafetyPolicy,
    Sensitivity,
    StateCondition,
    Target,
)
from tellerly.schema.artifact import Step

_MIN_LITERAL_LENGTH = 4  # shorter values would false-match unrelated text


class CompileError(Exception):
    pass


def load_outcome_catalog(path: Path, app_id: str) -> list[DeclaredOutcome]:
    if not path.is_file():
        raise CompileError(f"no outcome catalogue for app '{app_id}' at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("app_id") != app_id:
        raise CompileError(
            f"outcome catalogue at {path} is for '{data.get('app_id')}', not '{app_id}'"
        )
    return [DeclaredOutcome.model_validate(outcome) for outcome in data["outcomes"]]


def compile_capability(
    job: JobSpec,
    steps: list[Step],
    visited_hosts: set[str],
    performed_actions: set[ActionType],
    outcomes: list[DeclaredOutcome],
    provenance: Provenance,
    version: str = "1.0.0",
) -> Capability:
    checkpoints = [step for step in steps if isinstance(step, CheckpointStep)]
    if not checkpoints:
        raise CompileError("the run recorded no checkpoint that held — nothing proves success")

    # The final held checkpoint IS the success condition; it leaves the step list.
    success_checkpoint = checkpoints[-1]
    kept = [step for step in steps if step is not success_checkpoint]

    replacements = _replacements(job)
    kept = [_parameterize_step(step, replacements) for step in kept]
    success = _parameterize_condition(success_checkpoint.condition, replacements)

    try:
        return Capability(
            id=job.capability_id,
            version=version,
            title=job.title,
            description=job.description,
            app_id=job.app_id,
            entry=job.entry,
            inputs=job.input_decls(),
            outputs=dict(job.outputs),
            steps=kept,
            success=success,
            outcomes=outcomes,
            safety=SafetyPolicy(
                allowed_hosts=sorted(visited_hosts),
                # Entry navigation is intrinsic to every replay, so NAVIGATE is
                # always in the envelope even if no recorded step navigates.
                allowed_actions=sorted(
                    performed_actions | {ActionType.NAVIGATE}, key=lambda a: a.value
                ),
                require_confirmation=True,
            ),
            provenance=provenance,
        )
    except ValidationError as exc:
        raise CompileError(f"compiled capability is incoherent: {exc}") from exc


# ------------------------------------------------------------ parameterizing


def _replacements(job: JobSpec) -> list[tuple[re.Pattern[str], str]]:
    """Literal -> binding rewrites for non-secret inputs, longest first.

    Secret values are never rewritten (they must never have been on a page or
    in a locator to begin with; redaction catches leaks in evidence).
    """
    pairs: list[tuple[str, str]] = []
    for name, spec in job.inputs.items():
        if spec.sensitivity is not Sensitivity.NONE:
            continue
        value = spec.resolve()
        if len(value) >= _MIN_LITERAL_LENGTH:
            pairs.append((value, f"{{{{input.{name}}}}}"))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return [
        (re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])"), binding)
        for value, binding in pairs
    ]


def _bind(text: str | None, replacements) -> str | None:
    if text is None:
        return None
    for pattern, binding in replacements:
        text = pattern.sub(binding, text)
    return text


def _parameterize_target(target: Target, replacements) -> Target:
    target = target.model_copy(deep=True)
    target.description = _bind(target.description, replacements)
    for frame_ref in target.frame:
        # Frame document paths carry run literals too (/member/101555/panel).
        frame_ref.url_path = _bind(frame_ref.url_path, replacements)
        frame_ref.name = _bind(frame_ref.name, replacements)
    for rung in target.ladder:
        for attr in ("name", "label", "text", "anchor_text", "css"):
            value = getattr(rung, attr, None)
            if isinstance(value, str):
                setattr(rung, attr, _bind(value, replacements))
    if target.verify.text_contains:
        target.verify.text_contains = _bind(target.verify.text_contains, replacements)
    return target


def _parameterize_condition(condition: StateCondition, replacements) -> StateCondition:
    condition = condition.model_copy(deep=True)
    condition.url_path_matches = _bind(condition.url_path_matches, replacements)
    condition.text_visible = _bind(condition.text_visible, replacements)
    condition.text_absent = _bind(condition.text_absent, replacements)
    if condition.element_visible is not None:
        condition.element_visible = _parameterize_target(
            condition.element_visible, replacements
        )
    return condition


def _parameterize_step(step: Step, replacements) -> Step:
    if isinstance(step, ActStep):
        step = step.model_copy(deep=True)
        step.value = _bind(step.value, replacements)
        if step.target is not None:
            step.target = _parameterize_target(step.target, replacements)
        return step
    if isinstance(step, ReadStep):
        step = step.model_copy(deep=True)
        step.target = _parameterize_target(step.target, replacements)
        return step
    if isinstance(step, CheckpointStep):
        step = step.model_copy(deep=True)
        step.description = _bind(step.description, replacements)
        step.condition = _parameterize_condition(step.condition, replacements)
        return step
    return step
