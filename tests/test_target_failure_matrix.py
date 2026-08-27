"""Every row of failure matrix must be reachable from a real
request. These tests are the proof, row by row, plus the happy path the
capability will automate.
"""
from __future__ import annotations

import re
import time

import pytest

from target_app import data
from target_app.app import create_app

TXN_REF = re.compile(r'name="txn_ref" value="([0-9a-f]+)"')


def make_app(**overrides):
    data.reset()
    config = {
        "TESTING": True,
        "INTERSTITIAL_EVERY": 0,
        "SLOW_SECONDS": 0.0,
        "SESSION_TTL_S": 10_000,
    }
    config.update(overrides)
    return create_app(config)


@pytest.fixture()
def client():
    with make_app().test_client() as c:
        yield c


def login(client):
    return client.post(
        "/login", data={"opid": "tester", "opkey": "demo"}, follow_redirects=True
    )


def start_transfer(client, member_id="101555", src="S00", dst="S01", amt="25.00"):
    return client.post(
        f"/member/{member_id}/panel/transfer",
        data={"src_share": src, "dst_share": dst, "amt": amt, "memo": ""},
    )


# ----------------------------------------------------------- the happy path


def test_happy_path_transfer_posts_and_updates_balances(client):
    login(client)
    response = start_transfer(client, amt="25.00")
    assert response.status_code == 302

    confirm_html = client.get("/member/101555/panel/confirm").get_data(as_text=True)
    ref = TXN_REF.search(confirm_html).group(1)
    assert "CONFIRM TRANSFER" in confirm_html

    receipt = client.post(
        "/member/101555/panel/confirm", data={"txn_ref": ref}
    ).get_data(as_text=True)
    assert "TRANSFER POSTED" in receipt
    assert "TL-004211" in receipt
    assert "$2,475.00" in receipt  # S00: 2500 - 25
    assert "$865.25" in receipt    # S01: 840.25 + 25


# ------------------------------------------------------- matrix, row by row


def test_row1_no_such_member_is_a_no_records_result(client):
    """member 99999 -> no such member (BUSINESS_OUTCOME)."""
    login(client)
    response = client.post("/search", data={"mbr_no": "99999"})
    assert response.status_code == 200
    assert "No records found" in response.get_data(as_text=True)


def test_row2_permission_denied_is_403(client):
    """member 55555 -> permission denied (HARD_FAILURE)."""
    login(client)
    response = client.get("/member/55555")
    assert response.status_code == 403
    assert "NOT AUTHORIZED" in response.get_data(as_text=True)


def test_row3_transfer_from_held_share_is_refused(client):
    """source share on hold -> validation refusal (BUSINESS_OUTCOME)."""
    login(client)
    response = start_transfer(client, src="S02", dst="S00", amt="10.00")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "TRANSFER REFUSED" in body and "administrative hold" in body


def test_row4_insufficient_funds_is_refused(client):
    """amount > balance -> insufficient funds (BUSINESS_OUTCOME)."""
    login(client)
    response = start_transfer(client, amt="99999.00")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "TRANSFER REFUSED" in body and "Insufficient available funds" in body


@pytest.mark.parametrize("amount", ["-1", "1000000000"])
def test_row5_out_of_range_amount_is_a_backend_500(client, amount):
    """amount -1 / 1e9 -> backend 500 (HARD_FAILURE)."""
    login(client)
    response = start_transfer(client, amt=amount)
    assert response.status_code == 500
    assert "TELLERLY INTERNAL FAULT" in response.get_data(as_text=True)


def test_row6_slow_flag_delays_the_response():
    """?slow=1 -> slow load (RECOVERABLE). Delay scaled down for the test."""
    with make_app(SLOW_SECONDS=0.2).test_client() as client:
        started = time.monotonic()
        response = client.get("/login?slow=1")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed >= 0.2


def test_row6_slow_flag_must_be_exactly_1():
    """?slow=0 must not sleep — the switch is =1, not "param present"."""
    with make_app(SLOW_SECONDS=0.5).test_client() as client:
        started = time.monotonic()
        response = client.get("/login?slow=0")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed < 0.4


