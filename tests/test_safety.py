import pytest

from cua.safety.policy import (
    PolicyDecision,
    PolicyViolation,
    RiskMode,
    SafetyPolicy,
)
from cua.schema import ActionType, RiskLevel


def test_url_allowlist():
    p = SafetyPolicy(allowed_url_prefixes=["http://h:5000/"])
    p.check_url("http://h:5000/search")     # ok
    p.check_url("http://h:5000")            # bare origin tolerated
    with pytest.raises(PolicyViolation):
        p.check_url("http://evil.com/x")


def test_action_type_allowlist():
    p = SafetyPolicy(allowed_url_prefixes=[], allowed_action_types=[ActionType.CLICK])
    p.check_action_type(ActionType.CLICK)
    with pytest.raises(PolicyViolation):
        p.check_action_type(ActionType.FILL)


def test_risk_modes():
    block = SafetyPolicy(allowed_url_prefixes=[], risk_mode=RiskMode.BLOCK)
    assert block.decide_risk(RiskLevel.RISKY, "x") == PolicyDecision.BLOCK
    assert block.decide_risk(RiskLevel.SAFE, "x") == PolicyDecision.ALLOW

    confirm = SafetyPolicy(allowed_url_prefixes=[], risk_mode=RiskMode.CONFIRM)
    assert confirm.decide_risk(RiskLevel.RISKY, "x") == PolicyDecision.NEEDS_CONFIRMATION

    confirm_yes = SafetyPolicy(allowed_url_prefixes=[], risk_mode=RiskMode.CONFIRM,
                               confirm_cb=lambda d: True)
    assert confirm_yes.decide_risk(RiskLevel.RISKY, "x") == PolicyDecision.ALLOW

    allow = SafetyPolicy(allowed_url_prefixes=[], risk_mode=RiskMode.ALLOW)
    assert allow.decide_risk(RiskLevel.RISKY, "x") == PolicyDecision.ALLOW


def test_enforce_blocks_risky():
    p = SafetyPolicy(allowed_url_prefixes=[], risk_mode=RiskMode.BLOCK)
    with pytest.raises(PolicyViolation):
        p.enforce(ActionType.CLICK, RiskLevel.RISKY, "danger")
