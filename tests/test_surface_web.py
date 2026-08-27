"""Playwright surface against the live mock console: observation facts,
uniqueness probing, acting, and frame traversal. Needs the Chromium that
`playwright install chromium` provides."""
from __future__ import annotations

import threading

import pytest  # noqa: F401  (fixtures + raises)
from werkzeug.serving import make_server

from target_app import data
from target_app.app import create_app
from tellerly.schema import ActionType
from tellerly.schema.locators import (
    AnchorRung,
    CssRung,
    LabelRung,
    NameRung,
    RoleRung,
)
from tellerly.surface.web import PlaywrightWebSurface

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server():
    data.reset()
    app = create_app(
        {"INTERSTITIAL_EVERY": 0, "SESSION_TTL_S": 100_000, "SLOW_SECONDS": 0.0}
    )
    httpd = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield BASE
    httpd.shutdown()


@pytest.fixture(scope="module")
def surface(server):
    web = PlaywrightWebSurface(headless=True, step_timeout_s=8)
    web.open(f"{server}/login")
    yield web
    web.close()


def control_by(observation, **conditions):
    for control in observation.controls:
        if all(getattr(control, key) == value for key, value in conditions.items()):
            return control
    raise AssertionError(f"no control with {conditions}")


def sign_in(surface):
    surface.navigate("/login")
    observation = surface.observe()
    surface.act(control_by(observation, name_attr="opid").uid, ActionType.FILL, "surf-op")
    surface.act(control_by(observation, name_attr="opkey").uid, ActionType.FILL, "demo")
    surface.act(control_by(observation, accessible_name="Sign In").uid, ActionType.CLICK)


def test_login_facts_reflect_the_mixed_labelling(surface):
    surface.navigate("/login")
    observation = surface.observe()

    operator = control_by(observation, name_attr="opid")
    assert operator.label == "Operator ID:"           # has a real <label for=>
    assert operator.role == "textbox"
    assert operator.editable

    key = control_by(observation, name_attr="opkey")
    assert key.label is None                          # no label element at all
    assert key.anchor_text == "Access Key:"           # only the adjacent cell
    assert key.kind == "input:password"

    sign_in = control_by(observation, accessible_name="Sign In")
    assert sign_in.role == "button"


def test_stale_uids_fail_cleanly_across_observations(surface):
    """A uid from a previous observation must never silently resolve to a
    different element — it must be a clean KeyError."""
    surface.navigate("/login")
    old = surface.observe()
    old_uid = control_by(old, name_attr="opid").uid
    surface.navigate("/login")
    surface.observe()  # new generation; old_uid is now stale
    with pytest.raises(KeyError, match="not in the latest observation|stale"):
        surface.act(old_uid, ActionType.FILL, "x")


def test_facts_never_carry_element_ids(surface):
    surface.navigate("/login")
    observation = surface.observe()
    dumped = observation.model_dump_json()
    assert "fld_opid" not in dumped  # the rotating id prefix stays below the seam


def test_probe_measures_uniqueness_and_identity(surface):
    surface.navigate("/login")
    observation = surface.observe()
    operator = control_by(observation, name_attr="opid")
    key = control_by(observation, name_attr="opkey")

    rung = NameRung(name="opid", confidence=1.0)
    assert surface.probe(rung, [], operator.uid).is_target
    assert not surface.probe(rung, [], key.uid).is_target  # unique but not THIS control

    assert surface.probe(LabelRung(label="Operator ID:", confidence=1.0), [], operator.uid).is_target
    assert surface.probe(
        AnchorRung(anchor_text="Access Key:", control="input", confidence=1.0), [], key.uid
    ).is_target
    assert surface.probe(CssRung(css='input[name="opid"]', confidence=1.0), [], operator.uid).is_target

    generic = surface.probe(CssRung(css="input", confidence=1.0), [], operator.uid)
    assert generic.count > 1 and not generic.is_target


def test_act_navigates_the_flow_and_frames_are_traversed(surface):
    sign_in(surface)
    assert surface.current_path() == "/search"

    observation = surface.observe()
    surface.act(control_by(observation, name_attr="mbr_no").uid, ActionType.FILL, "101555")
    surface.act(control_by(observation, accessible_name="Search").uid, ActionType.CLICK)
    assert surface.current_path() == "/member/101555"
    assert surface.find_text("Member Record")

    observation = surface.observe()
    source = control_by(observation, name_attr="src_share")
    assert source.frame and source.frame[-1].name == "actionpanel"  # inside the iframe
    assert source.anchor_text == "From Share:"
    assert any("S00" in option for option in source.options)

    role_in_frame = RoleRung(role="button", name="Continue", confidence=1.0)
    assert surface.probe(
        role_in_frame,
        source.frame,
        control_by(observation, accessible_name="Continue").uid,
    ).is_target


def test_transfer_through_the_iframe_and_value_cell_read(surface):
    sign_in(surface)
    surface.navigate("/member/101555")
    observation = surface.observe()
    surface.act(control_by(observation, name_attr="src_share").uid, ActionType.SELECT, "S00")
    surface.act(control_by(observation, name_attr="dst_share").uid, ActionType.SELECT, "S01")
    surface.act(control_by(observation, name_attr="amt").uid, ActionType.FILL, "10.00")
    surface.act(control_by(observation, accessible_name="Continue").uid, ActionType.CLICK)
    assert surface.find_text("CONFIRM TRANSFER")

    observation = surface.observe()
    confirm = control_by(observation, accessible_name="Confirm & Post Transfer")
    surface.act(confirm.uid, ActionType.CLICK)
    assert surface.find_text("TRANSFER POSTED")

    cell = surface.locate_value_cell("Confirmation No.")
    assert cell is not None
    assert cell.text.startswith("TL-")
    probe = surface.probe(
        AnchorRung(anchor_text="Confirmation No.", control="td", confidence=1.0),
        cell.frame,
        cell.uid,
    )
    assert probe.is_target
