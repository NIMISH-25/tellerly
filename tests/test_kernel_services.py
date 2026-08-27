"""Kernel services: guardrails, value-based redaction, evidence, store."""
from __future__ import annotations

import json

import pytest

from capability_fixture import make_capability
from tellerly.kernel.evidence import RunLog
from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate, PolicyViolation
from tellerly.kernel.redaction import Redactor
from tellerly.kernel.store import CapabilityStore
from tellerly.schema import ActionType, SafetyPolicy


def make_policy(**overrides):
    fields = dict(
        allowed_hosts=["127.0.0.1:8000", "localhost:8000"],
        allowed_actions=[ActionType.NAVIGATE, ActionType.CLICK, ActionType.FILL],
        require_confirmation=True,
    )
    fields.update(overrides)
    return DeploymentPolicy(**fields)


# ------------------------------------------------------------------ guardrails


def test_gate_allows_and_refuses_hosts():
    gate = PolicyGate(make_policy())
    gate.check_url("http://127.0.0.1:8000/login")
    with pytest.raises(PolicyViolation, match="not in the policy allowlist"):
        gate.check_url("http://evil.example/login")


def test_gate_refuses_unlisted_actions():
    gate = PolicyGate(make_policy())
    gate.check_action(ActionType.CLICK)
    with pytest.raises(PolicyViolation, match="press"):
        gate.check_action(ActionType.PRESS)


def test_capability_safety_only_narrows():
    """The artifact intersects the deployment envelope — it can never widen it."""
    capability_safety = SafetyPolicy(
        allowed_hosts=["localhost:8000", "othersite:9000"],  # othersite NOT deployed
        allowed_actions=[ActionType.CLICK],                  # narrower than deployment
        require_confirmation=False,                          # cannot turn confirmation OFF
    )
    gate = PolicyGate(make_policy(), capability_safety)
    gate.check_url("http://localhost:8000/x")
    with pytest.raises(PolicyViolation):
        gate.check_url("http://othersite:9000/x")   # artifact tried to widen
    with pytest.raises(PolicyViolation):
        gate.check_url("http://127.0.0.1:8000/x")   # narrowed away by the artifact
    with pytest.raises(PolicyViolation):
        gate.check_action(ActionType.FILL)          # narrowed away by the artifact
    assert gate.require_confirmation is True        # OR-combined: stays on


def test_deployment_policy_loads_from_repo_config():
    from tellerly.config import REPO_ROOT

    policy = DeploymentPolicy.from_yaml(REPO_ROOT / "config" / "policy.yaml")
    assert policy.require_confirmation is True


# ------------------------------------------------------------------- redaction


def test_redaction_is_value_based_not_key_based():
    redactor = Redactor()
    redactor.register("access_key", "s3cr3t-key")
    # The same value caught in a URL, a form dump, and an error string.
    assert redactor.redact("GET /login?opkey=s3cr3t-key") == "GET /login?opkey=[REDACTED:access_key]"
    assert redactor.redact("form: {'opkey': 's3cr3t-key'}") == "form: {'opkey': '[REDACTED:access_key]'}"
    assert "s3cr3t-key" not in redactor.redact("boom: value s3cr3t-key rejected")


def test_redaction_walks_structures():
    redactor = Redactor()
    redactor.register("pin", "9911")
    redacted = redactor.redact_object({"a": ["x 9911 y", {"b": "9911"}], "n": 5})
    assert redacted == {"a": ["x [REDACTED:pin] y", {"b": "[REDACTED:pin]"}], "n": 5}


def test_too_short_sensitive_values_are_refused_loudly():
    """A silently unprotected secret is worse than a failed run."""
    redactor = Redactor()
    with pytest.raises(ValueError, match="cannot be redacted safely"):
        redactor.register("pin", "911")


def test_redaction_covers_dict_keys():
    redactor = Redactor()
    redactor.register("token", "hunter22")
    assert redactor.redact_object({"hunter22": "x"}) == {"[REDACTED:token]": "x"}


def test_redaction_catches_values_inside_arbitrary_objects_on_disk(tmp_path):
    """Serialize-first: a secret riding inside an exception must not reach disk."""
    redactor = Redactor()
    redactor.register("access_key", "hunter22")
    log = RunLog(tmp_path, "run-y", redactor)
    log.event("boom", error=RuntimeError("refused value hunter22"))
    events = (tmp_path / "run-y" / "events.jsonl").read_text(encoding="utf-8")
    assert "hunter22" not in events


def test_gate_normalizes_host_case_and_userinfo():
    gate = PolicyGate(make_policy())
    gate.check_url("http://LOCALHOST:8000/x")
    gate.check_url("http://user:pw@localhost:8000/x")
    with pytest.raises(PolicyViolation):
        gate.check_url("http://localhost:8000.evil.example/x")


def test_store_survives_a_stray_file(tmp_path):
    store = CapabilityStore(tmp_path)
    capability = make_capability()
    store.save(capability)
    (tmp_path / capability.id / "v1.0.0 - Copy.json").write_text("{}", encoding="utf-8")
    assert store.versions(capability.id) == ["1.0.0"]
    assert store.load(capability.id).version == "1.0.0"


# -------------------------------------------------------------------- evidence


def test_run_log_redacts_every_write(tmp_path):
    redactor = Redactor()
    redactor.register("access_key", "hunter22")
    log = RunLog(tmp_path, "run-x", redactor)
    log.event("typed", value="hunter22", detail={"echo": "saw hunter22 here"})
    log.write_json("doc.json", {"leak": "hunter22"})

    events = (tmp_path / "run-x" / "events.jsonl").read_text(encoding="utf-8")
    doc = (tmp_path / "run-x" / "doc.json").read_text(encoding="utf-8")
    assert "hunter22" not in events and "hunter22" not in doc
    assert "[REDACTED:access_key]" in events


# ----------------------------------------------------------------------- store


def test_store_roundtrip_and_versions(tmp_path):
    store = CapabilityStore(tmp_path)
    capability = make_capability()
    path = store.save(capability)
    assert path.name == "v1.0.0.json"
    assert store.versions(capability.id) == ["1.0.0"]

    newer = make_capability(version="1.1.0")
    store.save(newer)
    assert store.versions(capability.id) == ["1.0.0", "1.1.0"]
    assert store.load(capability.id).version == "1.1.0"          # latest by default
    assert store.load(capability.id, "1.0.0").version == "1.0.0"
    assert [c.id for c in store.list()] == [capability.id]


def test_store_loads_an_artifact_a_windows_editor_saved_with_a_bom(tmp_path):
    store = CapabilityStore(tmp_path)
    path = store.save(make_capability())
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert store.load("transfer_between_shares").version == "1.0.0"


def test_store_missing_capability_says_what_exists(tmp_path):
    store = CapabilityStore(tmp_path)
    store.save(make_capability())
    with pytest.raises(FileNotFoundError, match="transfer_between_shares"):
        store.load("nonexistent")


def test_saved_artifact_json_is_reviewable(tmp_path):
    store = CapabilityStore(tmp_path)
    path = store.save(make_capability())
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "1"
    assert raw["safety"]["require_confirmation"] is True
