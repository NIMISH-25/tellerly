# REPORT

## Architecture

One process, one CLI (`tellerly discover | replay | capabilities | start-app`),
five packages under `src/tellerly/`:

- **`schema`** — the capability artifact, result contracts, taxonomy,
  escalation records. Pure data; imports no browser and no model SDK.
- **`surface`** — the perceive/act seam (`surface/base.py`); one
  implementation today, Playwright over CDP (`surface/web.py`).
- **`discovery`** — the LLM planner loop, the locator recorder, and the
  trace→capability compiler. The only package allowed to import the Gemini SDK.
- **`replay`** — the deterministic executor.
- **`kernel`** — services both paths share: policy gate, value-based
  redaction, evidence log, capability store, the session-control token.

The discovery/replay split is **structural, not stylistic**:
`tests/test_replay_isolation.py` imports the whole replay stack in a clean
subprocess and fails if a model SDK, a *bare HTTP client*, or
`tellerly.discovery` shows up in `sys.modules` — then runs a full replay with
the SDK modules replaced by poison objects that fail on any attribute access.
Replay does not merely avoid the model; it cannot reach one.

**Mock target — own the failure matrix.** `target_app/` is a deliberately
hostile fake credit-union console: server-rendered tables, no test IDs,
element ids that rotate per render, the transfer form inside an iframe, and
every runtime condition reachable on demand (refusals, 403/500 pages,
interstitials, slow loads, session expiry, duplicate submits).
`tests/test_target_failure_matrix.py` exercises all nine rows. A real vendor
sandbox would give none of that control over error states.

Three design rules carry the trust story (details in the next section):
the **planner never sees selectors** — it sees controls as human facts behind
ephemeral uids and enters values as `{{input.name}}` placeholders, so
artifacts are parameterized and secret-free *by construction*;
**probe-and-record** — locator ladders are measured against the live page at
the moment an action succeeds, never guessed by the model; and the
**compiler is the trust boundary** (`discovery/compiler.py`) — it rewrites
echoed literals to bindings (inside locators and URLs too), attaches the
job's typed contract and the per-app outcome catalogue, derives the safety
envelope from what the run actually did, and refuses a run with no held
checkpoint.

**The agent-facing surface** (stretch goal, built): `tellerly serve-api`
exposes the catalog over HTTP (`src/tellerly/api.py`) — capabilities derived
from the artifacts on disk, each with its typed contract and a JSON Schema
for the inputs, invocable by name via
`POST /api/capabilities/<id>/invoke`. A completed run always returns HTTP
200 carrying the typed `ReplayResult` — a business outcome is an answer, not
a transport error — and mutating capabilities require explicit
`approve_mutations` in the request (`tests/test_api.py`; a live invocation
is indexed in the evidence).

**Measured economics** (committed under `evidence/`; see `evidence/README.md`
for the run index):

| | model calls | tokens in / out | cost | wall time |
|---|---|---|---|---|
| Discovery (gemini-3.5-flash-lite) | 15 | 68,116 / 651 | $0.1080 list ($0.00 free tier) | 81.5 s |
| Replay | 0 | 0 / 0 | $0.00 | 3.3 s, deterministic |

A model-in-the-loop execution costs roughly the discovery price on **every**
invocation, so the artifact pays for itself at the **first** replay; what
moves the breakeven is UI churn (re-record cadence), not invocation volume.

## Artifact schema

A capability (`tellerly.schema.artifact.Capability`) is a contract, not a
step list: typed inputs with sensitivity levels (none/pii/secret — sensitive
inputs may not even carry example values), typed outputs with extraction
specs, the ordered steps, declared business outcomes, a safety block, and a
required success checkpoint. It is semver-versioned, serializes to JSON, and
exports its own JSON Schema for review. The module imports no browser and no
model SDK: it describes intent and identification, so a different surface
(accessibility tree, screenshot+coordinates, desktop) can execute the same
artifact.

