"""Targeting: a ranked ladder of candidate locators, never one selector.

The ladder is ordered by durability, most durable first:

    role+name > label > form `name` attribute > visible text > anchored > CSS

and a validator refuses a ladder that is out of order. Two deliberate
absences shape everything here:

- **No element-id strategy exists.** On legacy surfaces ids rotate per render
  (our mock target does this on purpose). The schema cannot express an id
  locator, so one can never be recorded — the mistake is unrepresentable.
- **No coordinate strategy exists yet.** Screenshot+coordinates is a valid
  perception mechanism for surfaces with nothing better (declared as a
  surface feature), but it is not a *recordable* rung until a surface that
  needs it lands.

Confidence is **measured, not guessed**: at the moment an action succeeds
during discovery, every plausible locator is built and probed against the
live page, and only those matching exactly one element survive into the
artifact. The number recorded here is that probe result.

Every target also carries a ``verify`` predicate — a cheap assertion against
the resolved element run before acting — so a degraded rung cannot silently
match a similar-looking control. ``on_ambiguous`` defaults to fail: acting on
the wrong row in a banking screen is worse than not acting.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocatorStrategy(str, Enum):
    ROLE = "role"      # accessible role + accessible name
    LABEL = "label"    # <label for=> text
    NAME = "name"      # form control `name` attribute
    TEXT = "text"      # visible text content
    ANCHOR = "anchor"  # nth control after anchor text (label-less table layouts)
    CSS = "css"        # structural selector — last resort


#: Durability rank, higher = more durable. The ladder must be non-increasing.
#: The `name` attribute ranks above visible text on purpose: in a
#: server-rendered legacy app it is what the server reads on submit, so it
#: cannot churn like classes and ids without breaking the backend — and it is
#: invisible to users, so nobody "rebrands" it.
DURABILITY: dict[LocatorStrategy, int] = {
    LocatorStrategy.ROLE: 5,
    LocatorStrategy.LABEL: 4,
    LocatorStrategy.NAME: 3,
    LocatorStrategy.TEXT: 2,
    LocatorStrategy.ANCHOR: 1,
    LocatorStrategy.CSS: 0,
}


class SurfaceFeature(str, Enum):
    """Identification/perception features a surface can honour.

    An artifact's required features are computed over ALL its ladder rungs —
    not just the top one — so a surface that would silently strip an
    artifact's fallbacks is refused up front (SURFACE_INCOMPATIBLE), not
    discovered mid-flow.
    """

    ROLE_QUERY = "role_query"
    LABEL_QUERY = "label_query"
    NAME_QUERY = "name_query"
    TEXT_QUERY = "text_query"
    ANCHOR_QUERY = "anchor_query"
    DOM_QUERY = "dom_query"
    FRAMES = "frames"
    SCREENSHOT = "screenshot"
    COORDINATES = "coordinates"
    A11Y_TREE = "a11y_tree"


FEATURE_BY_STRATEGY: dict[LocatorStrategy, SurfaceFeature] = {
    LocatorStrategy.ROLE: SurfaceFeature.ROLE_QUERY,
    LocatorStrategy.LABEL: SurfaceFeature.LABEL_QUERY,
    LocatorStrategy.NAME: SurfaceFeature.NAME_QUERY,
    LocatorStrategy.TEXT: SurfaceFeature.TEXT_QUERY,
    LocatorStrategy.ANCHOR: SurfaceFeature.ANCHOR_QUERY,
    LocatorStrategy.CSS: SurfaceFeature.DOM_QUERY,
}


class _RungBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(
        gt=0.0,
        le=1.0,
        description=(
            "Measured at record time: the uniqueness-probe result for this "
            "locator against the live page, not an estimate."
        ),
    )
    note: str | None = None


class RoleRung(_RungBase):
    strategy: Literal["role"] = "role"
    role: str                      # e.g. "button", "textbox", "link"
    name: str                      # accessible name; {{input.*}} bindings legal


class LabelRung(_RungBase):
    strategy: Literal["label"] = "label"
    label: str                     # label text; bindings legal


class NameRung(_RungBase):
    strategy: Literal["name"] = "name"
    name: str                      # the form control's `name` attribute


class TextRung(_RungBase):
    strategy: Literal["text"] = "text"
    text: str                      # visible text; bindings legal
    control: str | None = None     # optional control kind filter, e.g. "a"


class AnchorRung(_RungBase):
    strategy: Literal["anchor"] = "anchor"
    anchor_text: str               # the nearby text that anchors the control
    control: str = "input"         # control kind to take after the anchor
    offset: int = Field(default=0, ge=0)  # nth such control after the anchor


_ID_SELECTOR = re.compile(r"#|\[\s*id\b", re.IGNORECASE)


class CssRung(_RungBase):
    strategy: Literal["css"] = "css"
    css: str

    @model_validator(mode="after")
    def _no_id_selectors(self) -> "CssRung":
        # The banned strategy must not sneak back in through the CSS escape
        # hatch: ids rotate per render on legacy surfaces.
        if _ID_SELECTOR.search(self.css):
            raise ValueError(
                f"css rung {self.css!r} addresses an element id — ids rotate per render "
                "and are not recordable"
            )
        return self


Rung = Annotated[
    Union[RoleRung, LabelRung, NameRung, TextRung, AnchorRung, CssRung],
    Field(discriminator="strategy"),
]


class AmbiguityPolicy(str, Enum):
    FAIL = "fail"    # more than one match = TARGET_AMBIGUOUS. The default.
    FIRST = "first"  # take the first match — legal only on non-mutating steps


class FrameRef(BaseModel):
    """One hop in a frame path, identified by durable frame attributes."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None      # the frame's `name` attribute
    url_path: str | None = None  # substring of the frame's document path

    @model_validator(mode="after")
    def _at_least_one(self) -> "FrameRef":
        if self.name is None and self.url_path is None:
            raise ValueError("frame reference needs a name or a url_path")
        return self


