"""Safety & policy guardrails.

Enforced in BOTH paths:
  * discovery -- before the agent's proposed action is executed;
  * replay    -- before each recorded step is executed.

Three checks:
  1. Action-type allowlist: only permitted action types may run.
  2. URL allowlist: navigation / the live URL must stay within permitted prefixes.
  3. Risk classification: risky (irreversible / state-changing) actions are handled
     conservatively. Policy is configurable:
        - "block"   : never run risky actions automatically
        - "confirm" : run only if an approval callback says yes (human-in-the-loop)
        - "allow"   : run (used for an approved capability, or an explicit demo)

The default is conservative: risky actions require confirmation unless the calling
capability is marked approved.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..schema import ActionType, RiskLevel


class RiskMode(str, Enum):
    BLOCK = "block"
    CONFIRM = "confirm"
    ALLOW = "allow"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    NEEDS_CONFIRMATION = "needs_confirmation"


class PolicyViolation(Exception):
    """Raised when an action is not permitted by policy."""


@dataclass
class SafetyPolicy:
    allowed_url_prefixes: list[str]
    allowed_action_types: Optional[list[ActionType]] = None
    risk_mode: RiskMode = RiskMode.CONFIRM
    # Called for risky actions when risk_mode == CONFIRM. Returns True to allow.
    confirm_cb: Optional[Callable[[str], bool]] = None

    # ------------------------------------------------------------------ #
    def check_url(self, url: str) -> None:
        if not self.allowed_url_prefixes:
            return  # no allowlist configured
        # Tolerate a trailing slash on the prefix (origin boundary): a prefix of
        # 'http://h:5000/' also permits the bare origin 'http://h:5000'.
        if not any(
            url.startswith(prefix) or url.startswith(prefix.rstrip("/"))
            for prefix in self.allowed_url_prefixes
        ):
            raise PolicyViolation(
                f"URL '{url}' is outside the allowlist {self.allowed_url_prefixes}."
            )

    def check_action_type(self, action: ActionType) -> None:
        if self.allowed_action_types is None:
            return
        if action not in self.allowed_action_types:
            raise PolicyViolation(f"Action type '{action}' is not permitted by policy.")

    def decide_risk(self, risk: RiskLevel, description: str) -> PolicyDecision:
        if risk != RiskLevel.RISKY:
            return PolicyDecision.ALLOW
        if self.risk_mode == RiskMode.ALLOW:
            return PolicyDecision.ALLOW
        if self.risk_mode == RiskMode.BLOCK:
            return PolicyDecision.BLOCK
        # CONFIRM
        if self.confirm_cb is not None and self.confirm_cb(description):
            return PolicyDecision.ALLOW
        return PolicyDecision.NEEDS_CONFIRMATION

    def enforce(self, action: ActionType, risk: RiskLevel, description: str,
                url: Optional[str] = None) -> PolicyDecision:
        """Run all checks. Raises PolicyViolation on a hard block; otherwise returns
        the risk decision (ALLOW or NEEDS_CONFIRMATION)."""
        self.check_action_type(action)
        if url is not None:
            self.check_url(url)
        decision = self.decide_risk(risk, description)
        if decision == PolicyDecision.BLOCK:
            raise PolicyViolation(f"Risky action blocked by policy: {description}")
        return decision
