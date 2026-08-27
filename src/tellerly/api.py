"""The agent-facing capability API: discover callable capabilities by name,
inspect their typed contracts, invoke them with typed args.

This is the catalog-as-a-service face of the same kernel the CLI drives: every
route is DERIVED from the saved artifacts (never hand-authored), and an invoke
runs the same intersection gate, the same replay engine, and the same evidence
pipeline as ``tellerly replay`` — an AI agent calling this API gets exactly the
guarantees a human operator gets at the terminal, because it is the same code
path behind a JSON contract.

Flask because it is already a dependency (the mock target uses it); no new
framework earns its keep for four routes. This module may import
replay/kernel/schema/surface but never ``tellerly.discovery`` or a model SDK —
serving and invoking capabilities is deterministic replay territory, and the
no-model property must survive putting an HTTP face on it.
"""
from __future__ import annotations

import threading

from flask import Flask, jsonify, request

from tellerly.config import load_settings
from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
from tellerly.kernel.store import CapabilityStore
from tellerly.replay import ReplayEngine
from tellerly.schema import (
    ActStep,
    Capability,
    OverlayError,
    Risk,
    apply_overlay,
)
from tellerly.surface.web import PlaywrightWebSurface

#: One invoke at a time, process-wide. Each invoke launches its own Chromium;
#: a short serial queue is simpler and cheaper than a browser pool, and at
#: this scale simplicity IS the reliability strategy.
_INVOKE_LOCK = threading.Lock()

#: The whole invoke body vocabulary — anything else in the body is a caller
#: error worth refusing loudly rather than silently ignoring (a typo'd
#: ``aprove_mutations`` that defaulted to False would look like a policy bug).
_INVOKE_KEYS = {"inputs", "version", "tenant", "approve_mutations", "target"}


def _catalog_item(store: CapabilityStore, capability: Capability) -> dict:
    """One catalog entry, derived entirely from the stored artifact — the
    catalog can never drift from what replay would actually execute."""
    versions = store.versions(capability.id)
    return {
        "id": capability.id,
        "versions": versions,
        "latest": versions[-1],
        "title": capability.title,
        "description": capability.description,
        "inputs": {
            name: {
                "type": decl.type.value,
                "required": decl.required,
                "sensitivity": decl.sensitivity.value,
                "pattern": decl.pattern,
            }
            for name, decl in capability.inputs.items()
        },
        "outputs": {
            name: {"type": decl.type.value, "description": decl.description}
            for name, decl in capability.outputs.items()
        },
        "mutating": any(
            isinstance(step, ActStep) and step.risk is Risk.MUTATING
            for step in capability.steps
        ),
        "tenants": store.list_overlays(capability.id),
    }


