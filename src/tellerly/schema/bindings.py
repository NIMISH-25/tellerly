"""The closed substitution grammar for capability artifacts.

Exactly one construct exists: ``{{input.<name>}}``. Substitution is a dict
lookup — never eval, never attribute access, never format-spec tricks.
Artifacts are data that move between systems; a closed grammar is what keeps
them inert.

Bindings are legal anywhere a string describes the flow — action values AND
locator queries. That second part matters: recording a member-row link as
``text == "101555"`` silently pins the capability to one member; recording it
as ``text == "{{input.member_id}}"`` makes it a capability.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_BINDING = re.compile(r"\{\{\s*input\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_MUSTACHE = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def referenced_inputs(text: str) -> set[str]:
    """Names of inputs referenced by ``{{input.name}}`` bindings in *text*."""
    return set(_BINDING.findall(text))


def malformed_bindings(text: str) -> list[str]:
    """Binding-shaped fragments that are not valid ``{{input.name}}`` bindings.

    A typo like ``{{inputs.member_id}}`` — or an unclosed ``{{input.member_id``
    — must be a load-time refusal; the alternative is the literal string being
    typed into a banking UI at replay. Anything mustache-like that is not a
    valid binding counts, including stray/unbalanced braces.
    """
    bad = [
        match.group(0)
        for match in _MUSTACHE.finditer(text)
        if not _BINDING.fullmatch(match.group(0))
    ]
    remainder = _MUSTACHE.sub("", text)  # drop every complete mustache, valid or not
    for marker in ("{{", "}}"):
        index = remainder.find(marker)
        if index != -1:
            bad.append(remainder[index : index + 24])
    return bad


def mask_bindings(text: str, placeholder: str = "BINDING") -> str:
    """Replace every valid binding with a plain placeholder.

    Used to validate regex fields at load time: at replay, substituted binding
    values are escaped before the regex compiles, so masking with a literal
    reproduces the compilable shape.
    """
    return _BINDING.sub(placeholder, text)


def substitute(text: str, inputs: Mapping[str, object]) -> str:
    """Resolve every binding in *text* by dict lookup.

    Single pass: a substituted value is never re-scanned, so input values
    containing ``{{...}}`` stay literal text instead of becoming references.
    Raises ``KeyError`` naming the missing input.
    """

    def _resolve(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise KeyError(f"binding references undeclared or unsupplied input '{name}'")
        return str(inputs[name])

    return _BINDING.sub(_resolve, text)
