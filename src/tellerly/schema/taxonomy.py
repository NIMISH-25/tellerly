"""The outcome and failure taxonomy: the fleet-wide vocabulary for "what
happened", derived from the target's failure matrix — not invented first.

Four tiers:

- SUCCESS          — the checkpoint held; declared outputs were captured.
- BUSINESS_OUTCOME — the application ran fine and said no. "No such member"
  is an answer the caller needs, not a crash.
- RECOVERABLE      — a runtime condition replay can clear itself (wait/retry,
  dismiss a known interstitial, re-establish a session).
- HARD_FAILURE     — stop, surface a debuggable error.

Per-step evaluation order (the replay engine follows this exactly):

    declared outcome? -> checkpoint? -> retryable? -> escalatable? -> hard failure

Declared outcomes are checked FIRST because a step can succeed mechanically
while landing on a refusal screen — and because on a refusal screen the next
step's locator will fail, which would misreport "the app said no" as a
locator failure.

Recovery order:  retry (cheap, bounded) -> escalate (expensive, unblocks
anything a person could) -> fail. Inverting the first two would page a human
for a slow page load.

SESSION_EXPIRED sits in both RETRYABLE and ESCALATABLE. Retry wins while the
step's retry budget lasts; once the budget is spent, escalation applies.

A retry-only code whose budget is spent (SLOW_RESPONSE, INTERSTITIAL_PRESENT)
terminates the run as a HARD_FAILURE whose FailureDetail preserves the
recoverable code: the status says how the run ended, the code says which
condition ended it. RECOVERABLE is therefore never itself a terminal status.
"""
from __future__ import annotations

from enum import Enum


class Tier(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class Code(str, Enum):
    # -- business outcomes: the app answered, and the answer was no ----------
    NO_SUCH_RECORD = "no_such_record"              # search/lookup found nothing
    OPERATION_REFUSED = "operation_refused"        # validation refusal (e.g. share on hold)
    INSUFFICIENT_FUNDS = "insufficient_funds"      # amount exceeds available balance
    ALREADY_PROCESSED = "already_processed"        # duplicate submit detected by the app

    # -- recoverable runtime conditions --------------------------------------
    SLOW_RESPONSE = "slow_response"                # load/step exceeded its timeout
    INTERSTITIAL_PRESENT = "interstitial_present"  # known dismissible screen appeared
    SESSION_EXPIRED = "session_expired"            # auth session lapsed mid-flow

    # -- hard failures -------------------------------------------------------
    PERMISSION_DENIED = "permission_denied"        # operator profile lacks access
    APP_FAULT = "app_fault"                        # target app 5xx / fault page
    TARGET_NOT_FOUND = "target_not_found"          # no ladder rung resolved the control
    TARGET_AMBIGUOUS = "target_ambiguous"          # a rung matched more than one control
    VERIFY_FAILED = "verify_failed"                # resolved control failed its verify predicate
    CHECKPOINT_FAILED = "checkpoint_failed"        # asserted state not reached
    SURFACE_INCOMPATIBLE = "surface_incompatible"  # surface lacks a feature the ladder needs
    POLICY_BLOCKED = "policy_blocked"              # guardrail refused the action
    INPUT_INVALID = "input_invalid"                # supplied params fail the input contract
    ESCALATION_TIMEOUT = "escalation_timeout"      # no operator picked up the intervention
    ABORTED_BY_OPERATOR = "aborted_by_operator"    # human chose to abort the run


#: The tier a code carries while being handled. Total over Code (tested).
#: At the caller boundary, RECOVERABLE codes never appear as a terminal status
#: — after budget exhaustion they surface inside a HARD_FAILURE's detail.
DEFAULT_TIER: dict[Code, Tier] = {
    Code.NO_SUCH_RECORD: Tier.BUSINESS_OUTCOME,
    Code.OPERATION_REFUSED: Tier.BUSINESS_OUTCOME,
    Code.INSUFFICIENT_FUNDS: Tier.BUSINESS_OUTCOME,
    Code.ALREADY_PROCESSED: Tier.BUSINESS_OUTCOME,
    Code.SLOW_RESPONSE: Tier.RECOVERABLE,
    Code.INTERSTITIAL_PRESENT: Tier.RECOVERABLE,
    Code.SESSION_EXPIRED: Tier.RECOVERABLE,
    Code.PERMISSION_DENIED: Tier.HARD_FAILURE,
    Code.APP_FAULT: Tier.HARD_FAILURE,
    Code.TARGET_NOT_FOUND: Tier.HARD_FAILURE,
    Code.TARGET_AMBIGUOUS: Tier.HARD_FAILURE,
    Code.VERIFY_FAILED: Tier.HARD_FAILURE,
    Code.CHECKPOINT_FAILED: Tier.HARD_FAILURE,
    Code.SURFACE_INCOMPATIBLE: Tier.HARD_FAILURE,
    Code.POLICY_BLOCKED: Tier.HARD_FAILURE,
    Code.INPUT_INVALID: Tier.HARD_FAILURE,
    Code.ESCALATION_TIMEOUT: Tier.HARD_FAILURE,
    Code.ABORTED_BY_OPERATOR: Tier.HARD_FAILURE,
}

#: Bounded retry may clear these. All are RECOVERABLE-tier by construction.
RETRYABLE: frozenset[Code] = frozenset(
    {Code.SLOW_RESPONSE, Code.INTERSTITIAL_PRESENT, Code.SESSION_EXPIRED}
)

#: Worth a human's attention once retry is exhausted (or immediately, when the
#: code is not retryable). A person can unblock any of these.
ESCALATABLE: frozenset[Code] = frozenset(
    {
        Code.SESSION_EXPIRED,       # also RETRYABLE — retry budget decides which wins
        Code.PERMISSION_DENIED,
        Code.APP_FAULT,
        Code.TARGET_NOT_FOUND,
        Code.TARGET_AMBIGUOUS,
        Code.VERIFY_FAILED,
        Code.CHECKPOINT_FAILED,
    }
)

#: Neither retried nor escalated: the run ends with this answer immediately.
#: Every business outcome is terminal — the app answered; asking a human to
#: "fix" an answer would be asking them to falsify it.
TERMINAL: frozenset[Code] = frozenset(
    {
        Code.NO_SUCH_RECORD,
        Code.OPERATION_REFUSED,
        Code.INSUFFICIENT_FUNDS,
        Code.ALREADY_PROCESSED,
        Code.SURFACE_INCOMPATIBLE,
        Code.POLICY_BLOCKED,
        Code.INPUT_INVALID,
        Code.ESCALATION_TIMEOUT,
        Code.ABORTED_BY_OPERATOR,
    }
)

#: The per-step evaluation order, in words the engine implements literally.
EVALUATION_ORDER: tuple[str, ...] = (
    "declared_outcome",
    "checkpoint",
    "retryable",
    "escalatable",
    "hard_failure",
)

#: The recovery order. Inverting the first two pages a human for a slow load.
RECOVERY_ORDER: tuple[str, ...] = ("retry", "escalate", "fail")