**Targeting is a ranked ladder, never one selector**: role+accessible-name →
label → form `name` attribute → visible text → anchored (nth control after
anchor text, for label-less table layouts) → CSS last, with a validator
refusing out-of-order ladders. The ordering is durability: role+name is what
a human sees and survives markup churn; label is visible semantics but legacy
tables often have no `<label>` element. The `name` attribute ranks third **on
purpose**: in a server-rendered app it is the server's own contract on submit
— it cannot churn without breaking the vendor's backend, and it is invisible
to users so nobody rebrands it. Visible text is real but copy-editable;
anchor is positional but anchored to semantics; CSS is structure-coupled,
hence last. **No element-id strategy exists at all** — our target rotates ids
per render, as legacy apps do, so the mistake is unrepresentable rather than
discouraged (a validator also bans `#id` inside the CSS escape hatch). Ladder
confidences are **measured, not guessed**: at the moment
a discovery action succeeds, every plausible locator is built and probed
against the live page; only locators matching exactly one element survive.
Every target also carries a cheap `verify` predicate checked before acting
(a degraded rung must not silently match a similar control), and
`on_ambiguous` defaults to fail — acting on the wrong row in a banking screen
is worse than not acting.

Substitution is a **closed grammar**: only `{{input.name}}`, resolved by
single-pass dict lookup, never eval. Bindings are legal inside locator
queries, not just action values — recording a row link as text `"101555"`
would pin the capability to one member. Load-time validators refuse an
incoherent artifact outright: undeclared or malformed bindings, unused
inputs, out-of-order ladders, dangling step/output references, an outcome
relabelling its fleet-classified tier, a mutating step that tolerates
ambiguity, and (structurally) a capability with no checkpoint.

## Determinism & error handling

Determinism is the replay engine executing only what the artifact declares —
resolve via the ladder, verify, act, assert checkpoints — with no model
anywhere. "No model" is enforced three ways: an import-graph test over
`replay`/`schema`/`surface`/`kernel` that forbids model SDKs *and bare HTTP
clients* (an SDK ban alone is defeated by posting to the endpoint by hand),
a stubbed-SDK replay test, and the result contract itself refusing
`economics.llm_calls != 0`.

Runtime conditions live in a four-tier taxonomy
(`tellerly.schema.taxonomy`), with codes derived from the target's failure
matrix, not invented: **SUCCESS**, **BUSINESS_OUTCOME** (the app ran fine and
said no — "no such member" is a typed return value carrying outcome id, code,
and message), **RECOVERABLE** (slow load, known interstitial, expired
session), **HARD_FAILURE** (fault pages, permission denials, unresolvable or
ambiguous targets, failed checkpoints — reported with step, expected,
observed, and evidence paths). The engine never raises at the caller
boundary; every run returns a `ReplayResult` whose payload must match its
status.

The per-step evaluation order is fixed: **declared outcome → checkpoint →
retryable → escalatable → hard failure**. Outcomes come first because a step
can succeed mechanically while landing on a refusal screen — checked later,
"the app said no" would be misreported as a locator failure on the *next*
step. Recovery order is **retry (cheap, bounded) → escalate (expensive,
unblocks anything a person could) → fail**; inverting the first two pages a
human for a slow page load. `SESSION_EXPIRED` is deliberately both retryable
and escalatable: retry wins while the step's budget lasts, then escalation
applies. A retry-only condition whose budget is spent (a persistently slow
load) terminates as HARD_FAILURE with the recoverable code preserved in the
failure detail — the status says how the run ended, the code says why. The
result contract also holds the fleet classification at the boundary: a fault
code can never be reported as a business outcome, or vice versa.

## Heterogeneity & multi-tenant

**A — the surface seam (built).** Everything above `surface/base.py` — the
planner, the replay engine, and a human operator mid-handoff — speaks the
same interface: `observe` / `act` / `resolve` / `probe`, plus a declared
`SurfaceFeature` set. An artifact computes its required features over **all**
ladder rungs of every target (`Capability.required_features()`), so a surface
that would silently strip an artifact's fallback rungs is refused as
`SURFACE_INCOMPATIBLE` before the first mutating action — implemented as the
engine's pre-flight check and tested
(`test_surface_without_frames_is_refused_before_any_interaction`,
`test_required_features_cover_all_rungs_and_frames`). A legacy-web or
desktop (UIA/accessibility) surface slots in by implementing the same seam
and declaring what it honours; the ladders already prefer exactly the
strategies an accessibility tree can serve — role, label, name rank highest
by durability.

