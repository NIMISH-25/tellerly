"""Value-based redaction.

Masking a field NAMED "password" is easy and insufficient: the same value
resurfaces in URLs, form dumps, page text, and error strings. So sensitive
VALUES are registered once and caught wherever they appear. Every disk write
(evidence, traces, results) goes through this boundary.
"""
from __future__ import annotations

from typing import Any


class Redactor:
    _MIN_LENGTH = 4  # shorter values would shred unrelated text

    def __init__(self) -> None:
        self._values: dict[str, str] = {}  # sensitive value -> input name

    def register(self, name: str, value: object) -> None:
        text = str(value)
        if len(text) < self._MIN_LENGTH:
            # Refusing loudly beats silently writing a declared-sensitive
            # value to evidence in cleartext.
            raise ValueError(
                f"sensitive value for '{name}' is shorter than {self._MIN_LENGTH} "
                "characters and cannot be redacted safely"
            )
        self._values[text] = name

    def redact(self, text: str) -> str:
        # Longest values first so overlapping registrations cannot leave tails.
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, f"[REDACTED:{self._values[value]}]")
        return text

    def redact_object(self, value: Any) -> Any:
        """Recursively redact every string inside dicts/lists/tuples — keys too."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {
                self.redact_object(key): self.redact_object(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_object(item) for item in value]
        return value
