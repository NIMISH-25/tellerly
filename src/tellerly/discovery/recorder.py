"""The recorder: turns a control the planner is about to touch into a
measured locator ladder.

At the moment of action, every plausible locator is built from the control's
observed facts and probed against the live page. Only locators that match
exactly one element — and provably *this* element — survive into the ladder.
Probing happens BEFORE the action executes: after a click the page may be
gone. The planner plays no part in this; it has never seen a selector.
"""
from __future__ import annotations

import re

from tellerly.schema import ActionType, Risk, Target, VerifyPredicate
from tellerly.schema.locators import (
    DURABILITY,
    AnchorRung,
    CssRung,
    LabelRung,
    LocatorStrategy,
    NameRung,
    RoleRung,
    Rung,
    TextRung,
)
from tellerly.surface.base import ControlFacts, Surface

#: A click is mutating when the control's own wording says it commits state.
#: Risk comes from what a step does, not what the planner calls it. Word-bounded
#: so "Payments" is not "pay"; a false positive gates one extra confirmation,
#: a false negative posts money unattended — err on the wide side.
_POSTING_VERBS = re.compile(
    r"\b(post|confirm|submit|approve|execute|pay|delete|save|transfer|send|apply|update|create)\b"
)

_MAX_ANCHOR_OFFSET = 3


class RecorderError(Exception):
    """No locator uniquely identifies the control — acting would be a guess."""


def classify_risk(action: ActionType, facts: ControlFacts) -> Risk:
    if action is not ActionType.CLICK:
        return Risk.SAFE  # typing into a form commits nothing; the click does
    wording = " ".join(
        filter(None, (facts.accessible_name, facts.text, facts.anchor_text))
    ).lower()
    if _POSTING_VERBS.search(wording):
        return Risk.MUTATING
    return Risk.SAFE


def describe(facts: ControlFacts) -> str:
    if facts.role == "button":
        return f"the {facts.accessible_name or facts.text or 'unnamed'} button"
    if facts.role == "link":
        return f"the {facts.text or 'unnamed'} link"
    handle = facts.label or facts.anchor_text or facts.name_attr or facts.kind
    return f"the {handle} {'dropdown' if facts.kind == 'select' else 'field'}"


def _tag(facts: ControlFacts) -> str:
    return facts.kind.split(":", 1)[0]


def build_candidates(facts: ControlFacts) -> list[Rung]:
    """Every plausible locator for this control, most durable first.

    Element ids are not among them — the facts don't carry one, by design.
    """
    tag = _tag(facts)
    candidates: list[Rung] = []
    if facts.role and facts.accessible_name:
        candidates.append(
            RoleRung(role=facts.role, name=facts.accessible_name, confidence=1.0)
        )
    if facts.label:
        candidates.append(LabelRung(label=facts.label, confidence=1.0))
    if facts.name_attr:
        candidates.append(NameRung(name=facts.name_attr, confidence=1.0))
    if facts.text and tag in ("a", "button"):
        candidates.append(TextRung(text=facts.text, control=tag, confidence=1.0))
    if facts.anchor_text:
        candidates.append(
            AnchorRung(anchor_text=facts.anchor_text, control=tag, confidence=1.0)
        )
    css = None
    if facts.name_attr:
        css = f'{tag}[name="{facts.name_attr}"]'
    elif facts.text and facts.kind.startswith("input:"):
        css = f'input[value="{facts.text}"]'
    if css:
        candidates.append(CssRung(css=css, confidence=1.0))
    return candidates


def measure_target(surface: Surface, facts: ControlFacts) -> Target:
    """Probe every candidate; keep the ones measured unique for THIS control."""
    survivors: list[Rung] = []
    for candidate in build_candidates(facts):
        if isinstance(candidate, AnchorRung):
            # The offset is itself measured: find which nth-after-anchor this is.
            # Value cells (td) are capped at offset 0 so the recorded rung and
            # locate_value_cell can never mean different cells.
            max_offset = 1 if _tag(facts) == "td" else _MAX_ANCHOR_OFFSET
            for offset in range(max_offset):
                probe = surface.probe(
                    candidate.model_copy(update={"offset": offset}), facts.frame, facts.uid
                )
                if probe.is_target:
                    survivors.append(candidate.model_copy(update={"offset": offset}))
                    break
            continue
        probe = surface.probe(candidate, facts.frame, facts.uid)
        if probe.count == 1 and probe.is_target:
            survivors.append(candidate)
    if not survivors:
        raise RecorderError(
            f"no locator uniquely identifies {describe(facts)} — refusing to act on a guess"
        )
    survivors.sort(
        key=lambda rung: DURABILITY[LocatorStrategy(rung.strategy)], reverse=True
    )
    # verify pins durable identity only: a button's caption is stable, but a
    # value cell's text is THIS run's value — pinning it would break replay.
    verify = VerifyPredicate(
        control=_tag(facts),
        name_attr=facts.name_attr,
        text_contains=facts.text if facts.role in ("button", "link") else None,
    )
    return Target(
        description=describe(facts),
        frame=facts.frame,
        ladder=survivors,
        verify=verify,
    )
