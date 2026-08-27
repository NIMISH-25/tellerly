"""The deterministic replay engine: execute a recorded capability, no model.

Where discovery spends model calls deciding what to do, replay only ever does
what the artifact says — substitute the caller's params into the recorded
steps, resolve each target down its locator ladder, and check every claimed
state. The per-step evaluation order comes from the taxonomy and is
implemented literally:

    declared outcome? -> the step itself -> retryable? -> escalatable? -> hard failure

Declared outcomes are checked FIRST because a step can succeed mechanically
while landing on a refusal screen — misreporting "the app said no" as a
locator failure would be worse than either.

``run()`` NEVER raises to the caller: every run — success, refusal, or wreck
— returns a ``ReplayResult``, and an unexpected engine/browser fault becomes
a HARD_FAILURE with ``Code.EXECUTION_ERROR``.

This module must not import a model SDK, an HTTP client, or
``tellerly.discovery`` — the no-model contract is enforced structurally
(import-graph tests) and economically (the result model refuses llm_calls>0).
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn
from urllib.parse import urljoin

from tellerly.config import repo_relative
from tellerly.kernel.control import (
    ControlEvent,
    ControlState,
    ControlToken,
    IllegalTransition,
)
from tellerly.kernel.evidence import RunLog
from tellerly.kernel.guardrails import PolicyGate, PolicyViolation
from tellerly.kernel.operator import (
    NOTE_PREFIX,
    EscalationHandler,
    EscalationTimeout,
    OperatorSession,
)
from tellerly.kernel.redaction import Redactor
from tellerly.schema import (
    ESCALATABLE,
    ActionType,
    ActStep,
    Capability,
    CheckpointStep,
    Code,
    DeclaredOutcome,
    Economics,
    FailureDetail,
    InputType,
    InterventionRecord,
    InterventionRequest,
    OutcomeReport,
    OutputType,
    ReadStep,
    ReplayResult,
    ResumeDecision,
    Risk,
    Sensitivity,
    StateCondition,
    StepOutcome,
    StepStatus,
    Target,
    Tier,
)
from tellerly.schema.artifact import Step
from tellerly.schema.bindings import substitute
from tellerly.surface.base import Resolution, Surface

# Outcome detection and text-absence checks use an immediate single-pass
# find_text. The spec's quick 0.2s window is deliberately NOT used: the
# surface's find_text polls on a 250ms grid, so any nonzero timeout costs a
# full 250ms per ABSENT text — and every step scans the whole outcome
# catalogue, which would multiply into minutes across a run. act() already
# settles the page before the scan, and a detection missed mid-frame-swap is
# caught by the very next attempt's scan, so the immediate pass loses nothing.
_QUICK_TIMEOUT_S = 0.0

#: Fields a rung can carry that bindings are legal in — mirrors
#: ``tellerly.schema.artifact._target_strings`` so materialization and
#: load-time validation always agree on what is substitutable.
_RUNG_STRING_FIELDS = ("role", "name", "label", "text", "anchor_text", "css", "control")

_RESOLUTION_CODE: dict[str, Code] = {
    "not_found": Code.TARGET_NOT_FOUND,
    "ambiguous": Code.TARGET_AMBIGUOUS,
    "verify_failed": Code.VERIFY_FAILED,
}


class _Terminal(Exception):
    """Internal control flow only: a fully-formed terminal state. ``run()`` is
    the single place that turns one into a ReplayResult — it never escapes."""

    def __init__(
        self,
        status: Tier,
        outputs: dict[str, str | int | float | bool] | None = None,
        outcome: OutcomeReport | None = None,
        failure: FailureDetail | None = None,
    ) -> None:
        super().__init__(status.value)
        self.status = status
        self.outputs = outputs
        self.outcome = outcome
        self.failure = failure


class _StepFailure(Exception):
    """One attempt of one step failed — the retry loop decides what happens."""

    def __init__(
        self, code: Code, expected: str, observed: str, resolution: Resolution | None = None
    ) -> None:
        super().__init__(observed)
        self.code = code
        self.expected = expected
        self.observed = observed
        self.resolution = resolution


class _SkipStep(Exception):
    """Internal control flow only: an operator decided the pending step is
    moot. The step loop records a SKIPPED outcome and moves on."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        gate: PolicyGate,
        evidence_root: Path,
        approve_mutations: bool = False,
        escalation: EscalationHandler | None = None,
        operator_name: str = "operator",
    ) -> None:
        self.surface = surface
        self.gate = gate
        self.evidence_root = evidence_root
        self.approve_mutations = approve_mutations
        self.escalation = escalation
        self.operator_name = operator_name
        # Per-run state; reset at the top of run().
        self._capability: Capability | None = None
        self._params: dict[str, str] = {}
        self._base_url = ""
        self._run_id = ""
        self._log: RunLog | None = None
        self._redactor: Redactor | None = None
        self._telemetry: list[StepOutcome] = []
        self._outputs: dict[str, str | int | float | bool] = {}
        self._current_step_id: str | None = None
        self._token = ControlToken()
        self._escalations: list[InterventionRecord] = []
        self._escalated_steps: dict[str | None, bool] = {}
        self._intervention_count = 0

    # ------------------------------------------------------------------- run

    def run(self, capability: Capability, params: dict[str, str], base_url: str) -> ReplayResult:
        started = time.monotonic()
        run_id = f"replay-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        self._capability = capability
        self._params = dict(params)
        self._base_url = base_url.rstrip("/")
        self._run_id = run_id
        self._log = None
        self._redactor = None
        self._telemetry = []
        self._outputs = {}
        self._current_step_id = None
        self._token = ControlToken()
        self._escalations = []
        self._escalated_steps = {}
        self._intervention_count = 0

        status = Tier.HARD_FAILURE
        outputs: dict[str, str | int | float | bool] | None = None
        outcome: OutcomeReport | None = None
        failure: FailureDetail | None = None
        try:
            self._execute(capability, run_id)
        except _Terminal as terminal:
            status = terminal.status
            outputs, outcome, failure = terminal.outputs, terminal.outcome, terminal.failure
        except PolicyViolation as exc:
            # A gate refusal anywhere outside the per-step guard (e.g. the
            # entry URL) is still a policy answer, not an engine fault.
            failure = FailureDetail(
                step_id=self._current_step_id,
                code=Code.POLICY_BLOCKED,
                expected="every destination and action inside the policy envelope",
                observed=str(exc),
            )
        except Exception as exc:
            # The whole run body is guarded: an unexpected fault is reported,
            # never raised — the run failed, the record didn't.
            failure = FailureDetail(
                step_id=self._current_step_id,
                code=Code.EXECUTION_ERROR,
                expected="the replay engine and browser to run without an internal fault",
                observed=f"{type(exc).__name__}: {exc}",
            )

        if failure is not None and failure.code in ESCALATABLE and self.escalation is None:
            # Non-configured runs keep the seam note; with a handler configured
            # the real mechanism (an InterventionRecord) replaces it.
            failure.observed += " | escalation seam: a human intervention would fire here"

        evidence_dir = repo_relative(self._log.dir) if self._log is not None else None
        if failure is not None:
            shot = self._screenshot("failure")
            if shot is not None:
                failure.evidence.append(shot)
        final_shot = self._screenshot("final")
        if failure is not None and final_shot is not None:
            failure.evidence.append(final_shot)

        economics = Economics(wall_time_s=round(time.monotonic() - started, 2))
        try:
            result = ReplayResult(
                run_id=run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=status,
                outputs=outputs,
                outcome=outcome,
                failure=failure,
                escalations=list(self._escalations),
                steps=self._telemetry,
                economics=economics,
                evidence_dir=evidence_dir,
            )
        except Exception as exc:
            # A payload that violates the result contract is an engine bug —
            # reported through the same never-raise boundary as any other.
            result = ReplayResult(
                run_id=run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=None,
                    code=Code.EXECUTION_ERROR,
                    expected="a payload satisfying the replay result contract",
                    observed=f"result assembly failed: {type(exc).__name__}: {exc}",
                ),
                economics=economics,
                evidence_dir=evidence_dir,
            )

        if self._log is not None:
            try:
                self._log.write_json("result.json", result)
                self._log.event("run_finished", status=result.status.value)
            except Exception:
                pass  # an evidence-write failure must not mask the result
        return result

    # ----------------------------------------------------------------- flow

    def _execute(self, capability: Capability, run_id: str) -> NoReturn:
        # a. The input contract, before anything else — every problem listed.
        problems = self._param_problems(capability)
        if problems:
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=None,
                    code=Code.INPUT_INVALID,
                    expected="params satisfying the capability's declared input contract",
                    observed="; ".join(problems),
                ),
            )

        # b. Feature check BEFORE anything touches the page: a surface that
        # would silently strip the artifact's fallbacks is refused up front.
        missing = capability.required_features() - set(self.surface.features())
        if missing:
            names = ", ".join(sorted(feature.value for feature in missing))
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=None,
                    code=Code.SURFACE_INCOMPATIBLE,
                    expected="a surface honouring every feature the artifact's ladders need",
                    observed=f"surface lacks: {names}",
                ),
            )

        # c. Redaction BEFORE the first disk write: sensitive values are
        # caught wherever they appear, not by field name.
        redactor = Redactor()
        for name, decl in capability.inputs.items():
            if decl.sensitivity is not Sensitivity.NONE and name in self._params:
                redactor.register(name, self._params[name])
        self._redactor = redactor  # escalations redact through the same run redactor
        self._log = RunLog(self.evidence_root, run_id, redactor)
        self._log.event(
            "run_started",
            capability=capability.id,
            version=capability.version,
            base_url=self._base_url,
            params=sorted(self._params),  # names only — values add nothing but risk
        )

        # d. Entry.
        entry_path = substitute(capability.entry, self._params)
        entry_url = urljoin(self._base_url + "/", entry_path.lstrip("/"))
        self.gate.check_url(entry_url)
        self._log.event("entry", url=entry_url)
        self.surface.open(entry_url)

        # e. The recorded steps, in order.
        for step in capability.steps:
            self._run_step(step)
        self._current_step_id = None

        # f. The final checkpoint, with a generous timeout.
        if self._condition_holds(capability.success, timeout_s=capability.limits.step_timeout_s):
            self._finish_success()

        # The success condition failed — the app may have said no.
        detected = self._detect_outcome()
        if detected is not None:
            cleared = False
            try:
                self._terminal_for_outcome(detected, step_id=None)
                cleared = True  # an operator cleared the declared condition
            except _SkipStep:
                # No pending step exists at the final gate; "skip" can only
                # mean the operator considers the condition moot.
                cleared = True
            if cleared and self._condition_holds(
                capability.success, timeout_s=capability.limits.step_timeout_s
            ):
                self._finish_success()
        raise _Terminal(
            Tier.HARD_FAILURE,
            failure=FailureDetail(
                step_id=None,
                code=Code.CHECKPOINT_FAILED,
                expected="success condition: " + _describe_condition(capability.success),
                observed=f"path '{self.surface.current_path()}'" + self._page_hint(),
            ),
        )

    def _finish_success(self) -> NoReturn:
        missing_outputs = sorted(set(self._capability.outputs) - set(self._outputs))
        if missing_outputs:
            # Structurally unreachable (the artifact validator ties every
            # output to a read step) — guarded so a schema change cannot
            # silently ship a SUCCESS with holes.
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=None,
                    code=Code.EXECUTION_ERROR,
                    expected="every declared output captured before success",
                    observed="missing outputs: " + ", ".join(missing_outputs),
                ),
            )
        raise _Terminal(Tier.SUCCESS, outputs=dict(self._outputs))

    def _run_step(self, step: Step) -> None:
        # A RETRY_STEP decision re-enters with a fresh budget and a fresh
        # resolution; the once-per-step escalation guard makes the loop finite.
        while self._run_step_pass(step):
            pass

    def _run_step_pass(self, step: Step) -> bool:
        """One fully-budgeted pass at a step. Returns True only when an
        operator decided RETRY_STEP and the step must run again from scratch."""
        self._current_step_id = step.id
        started = time.monotonic()
        budget = 1 + self._capability.limits.max_retries_per_step
        attempt = 0
        recovered = False
        last: _StepFailure | None = None
        recorded = False

        def record(
            status: StepStatus, resolution: Resolution | None, note: str | None = None
        ) -> None:
            nonlocal recorded
            if recorded:
                return  # the terminal path may pass through here twice
            recorded = True
            telemetry = StepOutcome(
                step_id=step.id,
                status=status,
                attempts=max(attempt, 1),
                resolved_via=resolution.strategy if resolution is not None else None,
                rung_index=resolution.rung_index if resolution is not None else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                note=note,
            )
            self._telemetry.append(telemetry)
            self._event("step_finished", **telemetry.model_dump(mode="json"))

        last_recovered: DeclaredOutcome | None = None
        try:
            while attempt < budget:
                attempt += 1
                # Declared outcome FIRST: on a refusal screen the step's own
                # locator would fail and misreport the app's answer.
                detected = self._detect_outcome()
                if detected is not None:
                    if detected.disposition is Tier.RECOVERABLE:
                        self._recover(detected)
                        recovered = True
                        last_recovered = detected
                        continue  # recovery counts against the retry budget
                    self._terminal_for_outcome(detected, step.id)
                # The step itself.
                try:
                    resolution = self._attempt(step)
                except _StepFailure as failure:
                    last = failure
                    if attempt < budget:
                        between = self._detect_outcome()
                        if between is not None and between.disposition is Tier.RECOVERABLE:
                            self._recover(between)
                            recovered = True
                            last_recovered = between
                        elif between is not None:
                            self._terminal_for_outcome(between, step.id)
                        else:
                            time.sleep(0.5)  # transient slowness — cheapest recovery
                    continue
                record(StepStatus.RECOVERED if recovered else StepStatus.OK, resolution)
                return False

            # Budget exhausted. Final outcome scan FIRST — the app may have
            # said no, and that answer outranks any locator diagnosis.
            final = self._detect_outcome()
            if final is not None:
                if not (
                    final.disposition is Tier.HARD_FAILURE
                    and self._escalation_applies(step.id, final.code)
                ):
                    # No escalation will fire for this outcome: record now so
                    # telemetry keeps the resolution detail and the outcome
                    # note (the terminal catch below knows neither).
                    record(StepStatus.FAILED, last.resolution if last else None,
                           note=f"declared outcome '{final.id}'")
                self._terminal_for_outcome(final, step.id)
                # Returned: an operator cleared the declared condition after
                # the budget was spent — the loop it interrupted is gone, so
                # give the step a fresh pass instead.
                return True
            if last is None:
                # Every attempt was consumed clearing a recoverable condition
                # that kept coming back. The status says how the run ended;
                # the preserved code says which condition ate the budget.
                code = last_recovered.code if last_recovered is not None else Code.EXECUTION_ERROR
                expected = f"step '{step.id}' to execute within its retry budget"
                observed = (
                    f"all {budget} attempts were consumed by recoveries"
                    + (
                        f" for declared outcome '{last_recovered.id}'"
                        if last_recovered is not None
                        else ""
                    )
                )
                # SESSION_EXPIRED sits in both RETRYABLE and ESCALATABLE; with
                # the budget spent, escalation gets its turn at this exit too.
                decision = self._maybe_escalate(step.id, code, expected, observed)
                if decision is not None:
                    return self._apply_step_decision(decision, step, record)
                record(StepStatus.FAILED, None, note=code.value)
                raise _Terminal(
                    Tier.HARD_FAILURE,
                    failure=FailureDetail(
                        step_id=step.id,
                        code=code,
                        expected=expected,
                        observed=observed,
                    ),
                )
            observed = f"{last.observed} ({attempt} attempts)"
            decision = self._maybe_escalate(step.id, last.code, last.expected, observed)
            if decision is not None:
                return self._apply_step_decision(decision, step, record)
            record(StepStatus.FAILED, last.resolution, note=last.code.value)
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step.id, code=last.code, expected=last.expected, observed=observed
                ),
            )
        except _SkipStep as skip:
            record(StepStatus.SKIPPED, None, note=skip.note)
            return False
        except _Terminal as terminal:
            record(StepStatus.FAILED, None, note=_terminal_note(terminal))
            raise
        except PolicyViolation as exc:
            record(StepStatus.FAILED, None, note="policy blocked")
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step.id,
                    code=Code.POLICY_BLOCKED,
                    expected="every destination and action inside the policy envelope",
                    observed=str(exc),
                ),
            )
        except Exception as exc:
            # Recorded for telemetry, then re-raised for the run-level guard
            # to classify as EXECUTION_ERROR.
            record(StepStatus.FAILED, None, note=f"engine fault: {type(exc).__name__}")
            raise

    # ------------------------------------------------------------- one step

    def _attempt(self, step: Step) -> Resolution | None:
        if isinstance(step, ActStep):
            return self._attempt_act(step)
        if isinstance(step, CheckpointStep):
            timeout = step.timeout_s or self._capability.limits.step_timeout_s
            if not self._condition_holds(step.condition, timeout_s=timeout):
                raise _StepFailure(
                    Code.CHECKPOINT_FAILED,
                    expected=f"checkpoint '{step.description}': "
                    + _describe_condition(step.condition),
                    observed=f"path '{self.surface.current_path()}'",
                )
            return None
        if isinstance(step, ReadStep):
            return self._attempt_read(step)
        raise ValueError(f"unknown step kind {type(step).__name__}")  # pragma: no cover

    def _attempt_act(self, step: ActStep) -> Resolution | None:
        if step.risk is Risk.MUTATING and self.gate.require_confirmation and not self.approve_mutations:
            # BEFORE anything executes — target-less mutating steps included:
            # a legacy GET-URL that commits state is exactly that shape.
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step.id,
                    code=Code.POLICY_BLOCKED,
                    expected="confirmation before a mutating step",
                    observed=(
                        f"step '{step.id}' mutates the target app and this run is not "
                        "approved — re-run with --approve (approve_mutations=True) to "
                        "authorize its mutations"
                    ),
                ),
            )

        if step.target is None:
            if step.action is not ActionType.NAVIGATE:
                # Schema-legal (PRESS may omit a target) but this engine has no
                # focused-control notion — executing it as anything else would
                # be a guess. Deterministic misconfiguration: retry cannot help.
                raise _Terminal(
                    Tier.HARD_FAILURE,
                    failure=FailureDetail(
                        step_id=step.id,
                        code=Code.EXECUTION_ERROR,
                        expected="a target on every non-navigate action",
                        observed=f"step '{step.id}' is a target-less {step.action.value}",
                    ),
                )
            path = substitute(step.value or "", self._params)
            self.gate.check_action(step.action)
            self.gate.check_url(urljoin(self._base_url + "/", path.lstrip("/")))
            self.surface.navigate(path)
            self._check_landed()
            return None

        resolution = self.surface.resolve(self._materialize_target(step.target))
        if resolution.status != "resolved":
            raise _StepFailure(
                _RESOLUTION_CODE[resolution.status],
                expected=f"exactly one control matching {step.target.description!r}",
                observed=resolution.detail or resolution.status,
                resolution=resolution,
            )
        self.gate.check_action(step.action)
        value = substitute(step.value, self._params) if step.value is not None else None
        self.surface.act(resolution.uid, step.action, value)
        self._check_landed()
        return resolution

    def _check_landed(self) -> None:
        """The policy envelope covers where the action actually landed —
        frame documents included, not just the top-level URL."""
        for url in self.surface.frame_urls():
            self.gate.check_url(url)

    def _attempt_read(self, step: ReadStep) -> Resolution:
        resolution = self.surface.resolve(self._materialize_target(step.target))
        if resolution.status != "resolved":
            raise _StepFailure(
                _RESOLUTION_CODE[resolution.status],
                expected=f"exactly one element matching {step.target.description!r}",
                observed=resolution.detail or resolution.status,
                resolution=resolution,
            )
        if step.extract.source == "attribute":
            # The surface seam has no attribute reader yet (read_text covers
            # every recorded artifact); refuse loudly rather than misread.
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step.id,
                    code=Code.SURFACE_INCOMPATIBLE,
                    expected="a surface with an attribute reader",
                    observed="extract.source='attribute' is not supported by this surface",
                ),
            )
        raw = self.surface.read_text(resolution.uid)
        if step.extract.pattern is not None:
            match = re.search(step.extract.pattern, raw)
            if match is None:
                # The element resolved but its content is not what the record
                # promised — the same "right place, wrong thing" family as a
                # verify rejection.
                raise _StepFailure(
                    Code.VERIFY_FAILED,
                    expected=f"text matching extract pattern {step.extract.pattern!r}",
                    observed=f"read {raw!r}",
                    resolution=resolution,
                )
            raw = match.group(1)
        declared = self._capability.outputs[step.output].type
        try:
            self._outputs[step.output] = _coerce_output(declared, raw)
        except ValueError:
            raise _StepFailure(
                Code.VERIFY_FAILED,
                expected=f"a {declared.value} value for output '{step.output}'",
                observed=f"read {raw!r}",
                resolution=resolution,
            )
        return resolution

    # ------------------------------------------------------------- outcomes

    def _detect_outcome(self) -> DeclaredOutcome | None:
        """The first declared outcome whose detect condition currently holds —
        artifact order is the priority order."""
        for outcome in self._capability.outcomes:
            if self._condition_holds(outcome.detect, timeout_s=_QUICK_TIMEOUT_S):
                return outcome
        return None

    def _terminal_for_outcome(self, outcome: DeclaredOutcome, step_id: str | None) -> None:
        """Terminal in the default: raises _Terminal. With an escalation
        handler, a HARD_FAILURE-tier ESCALATABLE outcome is handed to a human
        first — whose decision can make this RETURN (the condition was
        cleared; the caller resumes its loop) or raise _SkipStep."""
        self._event(
            "outcome_detected",
            outcome=outcome.id,
            code=outcome.code.value,
            disposition=outcome.disposition.value,
            step=step_id,
        )
        if outcome.disposition is Tier.BUSINESS_OUTCOME:
            raise _Terminal(
                Tier.BUSINESS_OUTCOME,
                outcome=OutcomeReport(
                    outcome_id=outcome.id, code=outcome.code, message=outcome.message
                ),
            )
        if outcome.disposition is Tier.HARD_FAILURE:
            decision = self._maybe_escalate(
                step_id,
                outcome.code,
                "the flow to proceed past this declared condition",
                f"declared outcome '{outcome.id}': {outcome.message}",
            )
            if decision is ResumeDecision.ABORT:
                raise _Terminal(
                    Tier.HARD_FAILURE,
                    failure=FailureDetail(
                        step_id=step_id,
                        code=Code.ABORTED_BY_OPERATOR,
                        expected="the operator to clear the blocking condition and resume",
                        observed=self._abort_observed(),
                    ),
                )
            if decision is ResumeDecision.SKIP_STEP:
                raise _SkipStep("skipped by operator")
            if decision is ResumeDecision.CONTINUE:
                # Uniform meaning everywhere: CONTINUE = "I completed the
                # pending step by hand — go on from the NEXT step". Re-running
                # the pending step here could double-post a mutation the human
                # already made.
                raise _SkipStep("completed by operator")
            if decision is ResumeDecision.RETRY_STEP:
                # "I cleared the obstacle — run the step again": re-scan ONCE;
                # the same hard outcome still detecting fails hard below, and
                # the once-per-step guard forbids a second escalation.
                still = self._detect_outcome()
                if still is None or still.id != outcome.id:
                    return  # cleared — the caller resumes its loop
        # HARD_FAILURE-tier outcomes terminate directly. A RECOVERABLE outcome
        # reaches here only with its retry budget spent: the status says how
        # the run ended, the code says which condition ended it.
        raise _Terminal(
            Tier.HARD_FAILURE,
            failure=FailureDetail(
                step_id=step_id,
                code=outcome.code,
                expected="the flow to proceed past this declared condition",
                observed=f"declared outcome '{outcome.id}': {outcome.message}",
            ),
        )

    def _recover(self, outcome: DeclaredOutcome) -> None:
        """Run an outcome's recovery steps: materialize + resolve + act,
        policy-checked like flow steps, never recorded as flow telemetry or
        outputs. All recovery steps are SAFE by schema, so the mutation
        confirmation gate does not apply."""
        self._event(
            "outcome_detected",
            outcome=outcome.id,
            code=outcome.code.value,
            disposition=outcome.disposition.value,
            step=self._current_step_id,
        )
        self._event("recovery_started", outcome=outcome.id, code=outcome.code.value)
        for step in outcome.recovery:
            if step.target is None:
                path = substitute(step.value or "", self._params)
                self.gate.check_url(urljoin(self._base_url + "/", path.lstrip("/")))
                self.gate.check_action(ActionType.NAVIGATE)
                self.surface.navigate(path)
                continue
            resolution = self.surface.resolve(self._materialize_target(step.target))
            if resolution.status != "resolved":
                # A recovery that cannot find its control has not recovered;
                # the step's retry budget decides what happens next.
                self._event(
                    "recovery_step_unresolved",
                    outcome=outcome.id,
                    step=step.id,
                    detail=resolution.detail or resolution.status,
                )
                return
            self.gate.check_action(step.action)
            value = substitute(step.value, self._params) if step.value is not None else None
            self.surface.act(resolution.uid, step.action, value)
            self.gate.check_url(self.surface.current_url())
        self._event("recovery_finished", outcome=outcome.id)

    # ------------------------------------------------------------ escalation

    def _escalation_applies(self, step_id: str | None, code: Code) -> bool:
        """One intervention per step per run: a human who already answered for
        this step is not paged again — which is also what keeps RETRY_STEP
        (and 'CONTINUE but the condition persists') from looping forever."""
        return (
            self.escalation is not None
            and code in ESCALATABLE
            and not self._escalated_steps.get(step_id, False)
        )

    def _maybe_escalate(
        self, step_id: str | None, code: Code, expected: str, observed: str
    ) -> ResumeDecision | None:
        """Hand a stuck run to a human, if one is configured and the condition
        warrants it. Returns the operator's decision, or None when escalation
        does not apply (caller keeps its default behavior). Raises _Terminal
        on an unanswered deadline."""
        if not self._escalation_applies(step_id, code):
            return None
        self._escalated_steps[step_id] = True
        self._token.fire(ControlEvent.ESCALATE)
        self._intervention_count += 1
        n = self._intervention_count
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=self._capability.limits.escalation_timeout_s)

        dom_snapshot_path: str | None = None
        if self._log is not None:
            dom_file = self._log.dir / f"intervention-{n}.html"
            try:
                dom_file.write_text(
                    self._redactor.redact(self.surface.dom_snapshot()), encoding="utf-8"
                )
                dom_snapshot_path = repo_relative(dom_file)
            except Exception:
                dom_snapshot_path = None  # a dead browser must not mask the escalation

        request = InterventionRequest(
            id=uuid.uuid4().hex,
            run_id=self._run_id,
            capability_id=self._capability.id,
            step_id=step_id,
            url=self.surface.current_url(),
            reason_code=code,
            message=f"{expected} | {observed}",
            screenshot_path=self._screenshot(f"intervention-{n}"),
            dom_snapshot_path=dom_snapshot_path,
            requested_at=now,
            deadline_at=deadline,
        )
        self._event(
            "escalation_raised",
            intervention=request.id,
            step=step_id,
            code=code.value,
            url=request.url,
            deadline_at=deadline.isoformat(),
        )
        # The human drives the SAME surface through the SAME gate with the
        # SAME redactor and evidence log as automation — that is the audit
        # story, and the engine's own ControlToken referees the handoff.
        session = OperatorSession(
            surface=self.surface,
            gate=self.gate,
            log=self._log,
            token=self._token,
            redactor=self._redactor,
            deadline=deadline,
        )
        try:
            decision = ResumeDecision(self.escalation.handle(request, session))
        except EscalationTimeout as exc:
            # TIMEOUT is an edge out of PAUSED only — a handler that never
            # engaged. A human who took control and then let the deadline
            # lapse abandoned a held session: that transition is an ABORT,
            # and the recorded history says which happened. (Deviation from
            # the literal "fire TIMEOUT" spec: the state machine has no
            # HUMAN_CONTROL--timeout--> edge, deliberately.)
            event = (
                ControlEvent.TIMEOUT
                if self._token.state is ControlState.PAUSED
                else ControlEvent.ABORT
            )
            self._token.fire(event)
            self._event(
                "escalation_timeout",
                intervention=request.id,
                detail=str(exc),
                token_history=_token_history(self._token),
            )
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step_id,
                    code=Code.ESCALATION_TIMEOUT,
                    expected="an operator decision before the deadline",
                    observed=f"deadline {deadline.isoformat()} passed unanswered: {exc}",
                ),
            )
        except Exception as exc:
            # A crashed handler (or a garbage return value) must not skip the
            # bookkeeping: the token reaches a terminal state, the intervention
            # is recorded with a synthesized ABORT carrying whatever the human
            # DID do, and the run reports the handler fault.
            try:
                self._token.fire(
                    ControlEvent.TIMEOUT
                    if self._token.state is ControlState.PAUSED
                    else ControlEvent.ABORT
                )
            except IllegalTransition:
                pass  # a handler that mangled the token is part of the fault
            self._escalations.append(
                InterventionRecord(
                    request=request,
                    actions=list(session.actions),
                    decision=ResumeDecision.ABORT,
                    resolved_at=datetime.now(timezone.utc),
                    operator=self.operator_name,
                )
            )
            self._event(
                "escalation_failed",
                intervention=request.id,
                detail=f"{type(exc).__name__}: {exc}",
                token_history=_token_history(self._token),
            )
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step_id,
                    code=Code.EXECUTION_ERROR,
                    expected="the escalation handler to return a decision",
                    observed=f"handler failed: {type(exc).__name__}: {exc}",
                ),
            )
        if decision is ResumeDecision.ABORT:
            self._token.fire(ControlEvent.ABORT)
        else:
            self._token.fire(ControlEvent.RESUME, decision)
        self._escalations.append(
            InterventionRecord(
                request=request,
                actions=list(session.actions),
                decision=decision,
                resolved_at=datetime.now(timezone.utc),
                operator=self.operator_name,
            )
        )
        self._event(
            "escalation_resolved",
            intervention=request.id,
            decision=decision.value,
            token_history=_token_history(self._token),
        )
        return decision

    def _apply_step_decision(self, decision: ResumeDecision, step: Step, record) -> bool:
        """Turn an operator decision at a budget-exhausted step exit into
        control flow: True re-runs the step from scratch, False moves on to
        the next step, ABORT raises the terminal failure."""
        if decision is ResumeDecision.ABORT:
            raise _Terminal(
                Tier.HARD_FAILURE,
                failure=FailureDetail(
                    step_id=step.id,
                    code=Code.ABORTED_BY_OPERATOR,
                    expected="the operator to clear the blocking condition and resume",
                    observed=self._abort_observed(),
                ),
            )
        if decision is ResumeDecision.RETRY_STEP:
            # Re-observe FIRST: after a human touched the page nothing is
            # assumed — the fresh pass re-resolves everything from scratch.
            try:
                self.surface.observe()
            except Exception:
                pass  # a truly dead page will surface through the fresh pass
            return True
        # CONTINUE: the human completed the step manually. SKIP_STEP: moot.
        # Either way automation did not run the step — SKIPPED, with the note
        # saying which flavour.
        record(
            StepStatus.SKIPPED,
            None,
            note=(
                "completed by operator"
                if decision is ResumeDecision.CONTINUE
                else "skipped by operator"
            ),
        )
        return False

    def _abort_observed(self) -> str:
        """Observed text for an operator abort, carrying the operator's
        note(s) when any were recorded during the intervention."""
        base = "the operator aborted the run"
        if self._escalations:
            notes = [
                action.description.removeprefix(NOTE_PREFIX)
                for action in self._escalations[-1].actions
                if action.description.startswith(NOTE_PREFIX)
            ]
            if notes:
                return base + " — " + "; ".join(notes)
        return base

    # ------------------------------------------------------------ conditions

    def _condition_holds(self, condition: StateCondition, timeout_s: float) -> bool:
        if condition.text_visible is not None:
            text = substitute(condition.text_visible, self._params)
            if not self.surface.find_text(text, timeout_s=timeout_s):
                return False
        if condition.text_absent is not None:
            text = substitute(condition.text_absent, self._params)
            if self.surface.find_text(text, timeout_s=_QUICK_TIMEOUT_S):
                return False
        if condition.url_path_matches is not None:
            # Binding values are escaped before the stored regex compiles, so
            # a member id can never smuggle metacharacters into the pattern.
            escaped = {name: re.escape(value) for name, value in self._params.items()}
            pattern = substitute(condition.url_path_matches, escaped)
            if re.search(pattern, self.surface.current_path()) is None:
                return False
        if condition.element_visible is not None:
            target = self._materialize_target(condition.element_visible)
            if self.surface.resolve(target).status != "resolved":
                return False
        return True

    # -------------------------------------------------------------- plumbing

    def _materialize_target(self, target: Target) -> Target:
        """A deep copy with bindings substituted into every executable string
        — the recorded artifact itself is never mutated."""
        resolved = target.model_copy(deep=True)
        for rung in resolved.ladder:
            for attr in _RUNG_STRING_FIELDS:
                value = getattr(rung, attr, None)
                if isinstance(value, str):
                    setattr(rung, attr, substitute(value, self._params))
        if resolved.verify.text_contains is not None:
            resolved.verify.text_contains = substitute(
                resolved.verify.text_contains, self._params
            )
        for ref in resolved.frame:
            if ref.name is not None:
                ref.name = substitute(ref.name, self._params)
            if ref.url_path is not None:
                ref.url_path = substitute(ref.url_path, self._params)
        return resolved

    def _param_problems(self, capability: Capability) -> list[str]:
        """Every violation of the input contract, not just the first. Values
        never appear in the messages — they may be sensitive and the run's
        redactor does not exist yet at this point."""
        problems: list[str] = []
        for name in sorted(set(self._params) - set(capability.inputs)):
            problems.append(f"unknown param '{name}'")
        for name, decl in capability.inputs.items():
            if name not in self._params:
                if decl.required:
                    problems.append(f"missing required param '{name}'")
                else:
                    # The artifact validator guarantees every declared input is
                    # referenced by an executable binding, so an unsupplied
                    # optional would KeyError mid-run with the browser open.
                    # Refuse pre-flight instead.
                    problems.append(
                        f"optional param '{name}' was not supplied but the flow "
                        "references it — supply a value"
                    )
                continue
            value = self._params[name]
            if not isinstance(value, str):
                problems.append(f"param '{name}' must be a string (got {type(value).__name__})")
                continue
            if decl.pattern is not None and re.fullmatch(decl.pattern, value) is None:
                problems.append(f"param '{name}' does not match pattern {decl.pattern!r}")
            coercion = _coercion_problem(decl.type, value)
            if coercion is not None:
                problems.append(f"param '{name}' {coercion}")
        return problems

    def _page_hint(self) -> str:
        try:
            observation = self.surface.observe()
            snippet = " ".join(observation.text.split())[:160]
            return f"; page '{observation.title.strip()}': {snippet}"
        except Exception:
            return ""  # a hint is a courtesy, never a failure source

    def _screenshot(self, label: str) -> str | None:
        if self._log is None:
            return None  # the run never got far enough to open evidence
        path = self._log.screenshot_path(label)
        try:
            self.surface.screenshot(path)
            return repo_relative(path)
        except Exception:
            return None  # a dead browser must not mask the real failure

    def _event(self, kind: str, **payload) -> None:
        if self._log is not None:
            self._log.event(kind, **payload)


