"""The operator seam: how a human drives the SAME live session automation was
driving, without stepping outside the audit story.

The design rule is single-path: every human action goes through the same
``Surface`` and the same ``PolicyGate`` as automation. That is what makes the
handoff auditable — a human cannot click something the policy forbids, and
everything they do lands in the same evidence trail as the machine's actions.
Operator mistakes (a stale uid, a forbidden action) come back as messages,
never as crashes: the person at the console is there precisely because the
situation is already abnormal.

This module lives in the kernel and therefore inside the no-model import
graph: no model SDKs, no HTTP clients. ``rich`` is terminal furniture, not a
network client.
"""
from __future__ import annotations

import builtins
import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tellerly.kernel.control import ControlEvent, ControlState, ControlToken
from tellerly.kernel.evidence import RunLog
from tellerly.kernel.guardrails import PolicyGate, PolicyViolation
from tellerly.kernel.redaction import Redactor
from tellerly.schema import ActionType
from tellerly.schema.escalation import HumanAction, InterventionRequest, ResumeDecision
from tellerly.surface.base import PageObservation, Surface

#: The marker OperatorSession.note() prefixes descriptions with. The replay
#: engine parses it back out to surface operator notes in an ABORTED_BY_OPERATOR
#: failure — keep the two sides in sync.
NOTE_PREFIX = "note: "


class EscalationTimeout(Exception):
    """The intervention deadline passed without an operator decision."""


class OperatorSession:
    """The engine-built facade a human drives during one intervention.

    Deliberately narrow: look / act / goto / note. There is no raw-page
    escape hatch, so nothing a human does here can bypass the policy gate or
    dodge the evidence trail.
    """

    def __init__(
        self,
        surface: Surface,
        gate: PolicyGate,
        log: RunLog | None,
        token: ControlToken,
        redactor: Redactor,
        deadline: datetime,
    ) -> None:
        self.surface = surface
        self.gate = gate
        self.token = token
        self.deadline = deadline
        self.actions: list[HumanAction] = []
        self._log = log
        self._redactor = redactor

    # ------------------------------------------------------------------ verbs

    def look(self) -> PageObservation:
        """A fresh observation, redacted before it reaches human eyes or a
        terminal scrollback — sensitive input values must not resurface just
        because a person is now watching."""
        observation = self.surface.observe()
        return PageObservation.model_validate(
            self._redactor.redact_object(observation.model_dump(mode="json"))
        )

    def act(self, uid: str, action: ActionType, value: str | None = None) -> str:
        """Perform one gated action on a control from the latest look().

        Returns a short confirmation or the error text — a refusal or a stale
        uid is information for the human, not a reason to tear the loop down.
        """
        try:
            action = ActionType(action)
        except ValueError:
            return self._mistake(f"act {uid}", f"unknown action {action!r}")
        what = f"{action.value} {uid}"
        if value is not None:
            what += f" = {value}"
        refused = self._refusal(what)
        if refused is not None:
            return refused
        try:
            self.gate.check_action(action)
            self.surface.act(uid, action, value)
            # Same landing check the engine performs: the envelope covers
            # where the action actually landed, frames included.
            for url in self.surface.frame_urls():
                self.gate.check_url(url)
        except PolicyViolation as exc:
            return self._mistake(what, f"refused by policy: {exc}")
        except (KeyError, ValueError) as exc:
            # KeyError: stale/unknown uid. ValueError: action/control mismatch.
            return self._mistake(what, f"error: {exc}")
        message = f"ok: {what}"
        self._record(message)
        return message

    def goto(self, path: str) -> str:
        """Navigate to a path — the same shape as an engine NAVIGATE step:
        action gate, destination gate, then the landing check."""
        what = f"navigate {path}"
        refused = self._refusal(what)
        if refused is not None:
            return refused
        try:
            self.gate.check_action(ActionType.NAVIGATE)
            # Human navigation is same-origin path movement, like the engine's;
            # the current URL supplies the origin for the destination check.
            self.gate.check_url(urljoin(self.surface.current_url(), path))
            self.surface.navigate(path)
            for url in self.surface.frame_urls():
                self.gate.check_url(url)
        except PolicyViolation as exc:
            return self._mistake(what, f"refused by policy: {exc}")
        message = f"ok: {what}"
        self._record(message)
        return message

    def note(self, text: str) -> None:
        """Record an observation with no page effect — it travels in the
        intervention record and (via the engine) in an abort's observed text."""
        self._record(f"{NOTE_PREFIX}{text}")

    def expired(self) -> bool:
        # A naive deadline (some tests build one) is compared in naive local
        # time; the engine always passes an aware UTC deadline.
        now = (
            datetime.now(timezone.utc) if self.deadline.tzinfo is not None else datetime.now()
        )
        return now >= self.deadline

    # -------------------------------------------------------------- internals

    def _refusal(self, what: str) -> str | None:
        """The state machine is ENFORCED at the verbs, not just described:
        page actions are legal only while control is transferable (PAUSED —
        the first verb IS the engagement) or held (HUMAN_CONTROL), and only
        before the deadline. A stashed session goes dead after resume/abort."""
        if self.expired():
            return self._mistake(what, "refused: the intervention deadline has passed")
        if self.token.state not in (ControlState.PAUSED, ControlState.HUMAN_CONTROL):
            return self._mistake(
                what, f"refused: session control is '{self.token.state.value}'"
            )
        if self.token.state is ControlState.PAUSED:
            self.token.fire(ControlEvent.TAKE_CONTROL)
        return None

    def _record(self, description: str) -> None:
        # Redacted at capture, not just at disk-write: HumanActions also ride
        # inside the in-memory ReplayResult a caller inspects.
        description = self._redactor.redact(description)
        self.actions.append(
            HumanAction(at=datetime.now(timezone.utc), description=description)
        )
        if self._log is not None:
            self._log.event("human_action", description=description)

    def _mistake(self, what: str, message: str) -> str:
        # Mistakes are part of the audit story too: the record shows what the
        # human TRIED, and that the gate (or the page) said no.
        self._record(f"{what} -> {message}")
        return message


