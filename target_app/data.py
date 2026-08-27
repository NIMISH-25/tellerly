"""Seed data for the Tellerly console mock. Every record is fictional.

Credit-union vocabulary: a member owns numbered "shares" (sub-accounts).
Share S02 of member 101555 is on administrative hold so the
"transfer from a held share" refusal is reachable. Member 55555 is a
restricted record so the permission-denial path is reachable.
"""
from __future__ import annotations

import copy
import itertools
from typing import Any

Member = dict[str, Any]

_SEED: dict[str, Member] = {
    "101555": {
        "member_id": "101555",
        "first_name": "Dana",
        "last_name": "Whitfield",
        "joined": "2012-06-14",
        "status": "Active",
        "restricted": False,
        "shares": [
            {"share_id": "S00", "type": "Regular Share (Savings)", "balance": 2500.00, "status": "OK"},
            {"share_id": "S01", "type": "Share Draft (Checking)", "balance": 840.25, "status": "OK"},
            {"share_id": "S02", "type": "Holiday Club", "balance": 150.00, "status": "HOLD"},
        ],
    },
    "101556": {
        "member_id": "101556",
        "first_name": "Marisol",
        "last_name": "Vega",
        "joined": "2016-02-09",
        "status": "Active",
        "restricted": False,
        "shares": [
            {"share_id": "S00", "type": "Regular Share (Savings)", "balance": 1200.50, "status": "OK"},
            {"share_id": "S01", "type": "Share Draft (Checking)", "balance": 310.00, "status": "OK"},
        ],
    },
    "101557": {
        "member_id": "101557",
        "first_name": "Theo",
        "last_name": "Brandt",
        "joined": "2021-11-30",
        "status": "Active",
        "restricted": False,
        "shares": [
            {"share_id": "S00", "type": "Regular Share (Savings)", "balance": 55.75, "status": "OK"},
        ],
    },
    # Restricted record: viewing it is a permission denial (matrix row 2).
    "55555": {
        "member_id": "55555",
        "first_name": "Restricted",
        "last_name": "Record",
        "joined": "2009-01-01",
        "status": "Active",
        "restricted": True,
        "shares": [],
    },
}

MEMBERS: dict[str, Member] = copy.deepcopy(_SEED)

# Confirmation numbers and the ledger of processed transfer references
# (duplicate-submit detection, matrix row 9).
_confirmation_seq = itertools.count(4211)
PROCESSED: dict[str, dict[str, Any]] = {}


def reset() -> None:
    """Restore seed state (used by tests)."""
    global _confirmation_seq
    MEMBERS.clear()
    MEMBERS.update(copy.deepcopy(_SEED))
    PROCESSED.clear()
    _confirmation_seq = itertools.count(4211)


def get_member(member_id: str) -> Member | None:
    return MEMBERS.get(member_id)


def find_members(query: str) -> list[Member]:
    """Exact member-number match or case-insensitive last-name substring."""
    q = query.strip()
    if not q:
        return []
    if q in MEMBERS:
        return [MEMBERS[q]]
    ql = q.lower()
    return [m for m in MEMBERS.values() if ql in m["last_name"].lower()]


def get_share(member: Member, share_id: str) -> dict[str, Any] | None:
    return next((s for s in member["shares"] if s["share_id"] == share_id), None)


def post_transfer(member_id: str, src_id: str, dst_id: str, amount: float, ref: str) -> dict[str, Any]:
    """Move funds between two shares and record the reference as processed.

    Validation (holds, funds, ranges) is the app layer's job; by the time this
    is called the transfer is committed.
    """
    member = MEMBERS[member_id]
    src = get_share(member, src_id)
    dst = get_share(member, dst_id)
    assert src is not None and dst is not None
    src["balance"] = round(src["balance"] - amount, 2)
    dst["balance"] = round(dst["balance"] + amount, 2)
    receipt = {
        "confirmation_no": f"TL-{next(_confirmation_seq):06d}",
        "ref": ref,
        "member_id": member_id,
        "src_id": src_id,
        "dst_id": dst_id,
        "amount": amount,
        "src_balance": src["balance"],
        "dst_balance": dst["balance"],
    }
    PROCESSED[ref] = receipt
    return receipt
