"""Scripted discovery flows for the offline demo and tests.

These are the exact action dicts a real LLM would emit, fed through MockProvider so
the full discovery pipeline (safety, execution, checkpoints, artifact assembly) runs
end-to-end without an API key. The committed evidence from a REAL Anthropic run is
produced separately via `cua discover`; these scripts keep the pipeline testable and
give a deterministic demo.
"""
from __future__ import annotations

from typing import Any

from .agent.llm import MockProvider
from .agent.loop import AgentLoop
from .config import build_target, make_logger, save_artifact
from .safety.policy import RiskMode, SafetyPolicy
from .surface.web import PlaywrightWebSurface

# Shared recovery + business-outcome declarations (what the author knows can happen
# at replay time, even though the happy-path discovery never hit them).
_RECOVERY_DIALOG = {
    "name": "dismiss_system_notice",
    "when": {"kind": "text_present", "value": "System Notice"},
    "do": [{
        "action": "click", "step_id": "r_ack", "description": "Acknowledge system notice",
        "target": {"description": "Acknowledge button",
                   "primary": {"strategy": "role", "role": "button", "value": "Acknowledge"},
                   "fallbacks": []},
    }],
    "max_attempts": 2,
}
_BO_NOT_FOUND = {"code": "member_not_found",
                 "when": {"kind": "text_present", "value": "No member record found"},
                 "message": "No member exists for the supplied id.", "terminal": True}
_BO_FORBIDDEN = {"code": "permission_denied",
                 "when": {"kind": "text_present", "value": "Permission denied"},
                 "message": "Operator role is not authorized for this record.", "terminal": True}
_BO_BAD_DEPOSIT = {"code": "invalid_deposit",
                   "when": {"kind": "text_present", "value": "Initial deposit must be a positive"},
                   "message": "The supplied initial deposit is not a positive amount.",
                   "terminal": True}


def _login_steps() -> list[dict[str, Any]]:
    return [
        {"action": "fill", "step_id": "s1_operator", "description": "Enter operator id",
         "value": "{{operator_id}}",
         "target": {"description": "Operator ID field",
                    "primary": {"strategy": "name_attr", "value": "operator_id"},
                    "fallbacks": [{"strategy": "css", "value": "input[type=text]"}],
                    "reasoning": "Legacy field has no accessible name; name attr is stable and meaningful."}},
        {"action": "fill", "step_id": "s2_passcode", "description": "Enter passcode",
         "value": "{{passcode}}",
         "target": {"description": "Passcode field",
                    "primary": {"strategy": "name_attr", "value": "passcode"},
                    "fallbacks": [{"strategy": "css", "value": "input[type=password]"}],
                    "reasoning": "Password input located by name attr; secret value is redacted."}},
        {"action": "click", "step_id": "s3_signin", "description": "Sign in",
         "target": {"description": "Sign In button",
                    "primary": {"strategy": "role", "role": "button", "value": "Sign In"},
                    "fallbacks": [{"strategy": "css", "value": "input[type=submit]"}],
                    "reasoning": "Submit button exposes accessible name 'Sign In' via its value."},
         "checkpoint": {"kind": "url_contains", "value": "/search",
                        "description": "Reached the member lookup screen"}},
    ]


def _search_steps() -> list[dict[str, Any]]:
    return [
        {"action": "fill", "step_id": "s4_member", "description": "Enter member id",
         "value": "{{member_id}}",
         "target": {"description": "Member ID field",
                    "primary": {"strategy": "name_attr", "value": "member_id"},
                    "fallbacks": [{"strategy": "xpath",
                                   "value": "//td[contains(.,'Member ID')]/following-sibling::td//input"}],
                    "reasoning": "name attr primary; xpath anchored on the visible 'Member ID' label as fallback."}},
        {"action": "click", "step_id": "s5_search", "description": "Run search",
         "target": {"description": "Search button",
                    "primary": {"strategy": "role", "role": "button", "value": "Search"},
                    "fallbacks": []},
         "checkpoint": {"kind": "url_contains", "value": "/member/",
                        "description": "Navigated to a member record route"}},
    ]