class EscalationHandler(ABC):
    """One intervention, handled: drive the session, return a decision.

    Contract:

    - Fire ``session.token`` TAKE_CONTROL when engagement starts — and only
      then. A handler that never engages leaves the token PAUSED, which is
      what lets the engine's TIMEOUT edge stay legal.
    - Never fire RESUME or ABORT — the engine owns those transitions.
    - Raise :class:`EscalationTimeout` when the deadline passes unanswered.
    """

    @abstractmethod
    def handle(
        self, request: InterventionRequest, session: OperatorSession
    ) -> ResumeDecision: ...


class ScriptedOperator(EscalationHandler):
    """A handler driven by a plain function — tests and piped demos."""

    def __init__(
        self, fn: Callable[[InterventionRequest, OperatorSession], ResumeDecision]
    ) -> None:
        self._fn = fn

    def handle(
        self, request: InterventionRequest, session: OperatorSession
    ) -> ResumeDecision:
        decision = ResumeDecision(self._fn(request, session))
        # TAKE_CONTROL fires after the script ran, not before (deviation from
        # "when engagement starts", deliberately): a script that raised
        # EscalationTimeout models "nobody picked up", so the token must still
        # read PAUSED for the engine's TIMEOUT transition to be legal. For a
        # script that decided, the instant of engagement is unobservable —
        # firing just before returning yields the same legal history.
        if session.token.state is ControlState.PAUSED:
            session.token.fire(ControlEvent.TAKE_CONTROL)
        return decision


_HELP = """\
commands:
  look                          observe the page (controls listed with uids)
  click <uid>                   click a control
  fill <uid> <value>            fill a control
  select <uid> <value>          choose a select option
  press <uid> <key>             press a key on a control
  goto <path>                   navigate to a path
  note <text>                   record a note in the intervention record
  done continue [note]          you completed the step by hand — go on
  done retry [note]             you cleared the obstacle — run the step again
  done skip [note]              the step is moot — go on without it
  done abort [note]             stop the run
  help                          show this help"""

_DECISIONS: dict[str, ResumeDecision] = {
    "continue": ResumeDecision.CONTINUE,
    "retry": ResumeDecision.RETRY_STEP,
    "skip": ResumeDecision.SKIP_STEP,
    "abort": ResumeDecision.ABORT,
}


