"""Tellerly(R) Teller Console — a deliberately legacy mock target.

    search 99999          -> no such member            (business outcome)
    member 55555          -> permission denied, 403    (hard failure)
    transfer from S02     -> share on administrative hold (business outcome)
    amount > balance      -> insufficient funds        (business outcome)
    amount -1 or >= 1e9   -> backend 500               (hard failure)
    any URL + ?slow=1     -> 8s load                   (recoverable)
    every 3rd member-record load -> maintenance interstitial (recoverable)
    idle > session TTL    -> session expired mid-flow  (recoverable)
    duplicate confirm     -> already processed         (business outcome)

All data is fictional. This app is a test double, not part of the product.
"""
from __future__ import annotations

import math
import secrets
import time
import uuid

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from target_app import data

ACCESS_KEY = "demo"  # mock credential, documented in the login screen itself

# Endpoints reachable without an operator session.
_OPEN_ENDPOINTS = {"login", "static"}


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = "tellerly-mock-session-key"  # mock target only; holds no real data
    app.config.update(
        SESSION_TTL_S=180,      # idle seconds before the operator session expires
        SLOW_SECONDS=8.0,       # delay applied when ?slow=1 is on the request
        INTERSTITIAL_EVERY=3,   # every Nth member-record load shows the maintenance notice; 0 = off
    )
    if config:
        app.config.update(config)
    # Interstitial cadence counter. Process-global on purpose: this mock
    # assumes one operator per instance, so concurrent clients share it.
    workspace_loads = {"count": 0}

    @app.template_global()
    def rid(base: str) -> str:
        """A per-render random element id — the reason id-based locators can't work here."""
        return f"{base}_{secrets.token_hex(3)}"

    # ------------------------------------------------------------------ gates

    @app.before_request
    def _gate():
        if request.args.get("slow") == "1":
            time.sleep(app.config["SLOW_SECONDS"])
        endpoint = request.endpoint or ""
        if endpoint in _OPEN_ENDPOINTS or endpoint.startswith("static"):
            return None
        if "operator" not in session:
            flash("Please sign in.")
            return redirect(url_for("login"))
        now = time.time()
        last_seen = session.get("last_seen", now)
        if now - last_seen > app.config["SESSION_TTL_S"]:
            session.clear()
            flash("Your session has expired due to inactivity. Please sign in again.")
            return redirect(url_for("login"))
        session["last_seen"] = now
        return None

    def _maybe_interstitial():
        """Every Nth member-record load: the scheduled-maintenance interstitial.

        Acknowledging it grants a one-shot pass so the retried load goes
        through instead of counting again.
        """
        every = app.config["INTERSTITIAL_EVERY"]
        if not every:
            return None
        if session.pop("maintenance_pass", None):
            return None
        workspace_loads["count"] += 1
        if workspace_loads["count"] % every == 0:
            return render_template("interstitial.html", next_url=request.path)
        return None

    # ------------------------------------------------------------------ auth

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            operator_id = request.form.get("opid", "").strip()
            access_key = request.form.get("opkey", "")
            if not operator_id or access_key != ACCESS_KEY:
                error = "Invalid operator ID or access key."
            else:
                session.clear()
                session["operator"] = operator_id
                session["last_seen"] = time.time()
                return redirect(url_for("search"))
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.")
        return redirect(url_for("login"))

    @app.post("/maintenance-ack")
    def maintenance_ack():
        session["maintenance_pass"] = True
        next_url = request.form.get("next") or url_for("search")
        # Same-site paths only; "//host" would be scheme-relative and off-site.
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = url_for("search")
        return redirect(next_url)

    # ------------------------------------------------------------- workspace

    @app.route("/")
    def home():
        return redirect(url_for("search"))

    @app.route("/search", methods=["GET", "POST"])
    def search():
        results = None
        query = ""
        if request.method == "POST":
            query = request.form.get("mbr_no", "").strip()
            if query in data.MEMBERS:
                # Legacy convenience: an exact member number goes straight to the record.
                return redirect(url_for("member_detail", member_id=query))
            results = data.find_members(query)
        return render_template("search.html", results=results, query=query)

    @app.route("/member/<member_id>")
    def member_detail(member_id: str):
        member = data.get_member(member_id)
        if member is None:
            abort(404)
        if member["restricted"]:
            abort(403)
        interstitial = _maybe_interstitial()
        if interstitial is not None:
            return interstitial
        return render_template("member.html", m=member)

    # ------------------------------------------- action panel (nested iframe)

    @app.route("/member/<member_id>/panel")
    def panel(member_id: str):
        member = data.get_member(member_id)
        if member is None:
            abort(404)
        if member["restricted"]:
            abort(403)
        return render_template("panel_transfer.html", m=member, errors=[], form={})

    @app.post("/member/<member_id>/panel/transfer")
    def panel_transfer(member_id: str):
        member = data.get_member(member_id)
        if member is None:
            abort(404)
        if member["restricted"]:
            abort(403)

        form = {
            "src_share": request.form.get("src_share", ""),
            "dst_share": request.form.get("dst_share", ""),
            "amt": request.form.get("amt", "").strip(),
            "memo": request.form.get("memo", "").strip(),
        }
        errors: list[str] = []

        try:
            amount = float(form["amt"])
        except ValueError:
            amount = None
            errors.append("Enter a valid dollar amount.")
        if amount is not None and not math.isfinite(amount):
            # float() happily parses "nan"/"inf"; NaN would sail past every
            # comparison below and corrupt the ledger.
            amount = None
            errors.append("Enter a valid dollar amount.")

        if amount is not None and (amount < 0 or amount >= 1e9):
            # Matrix row 5: the legacy backend does not validate this range —
            # it falls over. Deliberately a hard 500, not a friendly message.
            abort(500)

        if amount is not None:
            # The console posts whole cents, so debit, credit, and the rendered
            # amount always agree.
            amount = round(amount, 2)

        src = data.get_share(member, form["src_share"])
        dst = data.get_share(member, form["dst_share"])
        if src is None or dst is None:
            errors.append("Select a source and destination share.")
        elif form["src_share"] == form["dst_share"]:
            errors.append("Source and destination shares must differ.")

        if amount is not None and amount == 0:
            errors.append("Amount must be greater than zero.")

        if errors:
            return render_template("panel_transfer.html", m=member, errors=errors, form=form)

        # Business refusals (matrix rows 3 and 4): the app ran fine and said no.
        if src["status"] == "HOLD":
            return render_template(
                "panel_refusal.html",
                m=member,
                title="TRANSFER REFUSED",
                message=(
                    f"Share {src['share_id']} is on administrative hold. "
                    "Transfers from a held share are not permitted."
                ),
                code="HOLD-207",
            )
        if amount > src["balance"]:
            return render_template(
                "panel_refusal.html",
                m=member,
                title="TRANSFER REFUSED",
                message=(
                    f"Insufficient available funds in share {src['share_id']} "
                    f"(available ${src['balance']:,.2f})."
                ),
                code="NSF-104",
            )

        session["pending_transfer"] = {
            "ref": uuid.uuid4().hex,
            "member_id": member_id,
            "src_share": form["src_share"],
            "dst_share": form["dst_share"],
            "amount": amount,
            "memo": form["memo"],
        }
        return redirect(url_for("panel_confirm", member_id=member_id))

    @app.route("/member/<member_id>/panel/confirm", methods=["GET", "POST"])
    def panel_confirm(member_id: str):
        member = data.get_member(member_id)
        if member is None:
            abort(404)
        if member["restricted"]:
            abort(403)

        if request.method == "POST":
            ref = request.form.get("txn_ref", "")
            # Matrix row 9: a re-submitted reference was already posted.
            # Scoped to this member — a ref posted elsewhere is not "ours".
            processed = data.PROCESSED.get(ref)
            if processed is not None and processed["member_id"] == member_id:
                return render_template(
                    "panel_duplicate.html", m=member, receipt=processed
                )
            pending = session.get("pending_transfer")
            if not pending or pending["ref"] != ref or pending["member_id"] != member_id:
                flash("No pending transfer for this reference.")
                return redirect(url_for("panel", member_id=member_id))
            receipt = data.post_transfer(
                member_id,
                pending["src_share"],
                pending["dst_share"],
                pending["amount"],
                ref,
            )
            session.pop("pending_transfer", None)
            return render_template(
                "panel_receipt.html", m=member, receipt=receipt, memo=pending["memo"]
            )

        pending = session.get("pending_transfer")
        if not pending or pending["member_id"] != member_id:
            return redirect(url_for("panel", member_id=member_id))
        src = data.get_share(member, pending["src_share"])
        dst = data.get_share(member, pending["dst_share"])
        return render_template("panel_confirm.html", m=member, p=pending, src=src, dst=dst)

    # ---------------------------------------------------------- error pages

    @app.errorhandler(403)
    def forbidden(_e):
        return (
            render_template(
                "error.html",
                code="SEC-403",
                title="NOT AUTHORIZED",
                message=(
                    "Your operator profile does not have permission to view this "
                    "record. Contact a supervisor to request elevated access."
                ),
            ),
            403,
        )

    @app.errorhandler(404)
    def not_found(_e):
        return (
            render_template(
                "error.html",
                code="REC-404",
                title="RECORD NOT FOUND",
                message="The requested record does not exist in this institution's files.",
            ),
            404,
        )

    @app.errorhandler(500)
    def internal_fault(_e):
        return (
            render_template(
                "error.html",
                code="TL-ERR-2214",
                title="TELLERLY INTERNAL FAULT",
                message=(
                    "A general ledger posting fault occurred while processing the "
                    "request. The transaction was not posted. Contact your system "
                    "administrator if the condition persists."
                ),
            ),
            500,
        )

    return app
