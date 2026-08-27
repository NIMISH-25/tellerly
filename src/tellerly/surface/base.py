"""The surface seam: how tellerly perceives and acts on an application.

Everything above this line — the discovery engine, the replay engine, and a
human operator during a handoff — speaks this interface. Nothing above it may
assume a browser, a DOM, or Playwright.

Two things define the seam:

- ``observe()`` returns controls as **human-meaningful facts** (role, label,
  the form ``name`` attribute, nearby anchor text) plus an **ephemeral uid**
  valid only for that observation. Element ids are used internally by an
  implementation to correlate handles and are never part of the facts — so
  nothing above the seam can record one.
- ``features()`` declares which identification strategies the implementation
  can honour. An artifact computes what it needs across every ladder rung;
  a mismatch is refused before the first mutating action, not mid-flow.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tellerly.schema import ActionType, FrameRef, SurfaceFeature
from tellerly.schema.locators import LocatorStrategy, Rung, Target


class ControlFacts(BaseModel):
    """What a human operator could say about a control — nothing they couldn't.

    Deliberately absent: element id, CSS path, XPath. The planner and the
    recorder both work from these facts, so neither can leak markup trivia
    into an artifact.
    """

    model_config = ConfigDict(extra="forbid")

    uid: str                          # ephemeral handle, valid for this observation only
    frame: list[FrameRef] = Field(default_factory=list)
    kind: str                         # "input:text", "input:submit", "select", "a", ...
    role: str | None = None           # accessibility role: button, link, textbox, combobox
    accessible_name: str | None = None
    label: str | None = None          # explicit <label for=> text
    name_attr: str | None = None      # the form `name` attribute
    text: str | None = None           # visible text (links/buttons)
    anchor_text: str | None = None    # nearest preceding cell/label text
    value: str | None = None          # current value (masked for password inputs)
    options: list[str] | None = None  # visible option labels for selects
    editable: bool = False


class PageObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    path: str
    title: str
    text: str                         # visible page text (truncated)
    controls: list[ControlFacts]

    def control(self, uid: str) -> ControlFacts | None:
        return next((c for c in self.controls if c.uid == uid), None)


class ProbeResult(BaseModel):
    """The uniqueness measurement behind every recorded ladder rung."""

    model_config = ConfigDict(extra="forbid")

    count: int                        # how many elements the rung matches
    is_target: bool                   # count == 1 AND it is the probed control


class Resolution(BaseModel):
    """The outcome of walking a target's ladder at replay time.

    ``strategy``/``rung_index`` record which rung actually matched — the raw
    drift-telemetry signal — so a run that succeeded via a fallback rung is
    visibly different from one that matched the top rung.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["resolved", "not_found", "ambiguous", "verify_failed"]
    uid: str | None = None
    strategy: LocatorStrategy | None = None
    rung_index: int | None = None     # index in the LADDER, 0-based
    detail: str = ""                  # per-rung notes: why each rung was passed over


class Surface(ABC):
    """The perceive/act contract. One implementation exists today (Playwright
    web); the seam is what a legacy-web or desktop implementation would fill.
    """

    @abstractmethod
    def features(self) -> frozenset[SurfaceFeature]: ...

    @abstractmethod
    def open(self, url: str) -> None:
        """Open the entry URL; subsequent navigation is same-origin paths."""

    @abstractmethod
    def navigate(self, path: str) -> None: ...

    @abstractmethod
    def observe(self) -> PageObservation: ...

    @abstractmethod
    def act(self, uid: str, action: ActionType, value: str | None = None) -> None:
        """click / fill / select / press on a control from the LATEST observation."""

    @abstractmethod
    def probe(self, rung: Rung, frame: list[FrameRef], uid: str | None = None) -> ProbeResult:
        """Measure a candidate locator against the live page."""

    @abstractmethod
    def resolve(self, target: Target) -> Resolution:
        """Walk the target's ladder (bindings already substituted by the
        caller) and return a handle to the first rung that matches exactly one
        element AND passes the target's verify predicate."""

    @abstractmethod
    def read_text(self, uid: str) -> str: ...

    @abstractmethod
    def locate_value_cell(self, anchor_text: str) -> ControlFacts | None:
        """Find the value cell right after a label cell (e.g. the cell after
        'Confirmation No.'). Values on legacy screens live in table cells,
        which are not interactive controls — this is how reads target them.
        Returns registered facts (with a uid) or None."""

    @abstractmethod
    def find_text(self, text: str, timeout_s: float = 2.0) -> bool:
        """Is this text visible anywhere on the page (any frame)? Waits up to
        timeout_s for it to appear — pass a small value for instant checks."""

    @abstractmethod
    def current_path(self) -> str: ...

    @abstractmethod
    def current_url(self) -> str: ...

    def frame_urls(self) -> list[str]:
        """Every document URL currently loaded — the top page plus frames.
        Policy checks cover all of them: an action lands wherever its frame
        is, not where the address bar points. Default suits frameless fakes."""
        return [self.current_url()]

    def dom_snapshot(self) -> str:
        """The page's markup for intervention evidence — what an operator (or
        an auditor, later) can inspect when a screenshot is not enough.
        Non-abstract with an empty default: a surface that cannot serialize
        its documents still supports handoffs, just with thinner evidence."""
        return ""

    @abstractmethod
    def screenshot(self, path: Path) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
