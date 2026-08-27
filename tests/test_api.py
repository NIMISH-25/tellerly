"""The agent-facing capability API, driven through Flask's test client.

Catalog and detail must be DERIVED from the real recorded artifact (never
hand-authored prose), and invoke must return HTTP 200 with a typed
ReplayResult body for every completed run — a business outcome or a policy
refusal is an ANSWER, not a transport error, mirroring the CLI's exit-code
philosophy. Transport errors (404/400) are reserved for "you asked about
something that does not exist" and "your body is malformed".

Server on 8778 (never 8000 — a live target may own it). No model calls.

Why the fixture serves a COPIED catalog: the API builds the
deployment∩capability PolicyGate itself, and the recorded artifact pins
``safety.allowed_hosts`` to its discovery-time host (127.0.0.1:8000). The
engine tests sidestep that by building the deployment gate directly, but the
API offers no such side door — so the store copy re-binds the artifact's
hosts to the test port, the same "WHERE the instance lives, not what
automation may do there" move the overlay grammar blesses, applied through
the ``TELLERLY_CAPABILITIES_DIR`` seam. Evidence is redirected off the repo
tree through its sibling seam for the same reason: tests must not write into
the repo's evidence/.
"""
from __future__ import annotations

import json
import shutil
import threading

import pytest
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.config import REPO_ROOT
from tellerly.kernel.guardrails import DeploymentPolicy

PORT = 8778
BASE = f"http://127.0.0.1:{PORT}"
ARTIFACT = REPO_ROOT / "capabilities" / "transfer_between_shares" / "v1.0.0.json"

#: Chaos off: API behaviour only; recovery has its own engine tests.
CALM = {"TESTING": True, "INTERSTITIAL_EVERY": 0, "SLOW_SECONDS": 0.0, "SESSION_TTL_S": 100_000}


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def server():
    """One threaded ridgeline instance for the whole module — every invoke
    opens the entry URL afresh, so tests stay independent anyway."""
    app = create_app(dict(CALM))
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


@pytest.fixture(autouse=True)
def fresh_data():
    """Seed balances/ledger per test — the happy path posts a real transfer."""
    data.reset()
    yield


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """create_api with the spec's injected deployment policy, over a copied
    catalog whose artifact hosts are re-bound to the test port (see the
    module docstring for why the copy is necessary)."""
    from tellerly.api import create_api

    store_dir = tmp_path_factory.mktemp("catalog") / "capabilities"
    shutil.copytree(REPO_ROOT / "capabilities", store_dir)
    patched = store_dir / "transfer_between_shares" / "v1.0.0.json"
    raw = json.loads(patched.read_text(encoding="utf-8"))
    raw["safety"]["allowed_hosts"] = [f"127.0.0.1:{PORT}", f"localhost:{PORT}"]
    patched.write_text(json.dumps(raw), encoding="utf-8")

    monkey = pytest.MonkeyPatch()
    monkey.setenv("TELLERLY_CAPABILITIES_DIR", str(store_dir))
    monkey.setenv("TELLERLY_EVIDENCE_DIR", str(tmp_path_factory.mktemp("evidence")))
    api = create_api(
        policy=DeploymentPolicy(
            allowed_hosts=[f"127.0.0.1:{PORT}", f"localhost:{PORT}"],
            allowed_actions=["navigate", "click", "fill", "select", "press"],
            require_confirmation=True,
        )
    )
    yield api.test_client()
    monkey.undo()


# -------------------------------------------------------------------- helpers


def artifact_raw() -> dict:
    """The recorded artifact as plain JSON — the source of truth every
    catalog/detail assertion is checked against, so the tests can never
    drift into blessing hand-authored fiction."""
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def params(**overrides: str) -> dict[str, str]:
    values = {
        "operator_id": "op-api",
        "access_key": "demo",
        "member_id": "101556",
        "from_share": "S00",
        "to_share": "S01",
        "amount": "10.00",
    }
    values.update(overrides)
    return values


def invoke(client, **body_overrides):
    """POST an invoke with the standard body; override/add keys per case."""
    body: dict = {"inputs": params(), "target": BASE}
    body.update(body_overrides)
    return client.post("/api/capabilities/transfer_between_shares/invoke", json=body)


def balances(member_id: str) -> dict[str, float]:
    return {s["share_id"]: s["balance"] for s in data.get_member(member_id)["shares"]}


# ----------------------------------------------------------- catalog (no browser)


def test_health_reports_the_catalog_size(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "capabilities": 1}


