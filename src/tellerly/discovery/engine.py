"""The discovery engine: observe → decide → act, with recording on the side.

The engine owns everything the planner is not trusted with: policy checks
before every action, placeholder substitution (so sensitive values never
reach the model), locator measurement at the moment of action, checkpoint
verification, the stopping conditions, and the evidence trail.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from tellerly.discovery.compiler import CompileError, compile_capability, load_outcome_catalog
from tellerly.discovery.job import JobSpec
from tellerly.discovery.planner import Planner, ToolCall
from tellerly.discovery.recorder import RecorderError, classify_risk, measure_target
from tellerly.kernel.evidence import RunLog
from tellerly.kernel.guardrails import PolicyGate, PolicyViolation
from tellerly.kernel.redaction import Redactor
from tellerly.kernel.store import CapabilityStore
from tellerly.schema import (
    ActionType,
    ActStep,
    CheckpointStep,
    DeclaredOutcome,
    DiscoveryResult,
    DiscoveryStatus,
    Provenance,
    ReadStep,
    Risk,
    Sensitivity,
    StateCondition,
    Tier,
)
from tellerly.schema.artifact import Step
from tellerly.schema.bindings import (
    escape_regex_outside_bindings,
    mask_bindings,
    referenced_inputs,
    substitute,
)
from tellerly.surface.base import PageObservation, Surface

_REPEAT_LIMIT = 3


class DiscoveryEngine:
    def __init__(
        self,
        surface: Surface,
        planner: Planner,
        job: JobSpec,
        gate: PolicyGate,
        base_url: str,
        evidence_root: Path,
        store: CapabilityStore,
        outcome_catalog_path: Path,
        max_turns: int = 40,
    ) -> None:
        self.surface = surface
        self.planner = planner
        self.job = job
        self.gate = gate
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.max_turns = max_turns
        # Loaded up front: the engine needs the catalogue DURING the run to
        # tell recovery actions apart from flow steps.
        self.outcome_catalog: list[DeclaredOutcome] = load_outcome_catalog(
            outcome_catalog_path, job.app_id
        )

        self.redactor = Redactor()
        self.values = job.runtime_values()
        for name, spec in job.inputs.items():
            if spec.sensitivity is not Sensitivity.NONE:
                self.redactor.register(name, self.values[name])

        self.run_id = (
            f"discovery-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        )
        self.log = RunLog(evidence_root, self.run_id, self.redactor)

        self.steps: list[Step] = []
        self.captured: dict[str, str] = {}
        self.checkpoints_held = 0
        self.visited_hosts: set[str] = set()
        self.performed_actions: set[ActionType] = set()
        self._step_seq = 0
        self._observation: PageObservation | None = None
        self._last_call_key: str | None = None
        self._repeat_streak = 0

    # ------------------------------------------------------------------ run

    def run(self) -> DiscoveryResult:
        started = time.monotonic()
        status = DiscoveryStatus.BUDGET_EXHAUSTED
        summary = ""
        try:
            entry_url = urljoin(self.base_url + "/", self.job.entry.lstrip("/"))
            self.gate.check_url(entry_url)
            self.visited_hosts.add(urlparse(entry_url).netloc)
            self.surface.open(entry_url)
            self._observation = self.surface.observe()
            self.log.event(
                "run_started",
                goal=self.job.goal,
                entry=entry_url,
                max_turns=self.max_turns,
            )
            call = self.planner.first(self._system_prompt(), self._planner_view())

            for turn in range(1, self.max_turns + 1):
                self.log.event("planner_call", turn=turn, tool=call.tool, args=call.args)
                if self._is_repeating(call):
                    status, summary = DiscoveryStatus.STUCK_REPEATING, (
                        f"same call repeated {_REPEAT_LIMIT} times: {call.tool}"
                    )
                    break

                result_text, terminal = self._dispatch(call)
                self.surface.screenshot(self.log.screenshot_path(f"turn-{turn:02d}"))
                self.log.event("tool_result", turn=turn, result=result_text)

                if terminal is not None:
                    status, summary = terminal, result_text
                    break
                call = self.planner.next(result_text, self._planner_view())
            else:
                summary = f"turn budget of {self.max_turns} exhausted"
        except Exception as exc:
            # A dead model or dead browser must still leave a clean result and
            # a complete evidence trail — the run failed, the record didn't.
            status = DiscoveryStatus.GAVE_UP
            summary = f"aborted: {type(exc).__name__}: {exc}"
            self.log.event("run_aborted", error=summary)
        finally:
            wall = time.monotonic() - started

        economics = self.planner.economics().model_copy(update={"wall_time_s": round(wall, 2)})
        artifact_path: str | None = None

        if status is DiscoveryStatus.GOAL_MET:
            try:
                artifact_path = str(self._compile_and_save(economics))
            except CompileError as exc:
                status, summary = DiscoveryStatus.GAVE_UP, f"compile refused: {exc}"
            except Exception as exc:
                status, summary = (
                    DiscoveryStatus.GAVE_UP,
                    f"compile/persist failed: {type(exc).__name__}: {exc}",
                )
                self.log.event("run_aborted", error=summary)

        result = DiscoveryResult(
            run_id=self.run_id,
            goal=self.job.goal,
            status=status,
            artifact_path=artifact_path,
            steps_taken=len(self.steps),
            economics=economics,
            evidence_dir=str(self.log.dir),
        )
        self.log.write_json(
            "trace.json", {"steps": [step.model_dump(mode="json") for step in self.steps]}
        )
        self.log.write_json("result.json", result)
        self.log.event("run_finished", status=status.value, summary=summary)
        return result

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, call: ToolCall) -> tuple[str, DiscoveryStatus | None]:
        try:
            handler = {
                "navigate": self._do_navigate,
                "act": self._do_act,
                "assert_state": self._do_assert,
                "read_value": self._do_read,
                "finish": self._do_finish,
                "give_up": self._do_give_up,
            }.get(call.tool)
            if handler is None:
                return f"ERROR: unknown tool '{call.tool}'", None
            return handler(call.args)
        except (PolicyViolation, RecorderError, KeyError, ValueError) as exc:
            return f"ERROR: {exc}", None
        except Exception as exc:  # surface/browser errors reach the planner as facts
            return f"ERROR: the action failed: {type(exc).__name__}: {exc}", None

    def _do_navigate(self, args: dict) -> tuple[str, None]:
        path = args["path"]
        if not path.startswith("/"):
            return "ERROR: navigate takes an app-relative path starting with /", None
        resolved = substitute(path, self.values)
        url = urljoin(self.base_url + "/", resolved.lstrip("/"))
        self.gate.check_url(url)
        self.gate.check_action(ActionType.NAVIGATE)
        self.surface.navigate(resolved)
        self.visited_hosts.add(urlparse(url).netloc)
        self.performed_actions.add(ActionType.NAVIGATE)
        self._record(
            ActStep(id=self._step_id("navigate"), action=ActionType.NAVIGATE, value=path)
        )
        self._observation = self.surface.observe()
        return f"navigated{self._pending_note()}", None

    def _do_act(self, args: dict) -> tuple[str, None]:
        facts = self._facts(args["control"])
        action = ActionType(args["action"])
        self.gate.check_action(action)
        raw_value = args.get("value")
        if (
            raw_value is not None
            and action in (ActionType.FILL, ActionType.SELECT)
            and referenced_inputs(raw_value)
            and mask_bindings(raw_value, "").strip()
        ):
            # "{{input.from_share}} — Regular Share (Savings)" would couple the
            # input to THIS run's display label and break every other replay.
            return (
                "ERROR: when a value comes from an input, pass ONLY its placeholder "
                "(e.g. {{input.from_share}}) with no surrounding text",
                None,
            )
        concrete = substitute(raw_value, self.values) if raw_value is not None else None

        risk = classify_risk(action, facts)
        if risk is Risk.MUTATING:
            # Discovery runs are operator-attended; the recorded step carries
            # MUTATING so unattended replay can gate it on confirmation.
            self.log.event("mutating_action", control=facts.model_dump(mode="json"))

        # An act performed while a declared recoverable condition is on screen
        # is RECOVERY, not flow — recording it would bake a transient screen
        # (e.g. a maintenance notice) into the mandatory step list and break
        # every replay where the screen does not appear.
        recovering_from = self._active_recoverable()

        # Measure BEFORE acting — after a click the page may be gone.
        target = measure_target(self.surface, facts)
        self.surface.act(facts.uid, action, concrete)
        self.performed_actions.add(action)
        # Click-through navigation must not leave the policy envelope either.
        landed_url = self.surface.current_url()
        self.gate.check_url(landed_url)
        self.visited_hosts.add(urlparse(landed_url).netloc)

        if recovering_from is not None:
            self.log.event(
                "recovery_action", outcome=recovering_from.id, target=target.description
            )
            self._observation = self.surface.observe()
            return (
                f"done: {action.value} on {target.description} (recovery for "
                f"'{recovering_from.id}' — not recorded as a flow step)"
                f"{self._pending_note()}",
                None,
            )

        self._record(
            ActStep(
                id=self._step_id(f"{action.value}-{_slug(target.description)}"),
                action=action,
                target=target,
                value=raw_value,
                risk=risk,
            )
        )
        self._observation = self.surface.observe()
        return f"done: {action.value} on {target.description}{self._pending_note()}", None

    def _do_assert(self, args: dict) -> tuple[str, None]:
        description = args["description"]
        text_visible = args.get("text_visible")
        url_contains = args.get("url_path_contains")
        if not text_visible and not url_contains:
            return "ERROR: assert_state needs text_visible and/or url_path_contains", None

        holds = True
        observed = []
        if text_visible is not None:
            found = self.surface.find_text(substitute(text_visible, self.values))
            holds &= found
            observed.append(f"text {'found' if found else 'NOT found'}: '{text_visible}'")
        if url_contains is not None:
            expected = substitute(url_contains, self.values)
            actual = self.surface.current_path()
            matched = expected in actual
            holds &= matched
            observed.append(f"path is '{actual}'")

        if not holds:
            return f"ASSERTION FAILED ({description}): " + "; ".join(observed), None

        self._record(
            CheckpointStep(
                id=self._step_id("checkpoint"),
                description=description,
                condition=StateCondition(
                    text_visible=text_visible,
                    # Verified above as a substring; stored as a regex — escape
                    # everything except the bindings so both mean the same thing.
                    url_path_matches=(
                        escape_regex_outside_bindings(url_contains)
                        if url_contains is not None
                        else None
                    ),
                ),
            )
        )
        self.checkpoints_held += 1
        return f"checkpoint held: {description}", None

    def _do_read(self, args: dict) -> tuple[str, None]:
        output = args["output"]
        if output not in self.job.outputs:
            declared = ", ".join(self.job.outputs) or "none"
            return f"ERROR: '{output}' is not a declared output (declared: {declared})", None
        anchor = substitute(args["anchor"], self.values)
        facts = self.surface.locate_value_cell(anchor)
        if facts is None:
            return f"ERROR: no value cell found after a label reading exactly '{anchor}'", None
        target = measure_target(self.surface, facts)
        value = facts.text or ""
        self._record(
            ReadStep(id=self._step_id(f"read-{_slug(output)}"), output=output, target=target)
        )
        self.captured[output] = value
        return f"read {output} = {value!r}", None

    def _do_finish(self, args: dict) -> tuple[str, DiscoveryStatus | None]:
        missing = sorted(set(self.job.outputs) - set(self.captured))
        if missing:
            return f"ERROR: cannot finish — outputs not captured yet: {', '.join(missing)}", None
        if self.checkpoints_held == 0:
            return "ERROR: cannot finish — no assertion has held; assert the goal state first", None
        return f"finished: {args.get('summary', '')}", DiscoveryStatus.GOAL_MET

    def _do_give_up(self, args: dict) -> tuple[str, DiscoveryStatus]:
        return f"gave up: {args.get('reason', '')}", DiscoveryStatus.GAVE_UP

    # ------------------------------------------------------------- plumbing

    def _planner_view(self) -> PageObservation | None:
        """The observation as the model sees it: passed through the value-based
        redactor, so a sensitive value echoed into a plain text control (legacy
        consoles rarely use password inputs) never reaches the model."""
        if self._observation is None:
            return None
        return PageObservation.model_validate(
            self.redactor.redact_object(self._observation.model_dump(mode="python"))
        )

    def _active_recoverable(self) -> DeclaredOutcome | None:
        for outcome in self.outcome_catalog:
            if (
                outcome.disposition is Tier.RECOVERABLE
                and outcome.detect.text_visible
                and self.surface.find_text(outcome.detect.text_visible, timeout_s=0.2)
            ):
                return outcome
        return None

    def _pending_note(self) -> str:
        """Keeps the contract in front of the planner: declared outputs that are
        still uncaptured. Without it, small models wander off receipts that are
        right in front of them."""
        missing = sorted(set(self.job.outputs) - set(self.captured))
        if not missing:
            return ""
        return (
            " | REMINDER: outputs still to capture with read_value before finish: "
            + ", ".join(missing)
            + " (if the value is on screen now, read it now — do not navigate away)"
        )

    def _facts(self, uid: str):
        if self._observation is None:
            raise KeyError("no observation yet")
        facts = self._observation.control(uid)
        if facts is None:
            raise KeyError(
                f"unknown control '{uid}' — reference uids from the latest observation only"
            )
        return facts

    def _record(self, step: Step) -> None:
        self.steps.append(step)
        self.log.event("step_recorded", step=step.model_dump(mode="json"))

    def _step_id(self, base: str) -> str:
        self._step_seq += 1
        return f"s{self._step_seq:02d}-{base}"

    def _is_repeating(self, call: ToolCall) -> bool:
        key = call.tool + json.dumps(call.args, sort_keys=True, default=str)
        if key == self._last_call_key:
            self._repeat_streak += 1
        else:
            self._last_call_key, self._repeat_streak = key, 1
        return self._repeat_streak >= _REPEAT_LIMIT

    def _system_prompt(self) -> str:
        input_lines = []
        for name, spec in self.job.inputs.items():
            if spec.sensitivity is Sensitivity.NONE:
                hint = f"resolves to '{self.values[name]}'"
            else:
                hint = f"{spec.sensitivity.value} — value withheld; the placeholder still works"
            input_lines.append(f"- {{{{input.{name}}}}}: {spec.description} ({hint})")
        output_lines = [
            f"- {name}: {decl.description}" for name, decl in self.job.outputs.items()
        ]
        goal = substitute(
            self.job.goal,
            {
                name: (self.values[name] if spec.sensitivity is Sensitivity.NONE else f"{{{{input.{name}}}}}")
                for name, spec in self.job.inputs.items()
            },
        )
        return "\n".join(
            [
                "You are operating a bank back-office web console to complete one job,",
                "using only the tools provided. You see the page as visible text plus a",
                "list of controls with uids. You never see or write selectors.",
                "",
                f"GOAL: {goal}",
                "",
                "INPUTS — when entering one, pass the placeholder exactly as written:",
                *input_lines,
                "",
                "OUTPUTS to capture with read_value before finishing:",
                *output_lines,
                "",
                "RULES:",
                "- Exactly one tool call per turn.",
                "- Reference controls by uid from the LATEST observation only.",
                "- Enter input values as their {{input.name}} placeholder, never raw,",
                "  and never with surrounding text — for dropdowns pass the bare",
                "  placeholder, not the option's display label.",
                "- After each meaningful transition, assert_state what you expect.",
                "- Assertions become replay checkpoints for OTHER runs with OTHER",
                "  input values: assert stable page markers (headings, labels) or",
                "  {{input.name}} placeholders — never record-specific data like a",
                "  person's name or an account balance.",
                "- Dismissible notices (e.g. maintenance) can be clicked through.",
                "- If your session expires, sign in again and continue.",
                "- Before finish: assert the goal state and read every output.",
                "- If the app refuses the operation or you are truly stuck, give_up",
                "  with the reason.",
            ]
        )

    # -------------------------------------------------------------- compile

    def _compile_and_save(self, economics) -> Path:
        existing = self.store.versions(self.job.capability_id)
        if existing:
            major, minor, _ = existing[-1].split(".")
            version = f"{major}.{int(minor) + 1}.0"
        else:
            version = "1.0.0"
        capability = compile_capability(
            job=self.job,
            steps=self.steps,
            visited_hosts=self.visited_hosts,
            performed_actions=self.performed_actions,
            outcomes=self.outcome_catalog,
            version=version,
            provenance=Provenance(
                discovery_run_id=self.run_id,
                recorded_at=datetime.now(timezone.utc),
                model=getattr(self.planner, "model", None),
                discovery_economics=economics,
                notes=f"Recorded against {self.base_url}",
            ),
        )
        path = self.store.save(capability)
        self.log.event("capability_saved", path=str(path), version=capability.version)
        return path


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text.lower())
    parts = [p for p in cleaned.split("-") if p]
    slug = "-".join(parts[:4]) or "step"
    return slug if slug[0].isalpha() else f"x{slug}"
