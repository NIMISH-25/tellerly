# AUDIT — requirement → implementation → evidence

Traceability from the brief's core requirements (§3) to code and evidence.
Filled in as each piece lands.

| Requirement (brief §) | Implementation | Evidence |
|---|---|---|
| 3.1 Goal-driven agent loop | `src/tellerly/discovery/` (engine, planner, recorder) + `src/tellerly/surface/web.py` | `tests/test_discovery_scripted.py`, `evidence/discovery-*/` |
| 3.2 Structured artifact | `src/tellerly/schema/` (artifact, locators, bindings); `discovery/compiler.py` emits it | `tests/test_schema_artifact.py`, `tests/test_compiler.py`, `capabilities/`, REPORT §2 |
| 3.3 Deterministic replay | `src/tellerly/replay/engine.py` + `surface` `resolve()` ladder walk; no-model enforced structurally | `tests/test_replay_engine.py` (12 cases), `tests/test_replay_isolation.py` (import graph + poisoned SDK), `evidence/replay-*/`, REPORT §3 |
| 3.4 Safety & policy guardrails | `kernel/guardrails.py` (one gate, both paths; artifacts only narrow), `config/policy.yaml` | `tests/test_kernel_services.py` |
| 3.5 Evidence / observability | `kernel/evidence.py` + `kernel/redaction.py` (value-based, every structured write; screenshots captured raw — masking is a documented cut) | `tests/test_kernel_services.py`, `evidence/discovery-*/` |
| 3.6 Human-in-the-loop escalation | `kernel/operator.py` (OperatorSession + terminal console) + `kernel/control.py` (token) + the replay engine's escalation path; `tellerly replay --escalate` | `tests/test_escalation.py` (8 cases), `tests/test_control.py`, live run in `evidence/replay-*` (aborted_by_operator), REPORT §5 |
| 3.7 Heterogeneity & scale | `SurfaceFeature` + `required_features()` enforced pre-flight; tenant overlays built (`schema/overlay.py`, `replay --tenant`, bluepeak variant + committed overlay) | `tests/test_overlay.py`, `tests/test_cross_tenant.py`, cross-tenant runs in `evidence/`, REPORT §4 |
| Stretch: cross-tenant reuse | base artifact + `overlays/bluepeak.json` applied to a second tenant variant | `evidence/replay-*` (base-fails / overlay-succeeds pair) |
| Stretch: agent-facing capability interface | `src/tellerly/api.py` + `tellerly serve-api` — catalog, typed contracts, invoke-by-name | `tests/test_api.py`, API invocation run in `evidence/` |
| Realistic target with runtime error states | `target_app/` | `tests/test_target_failure_matrix.py` (all 9 matrix rows) |
| Target hostility (no test IDs, rotating ids, iframe) | `target_app/templates/` | `tests/test_target_smoke.py` |
