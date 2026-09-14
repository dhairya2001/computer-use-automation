"""Mock legacy bank back-office web app (a stand-in for a real core/servicing tool).

Design goals (see REPORT.md 'Heterogeneity'):
  * LEGACY MARKUP ON PURPOSE. Table-based layout, inline styles, NO data-testids,
    NO stable ids on most controls, form fields identified only by `name` and by
    the visible label text. This forces the automation to rely on robust,
    human-visible targeting (labels/roles/text) instead of brittle CSS nth-child.
  * RUNTIME ERRORS ON PURPOSE. The interesting failures at a bank are not layout
    drift, they are runtime/exceptional states. This app can produce, on demand:
        - "record not found"      (business outcome)
        - "permission denied"     (business outcome)
        - validation error        (recoverable / business, depending on caller)
        - an unexpected interstitial dialog that must be dismissed (recoverable)
        - session timeout / expiry (recoverable-ish: must re-auth)
        - transient slowness       (recoverable: wait/retry)

Error injection is controlled by query params or a session flag so a single replay
run can be pointed at a specific exceptional state without changing code:

    /search?inject=slow          -> add latency to the next detail load
    /member/00000                -> not found
    /member/99999                -> permission denied
    /member/12345?inject=dialog  -> show a blocking interstitial on the detail page
    any route with ?expire=1     -> force the session to look expired

This is not production code; it exists only to give the agent/replay a realistic
surface to drive.
"""
from __future__ import annotations

import json
import os
import time

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .data import get_member

app = Flask(__name__)
app.secret_key = "mock-app-not-a-secret"  # nosec - mock app only

# Process-wide test toggles, flipped via /_test/set, so a replay scenario can be
# pointed at a specific exceptional state without per-request query params.
STATE = {"force_dialog": False, "force_expire": False}

# Sub-accounts are keyed by member id and stored in a JSON file, which is the SINGLE
# SOURCE OF TRUTH: every page view reads it fresh and every creation writes it back.
# So the UI always reflects the current file -- including edits made to it by hand or
# by another process -- with no restart needed. (No DB required for the mock.)
# Override the location with CUA_SUBACCOUNTS_FILE; default is <project_root>/subaccounts.json.
SUBACCOUNTS_FILE = os.environ.get(
    "CUA_SUBACCOUNTS_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "subaccounts.json"),
)


def _load_subaccounts() -> dict:
    """Read the whole store from the JSON file. Returns {} if missing/unreadable."""
    try:
        with open(SUBACCOUNTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_subaccounts(data: dict) -> None:
    try:
        with open(SUBACCOUNTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass  # a mock; never crash the app over a write failure


def subaccounts_for(member_id: str) -> list[dict]:
    """Sub-accounts for one member, read fresh from the JSON file each call."""
    return _load_subaccounts().get(member_id, [])


@app.route("/_test/set")
def _test_set():
    if "dialog" in request.args:
        STATE["force_dialog"] = request.args.get("dialog") == "1"
    if "expire" in request.args:
        STATE["force_expire"] = request.args.get("expire") == "1"
    return {"state": STATE}


def _logged_in() -> bool:
    return bool(session.get("user"))


def _session_expired() -> bool:
    # Force-expire hook for replay error demos.
    if STATE["force_expire"] or request.args.get("expire") == "1":
        return True
    return not _logged_in()


@app.route("/")
def index():
    if not _logged_in():
        return redirect(url_for("login"))
    return redirect(url_for("search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("operator_id", "").strip()
        pw = request.form.get("passcode", "").strip()
        # Mock auth: any non-empty operator id + passcode 'demo' works.
        if user and pw == "demo":
            session["user"] = user
            return redirect(url_for("search"))
        return render_template("login.html", error="Invalid operator credentials.")
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET", "POST"])
def search():
    if _session_expired():
        return render_template("session_expired.html"), 440

    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
        inject = request.form.get("inject", "").strip()
        if not member_id:
            return render_template(
                "search.html", error="Member ID is required.", inject=""
            )
        # Carry an injected condition forward to the detail page.
        target = url_for("member_detail", member_id=member_id)
        if inject:
            target += f"?inject={inject}"
        return redirect(target)

    # Allow an inject param to pre-fill the hidden field so a replay can request it.
    return render_template("search.html", error=None, inject=request.args.get("inject", ""))


@app.route("/member/<member_id>")
def member_detail(member_id: str):
    if _session_expired():
        return render_template("session_expired.html"), 440

    inject = request.args.get("inject", "")

    # Transient slowness (recoverable: replay should wait for the checkpoint).
    if inject == "slow":
        time.sleep(3.0)

    member, reason = get_member(member_id)
    if reason == "not_found":
        # Business outcome, not a crash: HTTP 200 with a clear signal on the page.
        return render_template("not_found.html", member_id=member_id)
    if reason == "forbidden":
        return render_template("permission_denied.html", member_id=member_id), 403

    # Acknowledging the interstitial consumes the forced-dialog toggle (one-shot),
    # so a replay's recovery step genuinely clears it.
    if request.args.get("ack") == "1":
        STATE["force_dialog"] = False
    show_dialog = (inject == "dialog") or STATE["force_dialog"]
    return render_template(
        "member_detail.html",
        m=member,
        show_dialog=show_dialog,
        subaccounts=subaccounts_for(member_id),
    )


@app.route("/member/<member_id>/new-subaccount", methods=["GET", "POST"])
def new_subaccount(member_id: str):
    if _session_expired():
        return render_template("session_expired.html"), 440

    member, reason = get_member(member_id)
    if reason == "not_found":
        return render_template("not_found.html", member_id=member_id)
    if reason == "forbidden":
        return render_template("permission_denied.html", member_id=member_id), 403

    if request.method == "POST":
        acct_type = request.form.get("account_type", "").strip()
        deposit = request.form.get("initial_deposit", "").strip()
        # Validation error (recoverable / business): deposit must be a positive number.
        try:
            amount = float(deposit)
            assert amount > 0
        except (ValueError, AssertionError):
            return render_template(
                "new_subaccount.html",
                m=member,
                error="Initial deposit must be a positive dollar amount.",
            )
        # Reach the confirmation screen (the success checkpoint for the 'action' flow).
        new_id = f"{member_id}-SUB-{int(time.time()) % 10000:04d}"
        # Persist the created sub-account so it can be verified later on the
        # member detail page.
        store = _load_subaccounts()  # read-modify-write the JSON (source of truth)
        store.setdefault(member_id, []).append({
            "id": new_id,
            "type": acct_type,
            "deposit": f"${amount:,.2f}",
            "opened_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        _save_subaccounts(store)
        return render_template(
            "subaccount_confirm.html",
            m=member,
            acct_type=acct_type,
            deposit=f"${amount:,.2f}",
            new_id=new_id,
        )

    return render_template("new_subaccount.html", m=member, error=None)


if __name__ == "__main__":
    # Bound to localhost only; this is a demo surface.
    app.run(host="127.0.0.1", port=5000, debug=False)
