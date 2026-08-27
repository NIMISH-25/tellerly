"""Runtime configuration and repo-relative paths."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_computed_root = Path(__file__).resolve().parents[2]
REPO_ROOT = _computed_root if (_computed_root / "target_app").is_dir() else Path.cwd()

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    evidence_dir: Path
    capabilities_dir: Path
    target_base_url: str
    google_api_key: str | None
    gemini_model: str


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    return Settings(
        repo_root=REPO_ROOT,
        evidence_dir=Path(os.environ.get("TELLERLY_EVIDENCE_DIR", REPO_ROOT / "evidence")),
        capabilities_dir=Path(
            os.environ.get("TELLERLY_CAPABILITIES_DIR", REPO_ROOT / "capabilities")
        ),
        target_base_url=os.environ.get("TELLERLY_TARGET_URL", "http://127.0.0.1:8000"),
        # google-genai reads GOOGLE_API_KEY itself; GEMINI_API_KEY also accepted.
        google_api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        gemini_model=os.environ.get("TELLERLY_GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
    )