class TerminalOperatorConsole(EscalationHandler):
    """The minimal-but-real operator surface: an interactive command loop in
    the terminal, driving the OperatorSession verbs one line at a time.

    ``input_fn`` is pluggable (default ``builtins.input``) so tests and piped
    demos can feed commands; it is called with no arguments so any zero-arg
    callable (including ``iter(lines).__next__``) works.
    """

    def __init__(
        self,
        input_fn: Callable[[], str] | None = None,
        console: Console | None = None,
    ) -> None:
        self._input = input_fn if input_fn is not None else builtins.input
        self._console = console if console is not None else Console()

    def handle(
        self, request: InterventionRequest, session: OperatorSession
    ) -> ResumeDecision:
        self._print_request(request)
        while True:
            line = self._read_line(session)
            # Shells sometimes prepend a BOM when piping into stdin (observed
            # with PowerShell string pipes); it must not mangle a command.
            for bom in ("﻿", "ï»¿"):
                line = line.removeprefix(bom)
            if session.token.state is ControlState.PAUSED:
                # Engagement starts with the first command the human actually
                # typed — a console nobody answered leaves the token PAUSED.
                session.token.fire(ControlEvent.TAKE_CONTROL)
            decision = self._dispatch(line, session)
            if decision is not None:
                return decision

    # -------------------------------------------------------------- internals

    def _read_line(self, session: OperatorSession) -> str:
        """A DEADLINE-BOUNDED read. input() blocks indefinitely, and an
        operator who walked away must not hold a live banking session open —
        the read runs on a worker thread and the wait expires with the
        intervention deadline. (The abandoned daemon thread stays parked in
        input(); the process ends regardless — the run does not.)"""
        self._check_deadline(session)
        now = (
            datetime.now(timezone.utc)
            if session.deadline.tzinfo is not None
            else datetime.now()
        )
        remaining = (session.deadline - now).total_seconds()

        result: queue.Queue = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                self._console.print("[bold cyan]operator>[/bold cyan] ", end="")
                result.put(("line", str(self._input())))
            except BaseException as exc:  # EOFError/StopIteration travel too
                result.put(("raised", exc))

        threading.Thread(target=_worker, daemon=True).start()
        try:
            kind, payload = result.get(timeout=max(remaining, 0.01))
        except queue.Empty:
            raise EscalationTimeout(
                f"no operator input before the deadline {session.deadline.isoformat()}"
            ) from None
        if kind == "raised":
            if isinstance(payload, EOFError):
                raise EscalationTimeout("operator input closed before a decision") from payload
            if isinstance(payload, StopIteration):
                # A scripted feed running dry is the piped-input analogue of EOF.
                raise EscalationTimeout("scripted input ran out before a decision") from payload
            raise payload
        # Checked again AFTER the blocking read: a command typed past the
        # deadline must not execute.
        self._check_deadline(session)
        return payload

    def _check_deadline(self, session: OperatorSession) -> None:
        if session.expired():
            raise EscalationTimeout(
                f"the intervention deadline {session.deadline.isoformat()} has passed"
            )

    def _print_request(self, request: InterventionRequest) -> None:
        body = "\n".join(
            [
                f"capability: {request.capability_id}",
                f"step:       {request.step_id or '(outside any step)'}",
                f"reason:     {request.reason_code.value}",
                f"message:    {request.message}",
                f"url:        {request.url}",
                f"screenshot: {request.screenshot_path or '—'}",
                f"dom:        {request.dom_snapshot_path or '—'}",
                f"deadline:   {request.deadline_at.isoformat()}",
            ]
        )
        # Text(), not markup: messages and paths may contain square brackets
        # (redaction markers, allowlists) that rich would parse as tags.
        self._console.print(
            Panel(Text(body), title=f"intervention {request.id}", border_style="yellow")
        )
        self._console.print(Text(_HELP))

    def _dispatch(self, line: str, session: OperatorSession) -> ResumeDecision | None:
        head, _, rest = line.strip().partition(" ")
        command = head.lower()
        rest = rest.strip()

        if command == "" or command == "help":
            self._console.print(Text(_HELP))
            return None
        if command == "look":
            self._print_observation(session.look())
            return None
        if command == "click" and rest:
            self._console.print(Text(session.act(rest, ActionType.CLICK)))
            return None
        if command in ("fill", "select", "press") and rest:
            uid, _, value = rest.partition(" ")
            action = {
                "fill": ActionType.FILL,
                "select": ActionType.SELECT,
                "press": ActionType.PRESS,
            }[command]
            self._console.print(Text(session.act(uid, action, value)))
            return None
        if command == "goto" and rest:
            self._console.print(Text(session.goto(rest)))
            return None
        if command == "note" and rest:
            session.note(rest)
            self._console.print("noted.")
            return None
        if command == "done" and rest:
            word, _, note = rest.partition(" ")
            decision = _DECISIONS.get(word.lower())
            if decision is None:
                self._console.print(Text(_HELP))
                return None
            if note.strip():
                session.note(note.strip())
            return decision

        self._console.print(Text(_HELP))  # unknown or malformed — say what works
        return None

    def _print_observation(self, observation: PageObservation) -> None:
        self._console.print(
            Text.assemble((observation.title, "bold"), f"  {observation.url}")
        )
        table = Table(show_lines=False)
        for column in ("uid", "kind", "name", "text", "value"):
            table.add_column(column)
        for control in observation.controls:
            table.add_row(
                Text(control.uid),
                Text(control.kind),
                Text(control.accessible_name or control.label or control.name_attr or ""),
                Text((control.text or control.anchor_text or "")[:40]),
                Text((control.value or "")[:20]),
            )
        self._console.print(table)
