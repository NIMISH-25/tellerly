"""Compiler units: parameterization (including inside locator queries),
refusals, and derived safety."""
from __future__ import annotations

import pytest

from tellerly.config import REPO_ROOT
from tellerly.discovery import CompileError, JobSpec, compile_capability, load_outcome_catalog
from tellerly.discovery.job import JobInput
from tellerly.schema import (
    ActionType,
    ActStep,
    CheckpointStep,
    Economics,
    OutputDecl,
    Provenance,
    StateCondition,
    Target,
    VerifyPredicate,
)
from tellerly.schema.locators import AnchorRung, NameRung, TextRung


def make_job(**overrides):
    fields = dict(
        capability_id="open_member_record",
        title="Open a member record",
        description="Find the member and open their record.",
        goal="Open member {{input.member_id}}'s record.",
        app_id="tellerly_console",
        entry="/search",
        inputs={
            "member_id": JobInput(description="Member number.", value="101555"),
        },
        outputs={},
    )
    fields.update(overrides)
    return JobSpec(**fields)


def target_with_member_literal():
    return Target(
        description="the member 101555 row link",
        ladder=[
            TextRung(text="101555", control="a", confidence=1.0),
            AnchorRung(anchor_text="101555", control="a", confidence=1.0),
        ],
        verify=VerifyPredicate(control="a", text_contains="101555"),
    )


def trace_steps():
    return [
        ActStep(
            id="s01-fill-member",
            action=ActionType.FILL,
            target=Target(
                description="the search field",
                ladder=[NameRung(name="mbr_no", confidence=1.0)],
                verify=VerifyPredicate(control="input", name_attr="mbr_no"),
            ),
            value="{{input.member_id}}",
        ),
        ActStep(id="s02-open-row", action=ActionType.CLICK, target=target_with_member_literal()),
        CheckpointStep(
            id="s03-checkpoint",
            description="record open",
            condition=StateCondition(url_path_matches="/member/101555"),
        ),
    ]


def provenance():
    return Provenance(
        discovery_run_id="test-run",
        recorded_at="2026-08-27T12:00:00Z",
        discovery_economics=Economics(llm_calls=3),
    )


CATALOG = load_outcome_catalog(
    REPO_ROOT / "config" / "outcomes" / "tellerly_console.json", "tellerly_console"
)


def test_literals_become_bindings_even_inside_locators():
    capability = compile_capability(
        job=make_job(),
        steps=trace_steps(),
        visited_hosts={"127.0.0.1:8000"},
        performed_actions={ActionType.FILL, ActionType.CLICK},
        outcomes=CATALOG,
        provenance=provenance(),
    )
    # The run recorded the concrete member number; the capability must not.
    row_click = capability.steps[-1]
    assert row_click.target.ladder[0].text == "{{input.member_id}}"
    assert row_click.target.ladder[1].anchor_text == "{{input.member_id}}"
    assert row_click.target.verify.text_contains == "{{input.member_id}}"
    assert capability.success.url_path_matches == "/member/{{input.member_id}}"
    # The literal survives ONLY as the input's declared example — never in
    # anything executable.
    executable = capability.model_dump_json(exclude={"inputs"})
    assert "101555" not in executable


def test_compile_refuses_a_run_with_no_held_checkpoint():
    steps = [step for step in trace_steps() if not isinstance(step, CheckpointStep)]
    with pytest.raises(CompileError, match="no checkpoint"):
        compile_capability(
            job=make_job(),
            steps=steps,
            visited_hosts={"127.0.0.1:8000"},
            performed_actions={ActionType.FILL, ActionType.CLICK},
            outcomes=CATALOG,
            provenance=provenance(),
        )


def test_safety_derives_from_the_run_and_defaults_confirmation_on():
    capability = compile_capability(
        job=make_job(),
        steps=trace_steps(),
        visited_hosts={"127.0.0.1:8000"},
        performed_actions={ActionType.FILL, ActionType.CLICK},
        outcomes=CATALOG,
        provenance=provenance(),
    )
    assert capability.safety.allowed_hosts == ["127.0.0.1:8000"]
    # Entry navigation is intrinsic — NAVIGATE joins what the run performed.
    assert ActionType.NAVIGATE in capability.safety.allowed_actions
    assert capability.safety.require_confirmation is True


def test_wrong_app_catalog_is_refused(tmp_path):
    path = tmp_path / "other_app.json"
    path.write_text('{"app_id": "other_app", "outcomes": []}', encoding="utf-8")
    with pytest.raises(CompileError, match="other_app"):
        load_outcome_catalog(path, "tellerly_console")


def test_declared_output_never_read_makes_the_capability_refuse():
    job = make_job(outputs={"balance": OutputDecl(description="Share balance.")})
    with pytest.raises(CompileError, match="never captured"):
        compile_capability(
            job=job,
            steps=trace_steps(),
            visited_hosts={"127.0.0.1:8000"},
            performed_actions={ActionType.FILL, ActionType.CLICK},
            outcomes=CATALOG,
            provenance=provenance(),
        )
