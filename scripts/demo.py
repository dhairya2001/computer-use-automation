#!/usr/bin/env python3
"""End-to-end offline demo.

Starts the mock bank app, runs a scripted (mock-provider) discovery for two
capabilities, then exercises deterministic replay across the full error taxonomy:

    1. SUCCESS               read balance for an existing member
    2. BUSINESS_OUTCOME      member_not_found (a legitimate result, not a crash)
    3. BUSINESS_OUTCOME      permission_denied
    4. RECOVERY -> SUCCESS   an unexpected interstitial is dismissed, flow continues
    5. FAILED                session timeout detected and reported
    6. FAILED                element-not-found (tampered locator) -> debuggable failure
    7. ESCALATION -> SUCCESS a risky/irreversible step, human takes control, resume
    8. BUSINESS_OUTCOME      invalid_deposit on the open-subaccount flow

A REAL LLM discovery run (the required one) is produced separately with:
    cua discover --goal "..." --url http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)

from cua._demo import discover, open_subaccount_script, read_balance_script  # noqa: E402
from cua.config import load_artifact, make_logger  # noqa: E402
from cua.escalation.handoff import MockOperatorHandler  # noqa: E402
from cua.replay.engine import ReplayEngine  # noqa: E402
from cua.safety.policy import RiskMode  # noqa: E402
from cua.surface.web import PlaywrightWebSurface  # noqa: E402

BASE = os.environ.get("CUA_MOCKAPP_URL", "http://127.0.0.1:5000")


def toggle(**kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    urllib.request.urlopen(f"{BASE}/_test/set?{q}", timeout=5).read()


def replay(artifact, params, *, risk="confirm", escalate=None, label=""):
    secret_vals = [params[n] for n in artifact.secret_input_names() if n in params]
    logger = make_logger(f"replay-{label}", secret_values=secret_vals)
    surface = PlaywrightWebSurface(headless=True)
    engine = ReplayEngine(surface, logger, risk_mode=RiskMode(risk), escalation=escalate)
    res = engine.replay(artifact, params)
    return res


def line(n, title, res):
    d = res.to_dict()
    extra = ""
    if res.status.value == "success":
        extra = f"outputs={d.get('outputs')} recoveries={d.get('recoveries_applied')}"
    elif res.status.value == "business_outcome":
        extra = f"code={d.get('outcome_code')}"
    else:
        f = d.get("failure") or {}
        extra = f"failure.kind={f.get('kind')} step={f.get('step_id')}"
    print(f"  [{n}] {title:<34} -> {res.status.value.upper():<17} {extra}")


def main() -> int:
    proc = subprocess.Popen([sys.executable, "-m", "mockapp.server"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        print("== DISCOVERY (mock provider; scripted stand-in for the LLM) ==")
        a_path = "artifacts/coreserv.read_savings_balance.json"
        b_path = "artifacts/coreserv.open_subaccount.json"
        r1 = discover(read_balance_script(),
                      "Look up a member and read their savings balance", BASE, out_path=a_path)
        r2 = discover(open_subaccount_script(),
                      "Open a new sub-account and reach the confirmation screen", BASE, out_path=b_path)
        print(f"  read_savings_balance : {r1.status}  ({r1.steps_taken} steps) -> {a_path}")
        print(f"  open_subaccount      : {r2.status}  ({r2.steps_taken} steps) -> {b_path}")

        art_a = load_artifact(a_path)
        art_b = load_artifact(b_path)

        print("\n== REPLAY (deterministic; no LLM) ==")

        line(1, "success (read balance)",
             replay(art_a, {"operator_id": "op1", "passcode": "demo", "member_id": "12345"},
                    label="success"))

        line(2, "business: member_not_found",
             replay(art_a, {"operator_id": "op1", "passcode": "demo", "member_id": "00000"},
                    label="notfound"))

        line(3, "business: permission_denied",
             replay(art_a, {"operator_id": "op1", "passcode": "demo", "member_id": "99999"},
                    label="forbidden"))

        toggle(dialog=1)
        line(4, "recovery: dismiss interstitial",
             replay(art_a, {"operator_id": "op1", "passcode": "demo", "member_id": "12345"},
                    label="recovery"))
        toggle(dialog=0)

        toggle(expire=1)
        line(5, "failure: session timeout",
             replay(art_a, {"operator_id": "op1", "passcode": "demo", "member_id": "12345"},
                    label="session"))
        toggle(expire=0)

        # Tampered artifact (in memory only): an unresolvable locator -> debuggable
        # hard failure. Not saved, so it never pollutes the capability catalog.
        tampered = load_artifact(a_path)
        for s in tampered.steps:
            if s.id == "s4_member":
                s.target.primary.value = "does_not_exist_field"
                s.target.fallbacks = []
        line(6, "failure: element not found",
             replay(tampered, {"operator_id": "op1", "passcode": "demo", "member_id": "12345"},
                    label="notfound-hard"))

        # Risky action -> escalate -> mock operator approves -> resume -> success.
        operator = MockOperatorHandler(
            actions=[lambda s: "operator reviewed and approved the irreversible sub-account creation"],
            decision="resolved", note="risky action approved by operator")
        line(7, "escalation: risky step approved",
             replay(art_b, {"operator_id": "op1", "passcode": "demo", "member_id": "12345",
                            "account_type": "Money Market", "initial_deposit": "500"},
                    risk="confirm", escalate=operator, label="escalation"))

        line(8, "business: invalid_deposit",
             replay(art_b, {"operator_id": "op1", "passcode": "demo", "member_id": "12345",
                            "account_type": "Savings", "initial_deposit": "-5"},
                    risk="allow", label="baddeposit"))

        print("\nEvidence written under ./evidence/  (one dir per run)")
        print("Artifacts under ./artifacts/")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