def read_balance_script() -> list[dict[str, Any]]:
    plan = {
        "action": "plan",
        "capability": {
            "id": "coreserv.read_savings_balance",
            "name": "Read member savings balance",
            "description": "Look up a member by id and read their current savings balance.",
        },
        "inputs": [
            {"name": "operator_id", "type": "string", "required": True,
             "description": "Operator login id", "example": "op1", "secret": False},
            {"name": "passcode", "type": "string", "required": True,
             "description": "Operator passcode", "example": "demo", "secret": True},
            {"name": "member_id", "type": "string", "required": True,
             "description": "Member id to look up", "example": "12345", "secret": False},
        ],
        "run_params": {"operator_id": "op1", "passcode": "demo", "member_id": "12345"},
    }
    extract = {
        "action": "extract", "step_id": "s6_balance", "description": "Read savings balance",
        "extract_as": "savings_balance", "extract_type": "string",
        "extract_description": "Current savings balance as displayed (currency string).",
        "target": {"description": "Savings balance value cell",
                   "primary": {"strategy": "xpath",
                               "value": "//td[normalize-space()='Savings Balance']/following-sibling::td[1]"},
                   "fallbacks": [],
                   "reasoning": "Anchored to the visible label text, so robust to column reordering; "
                                "legacy table has no ids to target instead."}}
    finish = {
        "action": "finish",
        "success": {"kind": "text_present", "value": "Savings Balance",
                    "description": "Member detail page with the savings balance is shown."},
        "business_outcomes": [_BO_NOT_FOUND, _BO_FORBIDDEN],
        "recoveries": [_RECOVERY_DIALOG],
    }
    return [plan, *_login_steps(), *_search_steps(), extract, finish]


def open_subaccount_script() -> list[dict[str, Any]]:
    plan = {
        "action": "plan",
        "capability": {
            "id": "coreserv.open_subaccount",
            "name": "Open a new sub-account",
            "description": "Open a new sub-account for a member and reach the confirmation screen.",
        },
        "inputs": [
            {"name": "operator_id", "type": "string", "required": True, "example": "op1"},
            {"name": "passcode", "type": "string", "required": True, "example": "demo", "secret": True},
            {"name": "member_id", "type": "string", "required": True, "example": "12345"},
            {"name": "account_type", "type": "string", "required": True, "example": "Savings"},
            {"name": "initial_deposit", "type": "string", "required": True, "example": "250"},
        ],
        "run_params": {"operator_id": "op1", "passcode": "demo", "member_id": "12345",
                       "account_type": "Savings", "initial_deposit": "250"},
    }
    steps = [
        {"action": "click", "step_id": "s6_open", "description": "Open new sub-account",
         "target": {"description": "Open New Sub-Account link",
                    "primary": {"strategy": "role", "role": "link", "value": "Open New Sub-Account"},
                    "fallbacks": [{"strategy": "text", "value": "Open New Sub-Account"}],
                    "reasoning": "Link identified by its user-visible text via ARIA role."},
         "checkpoint": {"kind": "url_contains", "value": "/new-subaccount"}},
        {"action": "select", "step_id": "s7_type", "description": "Choose account type",
         "value": "{{account_type}}",
         "target": {"description": "Account Type dropdown",
                    "primary": {"strategy": "name_attr", "value": "account_type"},
                    "fallbacks": []}},
        {"action": "fill", "step_id": "s8_deposit", "description": "Enter initial deposit",
         "value": "{{initial_deposit}}",
         "target": {"description": "Initial Deposit field",
                    "primary": {"strategy": "name_attr", "value": "initial_deposit"},
                    "fallbacks": []}},
        {"action": "click", "step_id": "s9_submit", "description": "Open the sub-account (irreversible)",
         "risk": "risky",
         "target": {"description": "Open Sub-Account button",
                    "primary": {"strategy": "role", "role": "button", "value": "Open Sub-Account"},
                    "fallbacks": []},
         "checkpoint": {"kind": "text_present", "value": "Sub-account created successfully"}},
    ]
    finish = {
        "action": "finish",
        "success": {"kind": "text_present", "value": "Sub-account created successfully",
                    "description": "Confirmation screen reached."},
        "business_outcomes": [_BO_BAD_DEPOSIT, _BO_NOT_FOUND, _BO_FORBIDDEN],
        "recoveries": [_RECOVERY_DIALOG],
    }
    return [plan, *_login_steps(), *_search_steps(), *steps, finish]


def discover(script: list[dict[str, Any]], goal: str, base_url: str, app_id: str = "coreserv",
             tenant_id: str | None = None, out_path: str | None = None,
             allow_risky: bool = True, headless: bool = True):
    """Run a scripted (mock-provider) discovery and optionally save the artifact."""
    target = build_target(app_id=app_id, base_url=base_url, tenant_id=tenant_id)
    logger = make_logger("discovery")
    surface = PlaywrightWebSurface(headless=headless)
    surface.start()
    provider = MockProvider(script)
    prefix = "/".join(base_url.split("/")[:3]) + "/"
    policy = SafetyPolicy(allowed_url_prefixes=[prefix],
                          risk_mode=RiskMode.ALLOW if allow_risky else RiskMode.CONFIRM)
    loop = AgentLoop(provider, surface, goal, target, policy, logger)
    try:
        result = loop.run()
    finally:
        surface.close()
    if result.artifact is not None and out_path:
        save_artifact(result.artifact, out_path)
    return result
