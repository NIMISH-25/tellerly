"""The planner seam and its Gemini implementation.

The load-bearing rule: **the planner never writes selectors.** It is shown
controls as human-meaningful facts (role, label, name, the row a control sits
in) referenced by ephemeral uids, and it enters input values as
``{{input.name}}`` placeholders — so it cannot emit a selector and it never
sees a sensitive value. One tool call per turn.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from tellerly.schema import Economics
from tellerly.surface.base import ControlFacts, PageObservation


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any]


class Planner(ABC):
    @abstractmethod
    def first(self, system: str, observation: PageObservation) -> ToolCall: ...

    @abstractmethod
    def next(self, tool_result: str, observation: PageObservation | None) -> ToolCall: ...

    @abstractmethod
    def economics(self) -> Economics: ...


# ------------------------------------------------------------- rendering


def render_control(control: ControlFacts) -> str:
    bits = [f"{control.uid}: [{control.role or control.kind}]"]
    if control.label:
        bits.append(f'label="{control.label}"')
    elif control.accessible_name:
        bits.append(f'"{control.accessible_name}"')
    elif control.text:
        bits.append(f'text="{control.text}"')
    if control.anchor_text and not control.label:
        bits.append(f'row-label="{control.anchor_text}"')
    if control.options:
        bits.append("options=[" + "; ".join(control.options) + "]")
    if control.value:
        bits.append(f'current="{control.value}"')
    if control.frame:
        frame_name = control.frame[-1].name or control.frame[-1].url_path or "sub"
        bits.append(f"frame={frame_name}")
    return " ".join(bits)


def render_observation(observation: PageObservation) -> str:
    lines = [
        f"PAGE: {observation.path}  (title: {observation.title})",
        "VISIBLE TEXT (trimmed):",
        observation.text[:2500],
        "",
        "CONTROLS — reference by uid, valid for THIS observation only:",
    ]
    lines += [render_control(control) for control in observation.controls]
    return "\n".join(lines)


# ------------------------------------------------------------ Gemini planner

_FUNCTIONS: list[dict] = [
    {
        "name": "navigate",
        "description": "Go to an app-relative path on the same site (must start with /).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "why": {"type": "string"}},
            "required": ["path", "why"],
        },
    },
    {
        "name": "act",
        "description": (
            "Perform one action on one control from the latest observation. "
            "When entering an input value, ALWAYS pass its placeholder "
            "{{input.name}} — never the raw value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "control": {"type": "string", "description": "uid, e.g. c3"},
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "select", "press"],
                },
                "value": {
                    "type": "string",
                    "description": "For fill/select/press. Use {{input.name}} for inputs.",
                },
                "why": {"type": "string"},
            },
            "required": ["control", "action", "why"],
        },
    },
    {
        "name": "assert_state",
        "description": (
            "Assert the app reached an expected state. A held assertion becomes "
            "a replay checkpoint. Use after each meaningful transition, and once "
            "as the final proof the goal state was reached."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "text_visible": {"type": "string"},
                "url_path_contains": {
                    "type": "string",
                    "description": "May use {{input.name}} placeholders.",
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "read_value",
        "description": (
            "Read a declared output's value from the screen. Values on these "
            "consoles sit in a cell right after their label cell — pass the "
            "exact label text as the anchor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor": {
                    "type": "string",
                    "description": "Exact label text next to the value, e.g. 'Confirmation No.'",
                },
                "output": {"type": "string", "description": "A declared output name."},
                "why": {"type": "string"},
            },
            "required": ["anchor", "output", "why"],
        },
    },
    {
        "name": "finish",
        "description": "Declare the goal complete. Rejected until every declared output is captured and a final assertion has held.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "give_up",
        "description": "Declare the goal unreachable and stop.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

# Paid-tier list prices per million tokens; overridable. The free tier bills
# $0 — the cost line is what the same run WOULD cost, which is the honest
# number for the record-once/replay-many comparison.
_DEFAULT_PRICE_IN = 1.50
_DEFAULT_PRICE_OUT = 9.00


class GeminiPlanner(Planner):
    def __init__(
        self,
        api_key: str,
        model: str,
        price_in_per_mtok: float = _DEFAULT_PRICE_IN,
        price_out_per_mtok: float = _DEFAULT_PRICE_OUT,
        throttle_s: float = 0.0,
    ) -> None:
        from google import genai

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self._price_in = price_in_per_mtok
        self._price_out = price_out_per_mtok
        self._throttle_s = throttle_s
        self._chat = None
        self._last_tool: str | None = None
        self._nudged = False
        self._calls = 0
        self._tokens_in = 0
        self._tokens_out = 0

    def first(self, system: str, observation: PageObservation) -> ToolCall:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            tools=[types.Tool(function_declarations=_FUNCTIONS)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        )
        self._chat = self._client.chats.create(model=self.model, config=config)
        return self._send(render_observation(observation))

    def next(self, tool_result: str, observation: PageObservation | None) -> ToolCall:
        from google.genai import types

        parts = [
            types.Part.from_function_response(
                name=self._last_tool or "act", response={"result": tool_result}
            )
        ]
        if observation is not None:
            parts.append(types.Part.from_text(text=render_observation(observation)))
        return self._send(parts)

    def economics(self) -> Economics:
        return Economics(
            llm_calls=self._calls,
            input_tokens=self._tokens_in,
            output_tokens=self._tokens_out,
            cost_usd=round(
                self._tokens_in / 1e6 * self._price_in
                + self._tokens_out / 1e6 * self._price_out,
                6,
            ),
        )

    # ------------------------------------------------------------- internals

    def _send(self, message) -> ToolCall:
        from google.genai import errors

        if self._throttle_s:
            time.sleep(self._throttle_s)
        attempts = 0
        while True:
            try:
                response = self._chat.send_message(message)
                break
            except errors.APIError as exc:
                if exc.code == 429 and "PerDay" in str(exc):
                    # A daily quota does not recover by waiting minutes —
                    # fail fast with the fix in the message.
                    raise RuntimeError(
                        f"daily free-tier quota for '{self.model}' is exhausted; "
                        "switch models via TELLERLY_GEMINI_MODEL or retry tomorrow"
                    ) from exc
                attempts += 1
                retriable = exc.code in (429, 500, 502, 503)
                if not retriable or attempts > 4:
                    raise
                time.sleep(20 * attempts)  # free-tier rate limits are per-minute
        self._calls += 1
        usage = response.usage_metadata
        if usage is not None:
            self._tokens_in += usage.prompt_token_count or 0
            self._tokens_out += usage.candidates_token_count or 0
        calls = response.function_calls or []
        if not calls:
            # ONE corrective nudge; a planner that persistently talks instead
            # of acting must fail cleanly, not loop through quota.
            if self._nudged:
                raise RuntimeError(
                    "planner answered in prose twice in a row instead of calling a tool"
                )
            self._nudged = True
            try:
                return self._send(
                    "You must respond with exactly one tool call. Choose your next single action."
                )
            finally:
                self._nudged = False
        call = calls[0]  # one action per turn: extras are ignored, by design
        self._last_tool = call.name
        return ToolCall(tool=call.name, args=dict(call.args or {}))


# ----------------------------------------------------------- scripted planner


class ScriptedPlanner(Planner):
    """Deterministic planner for tests: a list of functions, each given the
    latest observation and the last tool result, returning the next call.
    Proves the engine/recorder/compiler pipeline with no model involved."""

    def __init__(
        self,
        script: list[Callable[[PageObservation | None, str], ToolCall]],
    ) -> None:
        self._script = list(script)
        self._index = 0

    def first(self, system: str, observation: PageObservation) -> ToolCall:
        return self._step(observation, "")

    def next(self, tool_result: str, observation: PageObservation | None) -> ToolCall:
        return self._step(observation, tool_result)

    def economics(self) -> Economics:
        return Economics()

    def _step(self, observation: PageObservation | None, tool_result: str) -> ToolCall:
        if self._index >= len(self._script):
            return ToolCall(tool="give_up", args={"reason": "script exhausted"})
        step = self._script[self._index]
        self._index += 1
        return step(observation, tool_result)