def test_row7_every_third_load_shows_maintenance_interstitial():
    """1-in-3 member-record loads -> maintenance interstitial (RECOVERABLE)."""
    with make_app(INTERSTITIAL_EVERY=3).test_client() as client:
        login(client)
        for _ in range(2):
            ok = client.get("/member/101555")
            assert "Member Record" in ok.get_data(as_text=True)
        third = client.get("/member/101555").get_data(as_text=True)
        assert "SCHEDULED MAINTENANCE NOTICE" in third

        # Acknowledging grants a one-shot pass and the load goes through.
        client.post("/maintenance-ack", data={"next": "/member/101555"})
        after = client.get("/member/101555").get_data(as_text=True)
        assert "Member Record" in after


def test_row8_idle_past_ttl_expires_the_session_mid_flow(client):
    """idle > TTL -> session expired (RECOVERABLE)."""
    login(client)
    with client.session_transaction() as s:
        s["last_seen"] = time.time() - 100_000
    response = client.get("/member/101555", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "session has expired" in body and "Operator Sign-In" in body


def test_row9_duplicate_submit_is_already_processed(client):
    """duplicate submit -> already processed (BUSINESS_OUTCOME)."""
    login(client)
    start_transfer(client, amt="25.00")
    confirm_html = client.get("/member/101555/panel/confirm").get_data(as_text=True)
    ref = TXN_REF.search(confirm_html).group(1)

    first = client.post("/member/101555/panel/confirm", data={"txn_ref": ref})
    assert "TRANSFER POSTED" in first.get_data(as_text=True)

    second = client.post("/member/101555/panel/confirm", data={"txn_ref": ref})
    body = second.get_data(as_text=True)
    assert "ALREADY PROCESSED" in body and "TL-004211" in body
    # The balance moved exactly once.
    assert data.get_share(data.get_member("101555"), "S00")["balance"] == 2475.00


# ----------------------------------------------- plain validation re-renders


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"src_share": "S00", "dst_share": "S01", "amt": "abc"}, "valid dollar amount"),
        ({"src_share": "S00", "dst_share": "S00", "amt": "5"}, "must differ"),
        ({"src_share": "S00", "dst_share": "S01", "amt": "0"}, "greater than zero"),
    ],
)
def test_form_validation_rerenders_with_a_message(client, form, message):
    login(client)
    response = client.post(
        "/member/101555/panel/transfer", data={**form, "memo": ""}
    )
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)


def test_login_rejects_a_bad_access_key(client):
    response = client.post("/login", data={"opid": "tester", "opkey": "wrong"})
    assert "Invalid operator ID or access key" in response.get_data(as_text=True)


# --------------------------------------------- regression: guard the guards


@pytest.mark.parametrize("amount", ["nan", "inf", "-inf"])
def test_nonfinite_amounts_are_rejected_not_posted(client, amount):
    """float() parses 'nan'/'inf'; NaN would sail past every comparison and
    corrupt the ledger. Must be a plain validation error."""
    login(client)
    response = start_transfer(client, amt=amount)
    assert response.status_code == 200
    assert "valid dollar amount" in response.get_data(as_text=True)
    assert data.get_share(data.get_member("101555"), "S00")["balance"] == 2500.00


def test_subcent_amounts_are_quantized_consistently(client):
    """Debit, credit, and the rendered amount must agree to the cent."""
    login(client)
    start_transfer(client, amt="25.005")
    confirm_html = client.get("/member/101555/panel/confirm").get_data(as_text=True)
    ref = TXN_REF.search(confirm_html).group(1)
    receipt = client.post(
        "/member/101555/panel/confirm", data={"txn_ref": ref}
    ).get_data(as_text=True)
    assert "TRANSFER POSTED" in receipt
    assert "$25.00" in receipt and "$2,475.00" in receipt and "$865.25" in receipt
    member = data.get_member("101555")
    total = sum(s["balance"] for s in member["shares"])
    assert total == 3490.25  # no cent vanished


def test_confirm_route_is_also_permission_gated(client):
    """The restricted-record 403 applies to every member-scoped route."""
    login(client)
    response = client.post(
        "/member/55555/panel/confirm", data={"txn_ref": "deadbeef"}
    )
    assert response.status_code == 403


def test_duplicate_ref_is_scoped_to_the_member(client):
    """A reference posted for one member is not 'already processed' for another."""
    login(client)
    start_transfer(client, amt="25.00")
    confirm_html = client.get("/member/101555/panel/confirm").get_data(as_text=True)
    ref = TXN_REF.search(confirm_html).group(1)
    client.post("/member/101555/panel/confirm", data={"txn_ref": ref})

    response = client.post("/member/101556/panel/confirm", data={"txn_ref": ref})
    assert response.status_code == 302  # back to the panel, not a duplicate page
