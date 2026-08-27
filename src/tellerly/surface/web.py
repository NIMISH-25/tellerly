"""Playwright-backed web surface.

Chosen because it drives a real browser (headed or headless), speaks CDP —
which is what later lets a human take over the same live session — and can
honour every locator strategy in the ladder, frames included.

Element ids are used strictly inside this module to correlate labels and
handles during one observation; they never appear in `ControlFacts`, so
nothing above the seam can record one.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from tellerly.schema import ActionType, FrameRef, SurfaceFeature
from tellerly.schema.locators import (
    AnchorRung,
    CssRung,
    LabelRung,
    NameRung,
    RoleRung,
    Rung,
    TextRung,
)
from tellerly.surface.base import ControlFacts, PageObservation, ProbeResult, Surface

_CONTROL_QUERY = "input, select, textarea, button, a[href]"
_MAX_CONTROLS = 80
_MAX_TEXT = 4000

# Facts a human operator could state about the control — computed in-page.
_FACTS_JS = """
el => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const kind = tag === 'input' ? `input:${type || 'text'}` : tag;
  let label = null;
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l) label = (l.innerText || '').trim() || null;
  }
  let text = (el.innerText || '').trim() || null;
  if (tag === 'input') text = ['submit','button','reset'].includes(type) ? ((el.value || '').trim() || null) : null;
  let anchor = null;
  const cell = el.closest('td,th');
  if (cell) {
    let prev = cell.previousElementSibling;
    while (prev && !anchor) {
      const t = (prev.innerText || '').trim();
      if (t) anchor = t;
      prev = prev.previousElementSibling;
    }
  }
  let role = null;
  if (tag === 'a') role = 'link';
  else if (tag === 'button' || (tag === 'input' && ['submit','button','reset'].includes(type))) role = 'button';
  else if (tag === 'select') role = 'combobox';
  else if (tag === 'input' && type === 'checkbox') role = 'checkbox';
  else if (tag === 'textarea' || tag === 'input') role = 'textbox';
  const isPassword = type === 'password';
  let value = null;
  if (tag === 'select') value = el.value || null;
  else if ('value' in el && el.value && !['submit','button','reset'].includes(type)) {
    value = isPassword ? '********' : el.value;
  }
  let options = null;
  if (tag === 'select') options = [...el.options].map(o => (o.label || '').trim());
  const accessible = label || ((role === 'button' || role === 'link') ? text : null)
    || el.getAttribute('aria-label') || null;
  const editable = ['input','select','textarea'].includes(tag) && !el.disabled && !el.readOnly
    && !['submit','button','reset','hidden'].includes(type);
  return {kind, role, accessible_name: accessible, label, name_attr: el.getAttribute('name'),
          text, anchor_text: anchor, value, options, editable};
}
"""

# Anchored resolution: the nth control of a kind after an exact anchor text.
# Nested markup (a <b> inside the labelling <td>) yields several anchor
# elements with the same text — picks are deduplicated so they count once.
_ANCHOR_JS = """
([args, target]) => {
  const anchors = [...document.querySelectorAll('td,th,label,b,font,span,legend')]
    .filter(e => (e.innerText || '').trim() === args.anchor && !e.querySelector(args.control));
  const controls = [...document.querySelectorAll(args.control)];
  const picks = [];
  for (const a of anchors) {
    const after = controls.filter(c => a.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (after.length > args.offset) picks.push(after[args.offset]);
  }
  const unique = [...new Set(picks)];
  return {count: unique.length, is: target !== null && unique.length === 1 && unique[0] === target};
}
"""

# The value cell after a label cell — SAME semantics as _ANCHOR_JS with
# control='td', offset=0, so the recorded read rung and this discovery-time
# lookup can never disagree about which cell they mean.
_VALUE_CELL_JS = """
(anchor) => {
  const anchors = [...document.querySelectorAll('td,th,label,b,font,span,legend')]
    .filter(e => (e.innerText || '').trim() === anchor && !e.querySelector('td'));
  const controls = [...document.querySelectorAll('td')];
  const picks = [];
  for (const a of anchors) {
    const after = controls.filter(c => a.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (after.length > 0) picks.push(after[0]);
  }
  const unique = [...new Set(picks)];
  return unique.length === 1 ? unique[0] : null;
}
"""


def _frame_ref_matches(wanted: FrameRef, actual: FrameRef) -> bool:
    """A frame's `name` is the durable handle; the document path shifts as the
    frame navigates within its flow, so it is only the fallback."""
    if wanted.name is not None:
        return actual.name == wanted.name
    return bool(wanted.url_path and actual.url_path and wanted.url_path in actual.url_path)


class PlaywrightWebSurface(Surface):
    def __init__(self, headless: bool = True, step_timeout_s: float = 10.0) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(step_timeout_s * 1000)
        self._page = self._context.new_page()
        self._origin = ""
        self._handles: dict[str, tuple[object, object]] = {}  # uid -> (frame, element)
        self._observation_generation = 0  # makes uids unique ACROSS observations

    # ------------------------------------------------------------------ seam

    def features(self) -> frozenset[SurfaceFeature]:
        return frozenset(
            {
                SurfaceFeature.ROLE_QUERY,
                SurfaceFeature.LABEL_QUERY,
                SurfaceFeature.NAME_QUERY,
                SurfaceFeature.TEXT_QUERY,
                SurfaceFeature.ANCHOR_QUERY,
                SurfaceFeature.DOM_QUERY,
                SurfaceFeature.FRAMES,
                SurfaceFeature.SCREENSHOT,
            }
        )

    def open(self, url: str) -> None:
        parsed = urlparse(url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._page.goto(url, wait_until="load")

    def navigate(self, path: str) -> None:
        self._page.goto(urljoin(self._origin, path), wait_until="load")

    def _settle(self) -> None:
        """Wait out page AND frame navigations — an action inside an iframe
        navigates the frame while the page reports itself loaded."""
        try:
            self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass  # a busy page is not an error; callers assert on state, not timing

    def observe(self) -> PageObservation:
        self._page.wait_for_load_state("load")
        self._settle()
        # One retry: a frame swapping documents at the exact moment of
        # enumeration throws adoption/context errors that are gone 300ms later.
        try:
            return self._observe_once()
        except Exception:
            self._page.wait_for_timeout(300)
            self._settle()
            return self._observe_once()

    def _observe_once(self) -> PageObservation:
        self._handles = {}
        self._observation_generation += 1
        generation = self._observation_generation
        controls: list[ControlFacts] = []
        texts: list[str] = []
        counter = 0
        for frame in self._page.frames:
            if frame.is_detached():
                continue
            try:
                body_text = frame.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception:
                continue  # frame detached mid-observation
            if body_text.strip():
                texts.append(body_text.strip())
            frame_path = self._frame_path(frame)
            for handle in frame.query_selector_all(_CONTROL_QUERY):
                # Visibility filters BEFORE the cap: legacy consoles carry
                # dozens of hidden form-state inputs that must not eat the
                # control budget.
                if len(controls) >= _MAX_CONTROLS:
                    break
                if not handle.is_visible():
                    continue
                facts = handle.evaluate(_FACTS_JS)
                uid = f"o{generation}c{counter}"  # stale uids can never collide
                counter += 1
                self._handles[uid] = (frame, handle)
                controls.append(ControlFacts(uid=uid, frame=frame_path, **facts))
        return PageObservation(
            url=self._page.url,
            path=urlparse(self._page.url).path,
            title=self._page.title(),
            text="\n".join(texts)[:_MAX_TEXT],
            controls=controls,
        )

    def act(self, uid: str, action: ActionType, value: str | None = None) -> None:
        frame, handle = self._require(uid)
        try:
            if action is ActionType.CLICK:
                handle.click()
            elif action is ActionType.FILL:
                handle.fill(value or "")
            elif action is ActionType.SELECT:
                try:
                    handle.select_option(value=value)
                except Exception:
                    handle.select_option(label=value)
            elif action is ActionType.PRESS:
                handle.press(value or "Enter")
            else:
                raise ValueError(f"surface cannot perform '{action.value}' on a control")
        except ValueError:
            raise
        except Exception as exc:
            if "not attached" in str(exc).lower() or "detached" in str(exc).lower():
                raise KeyError(
                    f"stale control '{uid}' — the page changed since it was observed"
                ) from exc
            raise
        self._page.wait_for_load_state("load")
        self._settle()

    def probe(self, rung: Rung, frame: list[FrameRef], uid: str | None = None) -> ProbeResult:
        target_frame = self._resolve_frame(frame)
        if target_frame is None:
            return ProbeResult(count=0, is_target=False)
        target_handle = self._handles[uid][1] if uid in self._handles else None

        if isinstance(rung, AnchorRung):
            result = target_frame.evaluate(
                _ANCHOR_JS,
                [
                    {"anchor": rung.anchor_text, "control": rung.control, "offset": rung.offset},
                    target_handle,
                ],
            )
            return ProbeResult(count=result["count"], is_target=bool(result["is"]))

        if isinstance(rung, RoleRung):
            locator = target_frame.get_by_role(rung.role, name=rung.name, exact=True)
        elif isinstance(rung, LabelRung):
            locator = target_frame.get_by_label(rung.label, exact=True)
        elif isinstance(rung, NameRung):
            import json as _json

            locator = target_frame.locator(f"[name={_json.dumps(rung.name)}]")
        elif isinstance(rung, TextRung):
            if rung.control:
                pattern = re.compile(rf"^\s*{re.escape(rung.text)}\s*$")
                locator = target_frame.locator(rung.control).filter(has_text=pattern)
            else:
                locator = target_frame.get_by_text(rung.text, exact=True)
        elif isinstance(rung, CssRung):
            locator = target_frame.locator(rung.css)
        else:  # pragma: no cover — the union is closed
            raise ValueError(f"unknown rung type {type(rung).__name__}")

        count = locator.count()
        is_target = False
        if count == 1 and target_handle is not None:
            matched = locator.first.element_handle()
            is_target = bool(
                target_frame.evaluate("([a, b]) => a === b", [matched, target_handle])
            )
        return ProbeResult(count=count, is_target=is_target)

    def read_text(self, uid: str) -> str:
        _, handle = self._require(uid)
        # Password values stay masked here just as they do in ControlFacts.
        return handle.evaluate(
            "el => (el.getAttribute && (el.getAttribute('type') || '').toLowerCase())"
            " === 'password' ? '********' : (el.innerText || el.value || '').trim()"
        )

    def locate_value_cell(self, anchor_text: str) -> ControlFacts | None:
        for frame in self._page.frames:
            try:
                handle = frame.evaluate_handle(_VALUE_CELL_JS, anchor_text)
            except Exception:
                continue
            element = handle.as_element()
            if element is None:
                continue
            uid = f"o{self._observation_generation}v{len(self._handles)}"
            self._handles[uid] = (frame, element)
            return ControlFacts(
                uid=uid,
                frame=self._frame_path(frame),
                kind="td",
                anchor_text=anchor_text,
                text=element.evaluate("el => (el.innerText || '').trim()"),
                editable=False,
            )
        return None

    def find_text(self, text: str, timeout_s: float = 2.0) -> bool:
        # Brief retry window: a frame can be mid-navigation at the exact moment
        # of the check, which destroys its execution context — that is "not
        # there YET", not "not there".
        deadline = time.monotonic() + timeout_s
        while True:
            for frame in self._page.frames:
                try:
                    if frame.evaluate(
                        "t => (document.body ? document.body.innerText : '').includes(t)",
                        text,
                    ):
                        return True
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return False
            self._page.wait_for_timeout(250)

    def current_path(self) -> str:
        return urlparse(self._page.url).path

    def current_url(self) -> str:
        return self._page.url

    def screenshot(self, path: Path) -> None:
        self._page.screenshot(path=str(path), full_page=True)

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    # -------------------------------------------------------------- internals

    def _require(self, uid: str) -> tuple[object, object]:
        if uid not in self._handles:
            raise KeyError(
                f"unknown control '{uid}' — it is not in the latest observation"
            )
        return self._handles[uid]

    def _frame_path(self, frame) -> list[FrameRef]:
        path: list[FrameRef] = []
        current = frame
        while current is not None and current != self._page.main_frame:
            url_path = urlparse(current.url).path or None
            path.append(FrameRef(name=current.name or None, url_path=url_path))
            current = current.parent_frame
        return list(reversed(path))

    def _resolve_frame(self, path: list[FrameRef]):
        # Resolved against the page's LIVE frame list, never child_frames
        # chains: after a page re-render, a detached frame object with the
        # same name can linger in child_frames and shadow the live one.
        if not path:
            return self._page.main_frame
        for frame in self._page.frames:
            if frame is self._page.main_frame or frame.is_detached():
                continue
            chain = self._frame_path(frame)
            if len(chain) == len(path) and all(
                _frame_ref_matches(wanted, actual)
                for wanted, actual in zip(path, chain)
            ):
                return frame
        return None
