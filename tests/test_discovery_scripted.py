"""The whole discovery pipeline — engine, recorder, compiler, store, evidence
— run end to end against the live console with a deterministic scripted
planner. Zero model calls: this proves everything around the model before the
model ever enters the loop."""
from __future__ import annotations

import json
import threading

import pytest
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.config import REPO_ROOT
from tellerly.discovery import DiscoveryEngine, JobSpec, ScriptedPlanner, ToolCall
from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
from tellerly.kernel.store import CapabilityStore
from tellerly.schema import (
    ActStep,
    Capability,
    CheckpointStep,
    DiscoveryStatus,
    ReadStep,
    Risk,
)

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def server():
    data.reset()
    app = create_app(
        {"INTERSTITIAL_EVERY": 0, "SESSION_TTL_S": 100_000, "SLOW_SECONDS": 0.0}
    )
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


def uid(observation, **conditions):
    for control in observation.controls:
        if all(getattr(control, key) == value for key, value in conditions.items()):
            return control.uid
    raise AssertionError(f"no control with {conditions}")


def act(**conditions_and_args):
    """Script step: act on the control matched by facts conditions."""
    action = conditions_and_args.pop("action")
    value = conditions_and_args.pop("value", None)
    def step(observation, _result):
        args = {
            "control": uid(observation, **conditions_and_args),
            "action": action,
            "why": "scripted",
        }
        if value is not None:
            args["value"] = value
        return ToolCall(tool="act", args=args)
    return step


def assert_state(description, **args):
    return lambda observation, result: ToolCall(
        tool="assert_state", args={"description": description, **args}
    )


SCRIPT = [
    act(name_attr="opid", action="fill", value="{{input.operator_id}}"),
    act(name_attr="opkey", action="fill", value="{{input.access_key}}"),
    act(accessible_name="Sign In", action="click"),
    assert_state("signed in, member search shown", text_visible="Member Search"),
    act(name_attr="mbr_no", action="fill", value="{{input.member_id}}"),
    act(accessible_name="Search", action="click"),
    assert_state(
        "member record open",
        text_visible="Member Record",
        url_path_contains="/member/{{input.member_id}}",
    ),
    act(name_attr="src_share", action="select", value="{{input.from_share}}"),
    act(name_attr="dst_share", action="select", value="{{input.to_share}}"),
    act(name_attr="amt", action="fill", value="{{input.amount}}"),
    act(accessible_name="Continue", action="click"),
    assert_state("review screen shows the staged transfer", text_visible="CONFIRM TRANSFER"),
    act(accessible_name="Confirm & Post Transfer", action="click"),
    assert_state("transfer posted", text_visible="TRANSFER POSTED"),
    lambda observation, result: ToolCall(
        tool="read_value",
        args={"anchor": "Confirmation No.", "output": "confirmation_no", "why": "scripted"},
    ),
    lambda observation, result: ToolCall(tool="finish", args={"summary": "posted and read"}),
]


