# REPORT

## Architecture

*(to come — single-process CLI, package boundaries `schema` / `surface` /
`discovery` / `replay` / `kernel`, and the cost table: discovery pays model
tokens once; replay reports `llm_calls=0`, `$0.00`.)*

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
discouraged. Ladder confidences are **measured, not guessed**: at the moment
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
code can never be reported as a business outcome, or vice versa. UI drift is the secondary concern (these surfaces are stable):
per-step `resolved_via` telemetry records which rung actually matched, so
degradation is visible long before anything breaks.

## Heterogeneity & multi-tenant

*(approach under discussion — designed next.)*

## Escalation & handoff

"Stuck" is not a heuristic: escalation fires iff a condition's code is in the
ESCALATABLE set and the retry budget is spent. The intervention request
carries what someone who wasn't watching needs — capability, step id, URL,
reason code, message, screenshot, DOM snapshot, timestamps, and a deadline.

Ownership of the live session is a **single token guarding one session**
(`tellerly.kernel.control.ControlToken`), which the engine awaits before
every mutating action — so handoff is a pause, not a restart: same page, same
cookies, same half-filled form. The browser runs headed over CDP precisely so
a person can operate the same session automation was using. The state machine
is `AUTOMATION_RUNNING → PAUSED → HUMAN_CONTROL → AUTOMATION_RUNNING`, with
`PAUSED --timeout--> ABORTED` (an unattended run fails loudly rather than
holding a live banking session open) and `HUMAN_CONTROL --abort--> ABORTED`.
**Resume is a decision, not a signal** — CONTINUE / RETRY_STEP / SKIP_STEP,
because after a human touches the page the engine cannot infer which is
correct; ABORT is its own transition, and the machine refuses a bare resume.
After any resume the engine re-establishes state from scratch: both CONTINUE
and RETRY_STEP re-resolve targets and re-check preconditions. The human
drives through the same Surface object as automation, so their actions land
in the same audit trail, subject to the same policy, and are preserved in the
result as an `InterventionRecord`.

## Safety

*(to come — host + action-type allowlist enforced in one place; artifacts can
narrow but never widen deployment policy; `require_confirmation` on by
default; value-based redaction on every disk write.)*

## Cuts

*(to come — what was deliberately left out and what would be built next.)*
