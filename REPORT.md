# REPORT

> Status: skeleton. Sections fill in as their phase lands — §2–§5 are drafted
> during the design phase, §1/§6/§7 during implementation.

## Architecture

*(to come — single-process CLI, package boundaries `schema` / `surface` /
`discovery` / `replay` / `kernel`, and the cost table: discovery pays model
tokens once; replay reports `llm_calls=0`, `$0.00`.)*

## Artifact schema

*(to come — designed in a dedicated session: ranked locator ladder with
measured-not-guessed rungs, closed `{{input.name}}` substitution grammar,
declared business outcomes, typed inputs/outputs, load-time coherence
validators.)*

## Determinism & error handling

*(to come — four-tier result contract: SUCCESS / BUSINESS_OUTCOME /
RECOVERABLE / HARD_FAILURE; per-step evaluation order: declared outcome →
checkpoint → retryable → escalatable → hard failure; recovery order:
retry → escalate → fail.)*

## Heterogeneity & multi-tenant

*(to come — surface feature declarations checked against all locator rungs
before the first mutating action; tenant overlays that can retarget or insert
but are structurally unable to remove a checkpoint or relax safety; drift
detection from `resolved_via` telemetry.)*

## Escalation & handoff

*(to come — single ownership token over one live CDP session; PAUSED →
HUMAN_CONTROL → resume as a decision (CONTINUE / RETRY_STEP / SKIP_STEP /
ABORT); state re-established after every human touch; escalation timeout
aborts rather than holding a live banking session open.)*

## Safety

*(to come — host + action-type allowlist enforced in one place; artifacts can
narrow but never widen deployment policy; `require_confirmation` on by
default; value-based redaction on every disk write.)*

## Cuts

*(to come — what was deliberately left out and what would be built next.)*