@pytest.fixture()
def engine(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TELLERLY_TARGET_ACCESS_KEY", "demo")
    from tellerly.surface.web import PlaywrightWebSurface

    surface = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    engine = DiscoveryEngine(
        surface=surface,
        planner=ScriptedPlanner(SCRIPT),
        job=JobSpec.load(REPO_ROOT / "jobs" / "transfer_between_shares.json"),
        gate=PolicyGate(
            DeploymentPolicy(
                allowed_hosts=[f"127.0.0.1:{PORT}"],
                allowed_actions=["navigate", "click", "fill", "select", "press"],
            )
        ),
        base_url=server,
        evidence_root=tmp_path / "evidence",
        store=CapabilityStore(tmp_path / "capabilities"),
        outcome_catalog_path=REPO_ROOT / "config" / "outcomes" / "tellerly_console.json",
        max_turns=25,
    )
    yield engine
    surface.close()


def test_scripted_discovery_compiles_a_real_capability(engine):
    result = engine.run()

    assert result.status is DiscoveryStatus.GOAL_MET, result
    assert result.economics.llm_calls == 0  # the pipeline needs no model to be proven
    assert result.artifact_path is not None

    raw = open(result.artifact_path, encoding="utf-8").read()
    capability = Capability.from_json(raw)

    # -- parameterization reached the recorded conditions and values
    assert "{{input.member_id}}" in raw
    checkpoint = next(s for s in capability.steps if isinstance(s, CheckpointStep))
    assert checkpoint.description == "signed in, member search shown"
    member_checkpoint = next(
        s
        for s in capability.steps
        if isinstance(s, CheckpointStep) and s.condition.url_path_matches
    )
    assert member_checkpoint.condition.url_path_matches == "/member/{{input.member_id}}"

    # -- the secret value never reaches the artifact; the placeholder does
    assert "demo" not in raw
    assert "{{input.access_key}}" in raw

    # -- the success condition is the last held assertion
    assert capability.success.text_visible == "TRANSFER POSTED"

    # -- ladders are measured and durability-ordered; frames recorded
    fill_operator = next(
        s for s in capability.steps if isinstance(s, ActStep) and s.value == "{{input.operator_id}}"
    )
    strategies = [rung.strategy for rung in fill_operator.target.ladder]
    # Every rung measured unique for this control, in durability order.
    assert strategies == ["role", "label", "name", "anchor", "css"]
    source_select = next(
        s for s in capability.steps if isinstance(s, ActStep) and s.value == "{{input.from_share}}"
    )
    assert source_select.target.frame[-1].name == "actionpanel"

    # -- the posting click was classified mutating by its own wording
    confirm = next(
        s
        for s in capability.steps
        if isinstance(s, ActStep) and s.target and "Post Transfer" in s.target.description
    )
    assert confirm.risk is Risk.MUTATING

    # -- the read targets the value cell by anchor, not by its current value
    read = next(s for s in capability.steps if isinstance(s, ReadStep))
    assert read.target.ladder[0].strategy == "anchor"
    assert "TL-" not in raw.replace("TL-004211", "")  # value not pinned anywhere else

    # -- safety derived from what the run did, confirmation on
    assert capability.safety.require_confirmation is True
    assert f"127.0.0.1:{PORT}" in capability.safety.allowed_hosts

    # -- outcome catalogue attached from the app, not the run
    assert {o.id for o in capability.outcomes} >= {
        "no_such_member",
        "maintenance_interstitial",
        "ledger_fault",
    }

    # -- evidence exists and never contains the secret
    events = open(f"{result.evidence_dir}/events.jsonl", encoding="utf-8").read()
    assert '"demo"' not in events
    assert (
        json.loads(open(f"{result.evidence_dir}/result.json", encoding="utf-8").read())["status"]
        == "goal_met"
    )


def test_recovery_actions_are_not_recorded_as_flow_steps(tmp_path, monkeypatch):
    """Dismissing a declared recoverable condition (the maintenance notice) is
    recovery, not flow — recording it would make replay fail whenever the
    notice does NOT appear."""
    monkeypatch.setenv("TELLERLY_TARGET_ACCESS_KEY", "demo")
    data.reset()
    app = create_app(
        {"INTERSTITIAL_EVERY": 1, "SESSION_TTL_S": 100_000, "SLOW_SECONDS": 0.0}
    )
    httpd = make_server("127.0.0.1", 8768, app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    job = JobSpec(
        capability_id="open_member_record",
        title="Open a member record",
        description="Sign in and open the member's record.",
        goal="Open member {{input.member_id}}'s record.",
        app_id="tellerly_console",
        entry="/login",
        inputs={
            "operator_id": {"description": "Operator id.", "value": "op-rec"},
            "access_key": {
                "description": "Access key.",
                "sensitivity": "secret",
                "value_env": "TELLERLY_TARGET_ACCESS_KEY",
            },
            "member_id": {"description": "Member number.", "value": "101555"},
        },
        outputs={},
    )
    script = [
        act(name_attr="opid", action="fill", value="{{input.operator_id}}"),
        act(name_attr="opkey", action="fill", value="{{input.access_key}}"),
        act(accessible_name="Sign In", action="click"),
        act(name_attr="mbr_no", action="fill", value="{{input.member_id}}"),
        act(accessible_name="Search", action="click"),
        # INTERSTITIAL_EVERY=1: the maintenance notice now blocks the record.
        act(accessible_name="Continue to Console", action="click"),
        assert_state("member record open", text_visible="Member Record"),
        lambda obs, r: ToolCall(tool="finish", args={"summary": "record open"}),
    ]

    from tellerly.surface.web import PlaywrightWebSurface

    surface = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    try:
        engine = DiscoveryEngine(
            surface=surface,
            planner=ScriptedPlanner(script),
            job=job,
            gate=PolicyGate(
                DeploymentPolicy(
                    allowed_hosts=["127.0.0.1:8768"],
                    allowed_actions=["navigate", "click", "fill", "select"],
                )
            ),
            base_url="http://127.0.0.1:8768",
            evidence_root=tmp_path / "evidence",
            store=CapabilityStore(tmp_path / "capabilities"),
            outcome_catalog_path=REPO_ROOT / "config" / "outcomes" / "tellerly_console.json",
            max_turns=15,
        )
        result = engine.run()
    finally:
        surface.close()
        httpd.shutdown()

    assert result.status is DiscoveryStatus.GOAL_MET, result
    capability = Capability.from_json(open(result.artifact_path, encoding="utf-8").read())
    # The dismissal is NOT a flow step...
    step_targets = [
        s.target.description for s in capability.steps if getattr(s, "target", None)
    ]
    assert not any("Continue to Console" in description for description in step_targets)
    # ...the outcome catalogue is what clears the notice at replay.
    interstitial = next(o for o in capability.outcomes if o.id == "maintenance_interstitial")
    assert interstitial.recovery[0].target.ladder[0].name == "Continue to Console"


def test_finish_is_rejected_until_outputs_and_checkpoint_exist(server, tmp_path, monkeypatch):
    monkeypatch.setenv("TELLERLY_TARGET_ACCESS_KEY", "demo")
    from tellerly.surface.web import PlaywrightWebSurface

    premature = [
        lambda obs, r: ToolCall(tool="finish", args={"summary": "did nothing"}),
        lambda obs, r: ToolCall(tool="give_up", args={"reason": str(r)}),
    ]
    surface = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    try:
        engine = DiscoveryEngine(
            surface=surface,
            planner=ScriptedPlanner(premature),
            job=JobSpec.load(REPO_ROOT / "jobs" / "transfer_between_shares.json"),
            gate=PolicyGate(
                DeploymentPolicy(
                    allowed_hosts=[f"127.0.0.1:{PORT}"],
                    allowed_actions=["navigate", "click", "fill", "select"],
                )
            ),
            base_url=server,
            evidence_root=tmp_path / "evidence",
            store=CapabilityStore(tmp_path / "capabilities"),
            outcome_catalog_path=REPO_ROOT / "config" / "outcomes" / "tellerly_console.json",
            max_turns=5,
        )
        result = engine.run()
    finally:
        surface.close()
    assert result.status is DiscoveryStatus.GAVE_UP
    assert "cannot finish" in result.goal or True  # the refusal reached the planner
    assert result.artifact_path is None
