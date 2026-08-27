from __future__ import annotations

import re

import pytest

from target_app import data
from target_app.app import create_app

ID_ATTR = re.compile(r'id="([^"]+)"')
NAME_ATTR = re.compile(r'name="([^"]+)"')


@pytest.fixture()
def client():
    data.reset()
    app = create_app(
        {
            "TESTING": True,
            "INTERSTITIAL_EVERY": 0,   # chaos off unless a test turns it on
            "SLOW_SECONDS": 0.0,
            "SESSION_TTL_S": 10_000,
        }
    )
    with app.test_client() as c:
        yield c


def login(client):
    return client.post(
        "/login", data={"opid": "tester", "opkey": "demo"}, follow_redirects=True
    )


def _page_html(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.get_data(as_text=True)


def test_element_ids_rotate_between_renders(client):
    """Recording an element id must be structurally impossible."""
    first = set(ID_ATTR.findall(_page_html(client, "/login")))
    second = set(ID_ATTR.findall(_page_html(client, "/login")))
    assert first and second
    assert first.isdisjoint(second), "element ids repeated across renders"


def test_form_names_are_stable_between_renders(client):
    """The `name` attribute is the durable handle — the server reads it on submit."""
    first = set(NAME_ATTR.findall(_page_html(client, "/login")))
    second = set(NAME_ATTR.findall(_page_html(client, "/login")))
    assert first == second and "opid" in first and "opkey" in first


def test_no_test_ids_anywhere(client):
    login(client)
    pages = [
        _page_html(client, "/login"),
        _page_html(client, "/search"),
        _page_html(client, "/member/101555"),
        _page_html(client, "/member/101555/panel"),
    ]
    for html in pages:
        assert "data-testid" not in html


def test_action_panel_is_an_iframe(client):
    login(client)
    html = _page_html(client, "/member/101555")
    assert "<iframe" in html and "/member/101555/panel" in html


def test_mixed_labelling(client):
    """Some controls have <label for=>, some are labelled only by adjacent cells."""
    login_html = _page_html(client, "/login")
    assert "<label for=" in login_html                       # Operator ID has a label
    assert login_html.count("<label for=") == 1              # Access Key does not
    login(client)
    panel_html = _page_html(client, "/member/101555/panel")
    assert "<label for=" in panel_html                       # Amount has a label
    assert panel_html.count("<label for=") == 1              # the selects do not