class VerifyPredicate(BaseModel):
    """Cheap assertions against the resolved element, run before acting.

    The ladder finds a candidate; verify proves it is the *right kind* of
    thing before anything is typed or clicked.
    """

    model_config = ConfigDict(extra="forbid")

    control: str | None = None        # expected control kind: "input", "select", "a"...
    name_attr: str | None = None      # expected `name` attribute
    text_contains: str | None = None  # expected visible text fragment; bindings legal

    @model_validator(mode="after")
    def _at_least_one(self) -> "VerifyPredicate":
        if self.control is None and self.name_attr is None and self.text_contains is None:
            raise ValueError("verify predicate must assert at least one property")
        return self


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        description="The human phrase for the control, e.g. 'the SEARCH button'."
    )
    frame: list[FrameRef] = Field(
        default_factory=list, description="Frame path from the top document; empty = top."
    )
    ladder: list[Rung] = Field(min_length=1)
    verify: VerifyPredicate
    on_ambiguous: AmbiguityPolicy = AmbiguityPolicy.FAIL

    @model_validator(mode="after")
    def _ladder_ordered(self) -> "Target":
        ranks = [DURABILITY[LocatorStrategy(rung.strategy)] for rung in self.ladder]
        if any(a < b for a, b in zip(ranks, ranks[1:])):
            order = " -> ".join(rung.strategy for rung in self.ladder)
            raise ValueError(
                f"ladder out of durability order ({order}); most durable rung must come first"
            )
        return self

    def required_features(self) -> set[SurfaceFeature]:
        """Features a surface must honour to execute this target — over ALL rungs."""
        features = {
            FEATURE_BY_STRATEGY[LocatorStrategy(rung.strategy)] for rung in self.ladder
        }
        if self.frame:
            features.add(SurfaceFeature.FRAMES)
        return features