**B — tenant overlays (built and demonstrated).** Each vendor product gets
one base capability. A tenant overlay (`schema/overlay.py`) is a typed list
of patch **operations only** — `retarget_step` (swap a ladder), `insert_steps`,
`add_outcome`, `set_entry` — and everything not overridden inherits, so a
vendor-side fix lands once in the base and every tenant picks it up. The
overlay grammar has **no vocabulary** for removing a step or outcome,
changing the success condition, or relaxing the safety block: the dangerous
tenant-local change is inexpressible rather than detected, and a retarget of
a mutating step must keep a verify predicate at least as strong as the
base's. The one nuance: an overlay may carry `hosts` that re-bind **where**
the tenant instance lives — never what automation may do there — and the
operator-owned deployment policy still gates every host at the CLI
intersection. Resolution is materialization: `apply_overlay` combines base +
overlay at load and revalidates the result through the same `Capability`
validators as any recorded artifact.

Demonstrated live against a second tenant skin of the mock console
("bluepeak": relabelled controls, an extra mandatory VERIFY screen): the base
artifact replayed against the tenant instance **fails** (its own host binding
refuses, and at the UI level the relabelled sign-in cannot resolve), while
`tellerly replay --tenant bluepeak` resolves the committed overlay
(`capabilities/transfer_between_shares/overlays/bluepeak.json`) and replays
to SUCCESS through the extra screen — with the drift signal visible in the
same run: the operator-id fill resolved via the form-`name` rung because the
tenant relabelled the field (`tests/test_cross_tenant.py`,
`tests/test_overlay.py`, evidence indexed in `evidence/README.md`).

**C — drift.** Replay already records `resolved_via` + `rung_index` per step
(`StepOutcome`) — which ladder rung actually matched. Aggregated per tenant,
a tenant resolving below the fleet-median rung for the same step is
diverging from the base UI: flag it for re-record **before** anything breaks,
instead of discovering the divergence as a failed run.

## Escalation & handoff

"Stuck" is not a heuristic: escalation fires only for a condition whose code
is in the ESCALATABLE set and only when an `EscalationHandler` is configured
— at a step whose retry budget is spent, or immediately when a declared
HARD_FAILURE outcome (a permission-denial page) is detected
(`kernel/operator.py`; without a handler, the run fails exactly as before,
with a note marking the seam). The
engine escalates at two places: a budget-exhausted step failure, and a
declared HARD_FAILURE outcome with an escalatable code (the NOT AUTHORIZED
page). **One intervention per step** — a decision that re-fails cannot loop.
The `InterventionRequest` carries what someone who wasn't watching needs:
capability, step id, URL, reason code, expected-vs-observed message,
screenshot, redacted DOM snapshot, timestamps, and a deadline
(`limits.escalation_timeout_s`).

