"""Evidence: the structured, redacted record of what a run did and why.

One directory per run under /evidence/: an append-only events.jsonl, numbered
screenshots, and any JSON documents the run wants preserved (trace, result).

Structured writes are serialized FIRST and redacted over the exact bytes that
hit disk, so a sensitive value cannot ride through inside a non-string
payload. Screenshots are the documented exception: pixels are captured raw
(pixel-level masking is future work; sensitive form values are only safe in
screenshots when the control masks itself, e.g. password inputs).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tellerly.kernel.redaction import Redactor


class RunLog:
    def __init__(self, evidence_root: Path, run_id: str, redactor: Redactor) -> None:
        self.run_id = run_id
        self.dir = evidence_root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor
        self._events_path = self.dir / "events.jsonl"

    def event(self, kind: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": kind,
            **payload,
        }
        # Serialize first, then redact the final text: default=str can surface
        # a sensitive value from inside a non-JSON-native object.
        text = json.dumps(record, ensure_ascii=False, default=str)
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(self._redactor.redact(text))
            handle.write("\n")

    def screenshot_path(self, label: str) -> Path:
        return self.dir / f"{label}.png"

    def write_json(self, filename: str, document: BaseModel | dict) -> Path:
        if isinstance(document, BaseModel):
            data = document.model_dump(mode="json")
        else:
            data = document
        path = self.dir / filename
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        path.write_text(self._redactor.redact(text), encoding="utf-8")
        return path