def _input_json_schema(capability: Capability) -> dict:
    """A plain JSON Schema for the capability's inputs — the lingua franca an
    agent framework already knows how to validate against and prompt from,
    so callers need no tellerly-specific type system to build a request."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, decl in capability.inputs.items():
        # Sensitivity rides in the description: JSON Schema has no standard
        # keyword for it, and an agent deciding what to log or echo needs the
        # signal exactly where it reads the field's meaning.
        prop: dict = {
            "type": decl.type.value,
            "description": f"{decl.description} [sensitivity: {decl.sensitivity.value}]",
        }
        if decl.pattern is not None:
            prop["pattern"] = decl.pattern
        properties[name] = prop
        if decl.required:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def create_api(policy: DeploymentPolicy | None = None) -> Flask:
    """Build the Flask app. ``policy`` is injectable so tests (and unusual
    deployments) can pin their own envelope; None means the operator-owned
    ``config/policy.yaml`` — the same file the CLI enforces."""
    settings = load_settings()
    if policy is None:
        policy = DeploymentPolicy.from_yaml(settings.repo_root / "config" / "policy.yaml")
    store = CapabilityStore(settings.capabilities_dir)

    app = Flask("tellerly-api")

    @app.get("/api/health")
    def health():
        # A load-balancer nicety: proves the process is up AND the catalog dir
        # is readable, in one line.
        return jsonify({"status": "ok", "capabilities": len(store.list())})

    @app.get("/api/capabilities")
    def list_capabilities():
        return jsonify([_catalog_item(store, capability) for capability in store.list()])

    @app.get("/api/capabilities/<capability_id>")
    def show_capability(capability_id: str):
        try:
            capability = store.load(capability_id)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        item = _catalog_item(store, capability)
        item.update(
            {
                "outcomes": [
                    {
                        "id": outcome.id,
                        "code": outcome.code.value,
                        "disposition": outcome.disposition.value,
                        "message": outcome.message,
                    }
                    for outcome in capability.outcomes
                ],
                "steps": len(capability.steps),
                "required_features": sorted(
                    feature.value for feature in capability.required_features()
                ),
                "input_json_schema": _input_json_schema(capability),
            }
        )
        return jsonify(item)

    @app.post("/api/capabilities/<capability_id>/invoke")
    def invoke_capability(capability_id: str):
        # --- body shape: 400 is reserved for a MALFORMED request only.
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        unknown = sorted(set(body) - _INVOKE_KEYS)
        if unknown:
            return (
                jsonify(
                    {
                        "error": f"unknown body key(s): {', '.join(unknown)} "
                        f"(known: {', '.join(sorted(_INVOKE_KEYS))})"
                    }
                ),
                400,
            )
        inputs = body.get("inputs")
        if not isinstance(inputs, dict):
            return jsonify({"error": "'inputs' must be an object of input name -> value"}), 400
        version = body.get("version")
        tenant = body.get("tenant")
        approve_mutations = body.get("approve_mutations", False)
        target = body.get("target") or settings.target_base_url
        for name, value, kind in (
            ("version", version, str),
            ("tenant", tenant, str),
            ("target", target, str),
        ):
            if value is not None and not isinstance(value, kind):
                return jsonify({"error": f"'{name}' must be a string"}), 400
        if not isinstance(approve_mutations, bool):
            return jsonify({"error": "'approve_mutations' must be a boolean"}), 400

        # --- resolve the capability: unknown names are the caller addressing
        # something that does not exist — the one family of true 404s here.
        try:
            capability = store.load(capability_id, version)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        if tenant is not None:
            try:
                overlay = store.load_overlay(capability_id, tenant)
            except FileNotFoundError as exc:
                # The store's message names the known tenants — an agent can
                # self-correct without a second discovery round-trip.
                return jsonify({"error": str(exc)}), 404
            try:
                capability = apply_overlay(capability, overlay)
            except OverlayError as exc:
                return jsonify({"error": f"overlay '{tenant}' does not apply: {exc}"}), 400

        # The INTERSECTION gate, exactly as the CLI builds it: the artifact
        # can only narrow the operator-owned deployment policy, never widen
        # it, no matter who (or what) is calling over HTTP.
        gate = PolicyGate(policy, capability.safety)

        # One browser at a time (module lock): the surface owns a whole
        # Chromium, and serializing runs keeps the API's resource story as
        # auditable as its policy story.
        with _INVOKE_LOCK:
            surface = PlaywrightWebSurface(
                headless=True, step_timeout_s=capability.limits.step_timeout_s
            )
            try:
                engine = ReplayEngine(
                    surface=surface,
                    gate=gate,
                    evidence_root=settings.evidence_dir,
                    approve_mutations=approve_mutations,
                )
                result = engine.run(capability, inputs, target)
            finally:
                try:
                    surface.close()
                except Exception:
                    pass  # a dead browser must not mask the result

        # HTTP 200 for EVERY completed run — mirroring the CLI's exit-code
        # philosophy: a business outcome ("no such member") or a typed
        # failure (policy_blocked on an unapproved mutation) is an ANSWER
        # about the domain, carried in the typed result body. Transport
        # status codes describe the conversation with the API, not the fate
        # of the flow inside the target app.
        return jsonify(result.model_dump(mode="json"))

    return app