# ------------------------------------------------------------------ helpers


def _coercion_problem(input_type: InputType, value: str) -> str | None:
    """Params always travel as strings; this checks the string coerces to the
    declared type. The string form is what substitution uses either way."""
    try:
        if input_type is InputType.INTEGER:
            int(value)
        elif input_type is InputType.NUMBER:
            float(value)
        elif input_type is InputType.BOOLEAN:
            if value.strip().lower() not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(value)
    except (TypeError, ValueError):
        return f"is not a valid {input_type.value}"
    return None


def _coerce_output(output_type: OutputType, raw: str) -> str | int | float | bool:
    if output_type is OutputType.INTEGER:
        return int(raw.strip())
    if output_type is OutputType.NUMBER:
        return float(raw.strip())
    if output_type is OutputType.BOOLEAN:
        lowered = raw.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    # money / date keep the displayed string: replay reports what the app
    # showed; interpretation is the caller's business.
    return raw


def _describe_condition(condition: StateCondition) -> str:
    parts: list[str] = []
    if condition.text_visible is not None:
        parts.append(f"text visible {condition.text_visible!r}")
    if condition.text_absent is not None:
        parts.append(f"text absent {condition.text_absent!r}")
    if condition.url_path_matches is not None:
        parts.append(f"path matches {condition.url_path_matches!r}")
    if condition.element_visible is not None:
        parts.append(f"element visible: {condition.element_visible.description}")
    return ", ".join(parts)


def _token_history(token: ControlToken) -> list[dict[str, str | None]]:
    """The control token's transition log in evidence-friendly form — how an
    auditor (or a test) sees exactly how ownership moved during a handoff."""
    return [
        {
            "from": before.value,
            "event": event.value,
            "to": after.value,
            "decision": decision.value if decision is not None else None,
        }
        for before, event, after, decision in token.history
    ]


def _terminal_note(terminal: _Terminal) -> str | None:
    if terminal.outcome is not None:
        return f"declared outcome '{terminal.outcome.outcome_id}' ({terminal.outcome.code.value})"
    if terminal.failure is not None:
        return terminal.failure.code.value
    return None