Ownership of the live session is a **single token guarding one session**
(`tellerly.kernel.control.ControlToken`): `AUTOMATION_RUNNING → PAUSED →
HUMAN_CONTROL → AUTOMATION_RUNNING`, with `PAUSED --timeout--> ABORTED` and
`HUMAN_CONTROL --abort--> ABORTED`. The engine fires ESCALATE; the handler
fires TAKE_CONTROL when engagement starts; RESUME and ABORT belong to the
engine alone. A deadline nobody meets raises `EscalationTimeout`: the token
times out into ABORTED and the run fails `ESCALATION_TIMEOUT` — the console's
blocking read is itself deadline-bounded, so an unattended run fails loudly
rather than holding a live banking session open. Handoff is a pause, not a
restart: same page, same cookies, same half-filled form — for interactive
sessions `--escalate` opens the browser headed so the operator can see the
session they may take over (a piped session drives via the console's `look`).

The human drives an `OperatorSession` — `look` / `act` / `goto` / `note` —
through the **same Surface and the same PolicyGate** as automation: a policy
violation comes back as a message, not a crash; every action is recorded as
a `HumanAction` and a `human_action` evidence event; and the whole exchange
is preserved in the result as an `InterventionRecord`. That is the audit
story — there is no side door. **Resume is a decision, not a signal** —
CONTINUE (the human completed the step; it is recorded SKIPPED "completed by
operator"), RETRY_STEP (same step, fresh budget, fresh resolution — after a
human touched the page nothing is assumed), SKIP_STEP, or ABORT
(`ABORTED_BY_OPERATOR`); the state machine refuses a bare resume. For an
escalated declared outcome, CONTINUE re-scans the outcomes once — still
detected means fail hard, with no second escalation.

The shipped operator surface is a **terminal console**
(`TerminalOperatorConsole`, wired via `tellerly replay --escalate`):
deliberately minimal — it prints the intervention and reads
`look` / `click` / `fill` / … / `done <decision>` commands — but it sits at
the real seam; a web console would be a different front-end to the same
`OperatorSession`. All of the above is exercised in
`tests/test_escalation.py` (abort, skip-to-success, a human click through
the Surface, timeout, no-handler unchanged, one-intervention-per-step).

## Safety

One `PolicyGate` (`kernel/guardrails.py`) is the single enforcement point —
discovery, replay, and the operator session all call the same
`check_url`/`check_action`, and replay checks every *frame* URL an action
landed in, not just the address bar. The deployment policy
(`config/policy.yaml`, operator-owned) intersects with the artifact's safety
block: an artifact can only **narrow** hosts and actions, never widen them,
and `require_confirmation` only ORs **on**. It ships `true` at every layer —
deployment default, `SafetyPolicy` default, and compiler output — and tests
lock each (`test_kernel_services`, `test_schema_artifact`, `test_compiler`).

Risk is classified from what a step **does**, not what the planner calls it:
only clicks can be MUTATING, decided by word-bounded posting verbs in the
control's own wording ("Payments" is not "pay"; a false positive costs one
extra confirmation, a false negative posts money unattended). Reads and form
typing are never risky — the click commits. An unapproved mutating step
blocks with `POLICY_BLOCKED` **before** acting, target-less legacy GET-URL
mutations included
(`test_unapproved_mutation_is_policy_blocked_before_posting`).

Redaction is **value-based**, not field-name-based: sensitive values are
registered once and masked wherever they appear; every structured write is
serialized first, then redacted over the exact bytes that hit disk. Secrets
reach neither the model (the planner only ever sees `{{input.*}}`
placeholders) nor disk (`test_evidence_never_contains_the_access_key`), and
a value too short to redact safely is refused rather than written. Limits:
screenshots are captured **raw** (pixel masking is a documented cut), and
interactive approval is not wired into escalation — `--approve` is a per-run
pre-authorization, not something an operator can grant mid-run.

## Cuts

Deliberate, with what would be built next:

- **Second surface implementation** — the seam is real (features, refusal,
  tests) but only Playwright-web exists. Next: a desktop UIA/a11y surface,
  which the role/label/name ladder ordering was designed for.
- **Web operator console** — the terminal console sits at the real seam
  instead; a web UI would be another front-end to the same `OperatorSession`.
- **Screenshot masking** — value redaction covers every structured write;
  pixels are raw. Next: mask the bounding boxes of sensitive controls.
- **Credential vault** — secrets arrive via `env:` indirection, never on the
  command line or in evidence; a real deployment would bind
  `sensitivity: secret` inputs to a vault.
- **Session-expiry assisted re-login** — detected, retried, and escalatable;
  not auto-healed. Next: a declared re-auth recovery flow gated on the vault.
- **Overlay authoring tooling** — the overlay grammar, resolver, and a real
  tenant demonstration are built (§4B); authoring an overlay is still manual
  (JSON by hand). Next: derive one from a short verification run against the
  tenant instance, diffing which rungs stopped resolving.
- **Drift telemetry aggregation** — the per-step signal is recorded on every
  run; the fleet-median comparison job does not exist yet.
- **Queueing / service split** — one process is honest for the scale of one
  vertical slice; the artifact store and result contract are the future
  service API.
- **Discovery-time escalation** — discovery is operator-attended by nature;
  the planner's `give_up` covers its dead ends.