def test_catalog_is_derived_from_the_recorded_artifact(client):
    response = client.get("/api/capabilities")

    assert response.status_code == 200
    catalog = response.get_json()
    assert isinstance(catalog, list)
    item = next(entry for entry in catalog if entry["id"] == "transfer_between_shares")

    raw = artifact_raw()
    assert item["latest"] == raw["version"]
    assert raw["version"] in item["versions"]
    assert item["title"] == raw["title"]
    assert item["description"] == raw["description"]

    # The input contract travels: an agent must learn BEFORE invoking that
    # access_key is a secret (vault/env sourced, never persisted).
    assert set(item["inputs"]) == set(raw["inputs"])
    assert item["inputs"]["access_key"]["sensitivity"] == "secret"
    assert item["inputs"]["member_id"]["required"] is True
    assert item["inputs"]["member_id"]["pattern"] == raw["inputs"]["member_id"]["pattern"]
    assert set(item["outputs"]) == set(raw["outputs"])
    assert item["outputs"]["confirmation_no"]["type"] == "string"

    # The confirm click is a mutating act step, so the whole capability is
    # flagged mutating — the signal an agent needs to know approval applies.
    assert item["mutating"] is True
    assert "bluepeak" in item["tenants"]


def test_detail_adds_outcomes_features_and_a_plain_json_schema(client):
    response = client.get("/api/capabilities/transfer_between_shares")

    assert response.status_code == 200
    detail = response.get_json()
    raw = artifact_raw()
    assert detail["id"] == "transfer_between_shares"

    # Declared outcomes are part of the callable contract: the agent can
    # know "no_such_member" is a possible ANSWER before it ever invokes.
    outcomes = {outcome["id"]: outcome for outcome in detail["outcomes"]}
    assert set(outcomes) == {o["id"] for o in raw["outcomes"]}
    assert "no_such_member" in outcomes
    assert outcomes["no_such_member"]["code"] == "no_such_record"
    assert outcomes["no_such_member"]["disposition"] == "business_outcome"
    assert outcomes["no_such_member"]["message"]

    assert detail["steps"] == len(raw["steps"])
    assert detail["required_features"] == sorted(detail["required_features"])

    # A PLAIN JSON Schema — the lingua franca a tool-calling agent consumes.
    schema = detail["input_json_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert len(raw["inputs"]) == 6
    assert set(schema["required"]) == set(raw["inputs"])
    member = schema["properties"]["member_id"]
    assert member["pattern"] == raw["inputs"]["member_id"]["pattern"]
    # Sensitivity is noted in the property description, not invented fields.
    assert "secret" in schema["properties"]["access_key"]["description"].lower()


def test_unknown_capability_is_a_404_with_an_error(client):
    response = client.get("/api/capabilities/uncatalogued_capability")

    assert response.status_code == 404
    assert response.get_json()["error"]


# ------------------------------------------------------------ invoke (browser)


def test_invoke_happy_path_posts_the_transfer(client, server):
    before = balances("101556")

    response = invoke(client, approve_mutations=True)

    # HTTP 200 for a COMPLETED run: the transport succeeded, the body says
    # what the target app did.
    assert response.status_code == 200
    result = response.get_json()
    assert result["status"] == "success", result
    assert str(result["outputs"]["confirmation_no"]).startswith("TL-")
    assert result["economics"]["llm_calls"] == 0  # replay is deterministic

    # The money actually moved — the API drove the real console, it did not
    # merely echo a result shape.
    after = balances("101556")
    assert after["S00"] == pytest.approx(before["S00"] - 10.00)
    assert after["S01"] == pytest.approx(before["S01"] + 10.00)


def test_unknown_member_is_a_200_business_outcome(client, server):
    response = invoke(client, inputs=params(member_id="99999"), approve_mutations=True)

    # "No such member" is the app's ANSWER — 200, typed, never an HTTP error.
    assert response.status_code == 200
    result = response.get_json()
    assert result["status"] == "business_outcome", result
    assert result["outcome"]["code"] == "no_such_record"


def test_unapproved_mutation_is_a_typed_policy_block_not_a_transport_error(client, server):
    before = balances("101556")

    response = invoke(client)  # approve_mutations defaults to false

    # The confirmation gate is typed, not transport-level: still 200, and
    # the body names the refusal — and nothing posted.
    assert response.status_code == 200
    result = response.get_json()
    assert result["status"] == "hard_failure", result
    assert result["failure"]["code"] == "policy_blocked"
    assert balances("101556") == before, "the blocked transfer must not have posted"


# ------------------------------------------------- invoke refusals (no browser)


def test_unknown_tenant_is_a_404_naming_known_tenants(client):
    response = invoke(client, tenant="summitcrest", approve_mutations=True)

    assert response.status_code == 404
    error = response.get_json()["error"]
    assert "bluepeak" in error  # the fix is IN the refusal: the known tenants


def test_malformed_bodies_are_a_400(client):
    # inputs must be an object of strings, not a query string.
    response = invoke(client, inputs="member_id=101556")
    assert response.status_code == 400
    assert response.get_json()["error"]

    # An unknown body key is a caller bug too — refused, never ignored.
    response = invoke(client, approve="yes")
    assert response.status_code == 400
    assert response.get_json()["error"]
