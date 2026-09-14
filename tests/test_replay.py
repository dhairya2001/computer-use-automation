"""Engine tests for the result contract / error taxonomy, using FakeSurface."""
from __future__ import annotations

from cua.escalation.handoff import MockOperatorHandler
from cua.observability.logging import RunLogger
from cua.replay.engine import ReplayEngine
from cua.replay.errors import FailureKind, ResultStatus
from cua.safety.policy import RiskMode
from cua.safety.redaction import Redactor
from cua.schema import (
    ActionType,
    ArtifactPolicy,
    BusinessOutcomeDetector,
    CapabilityArtifact,
    CapabilityMeta,
    Checkpoint,
    CheckpointKind,
    Locator,
    LocatorStrategy,
    OutputField,
    RecoveryRule,
    RiskLevel,
    Selector,
    Step,
    TargetBinding,
)
from fakes import FakeSurface

BASE = "http://h:5000/"


def loc(desc):
    return Locator(description=desc, primary=Selector(strategy=LocatorStrategy.TEXT, value=desc))


def make_logger(tmp_path):
    return RunLogger(str(tmp_path), "run-test", Redactor())


def artifact(steps, success, business=None, recoveries=None, policy=None, outputs=None):
    return CapabilityArtifact(
        capability=CapabilityMeta(id="app.cap", name="cap", description="d"),
        target=TargetBinding(app_id="app", base_url=BASE),
        steps=steps,
        outputs=outputs or [],
        success=success,
        business_outcomes=business or [],
        recoveries=recoveries or [],
        policy=policy or ArtifactPolicy(allowed_url_prefixes=[BASE]),
    )


def engine(surface, tmp_path, **kw):
    return ReplayEngine(surface, make_logger(tmp_path), manage_surface_lifecycle=False, **kw)


# --------------------------------------------------------------------------- #
def test_success_with_output(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Search", "Balance"}
    s.transitions["Search"] = lambda fs: fs.texts.add("Savings Balance")
    s.extract_values["Balance"] = "$4,210.55"
    art = artifact(
        steps=[
            Step(id="s1", action=ActionType.CLICK, target=loc("Search"),
                 checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Savings Balance")),
            Step(id="s2", action=ActionType.EXTRACT, target=loc("Balance"), extract_as="bal"),
        ],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Savings Balance"),
        outputs=[OutputField(name="bal")],
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.SUCCESS
    assert r.outputs == {"bal": "$4,210.55"}


def test_business_outcome_not_failure(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Search"}
    s.transitions["Search"] = lambda fs: fs.texts.add("No member record found")
    art = artifact(
        steps=[
            Step(id="s1", action=ActionType.CLICK, target=loc("Search")),
            Step(id="s2", action=ActionType.EXTRACT, target=loc("Balance"), extract_as="bal"),
        ],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Savings Balance"),
        business=[BusinessOutcomeDetector(
            code="member_not_found",
            when=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="No member record found"))],
        outputs=[OutputField(name="bal")],
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.BUSINESS_OUTCOME
    assert r.outcome_code == "member_not_found"


def test_element_not_found_is_hard_failure(tmp_path):
    s = FakeSurface()  # nothing resolvable
    art = artifact(
        steps=[Step(id="s1", action=ActionType.CLICK, target=loc("Missing"))],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="never"),
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.FAILED
    assert r.failure.kind == FailureKind.ELEMENT_NOT_FOUND
    assert r.failure.step_id == "s1"


def test_checkpoint_failure(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Go"}  # click works but no text appears
    art = artifact(
        steps=[Step(id="s1", action=ActionType.CLICK, target=loc("Go"),
                    checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Arrived"),
                    timeout_ms=100)],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Arrived"),
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.FAILED
    assert r.failure.kind == FailureKind.CHECKPOINT_FAILED


def test_recovery_then_success(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Go", "Ack"}
    # Clicking Go raises an interstitial; dismissing it (Ack) clears it + reveals success.
    s.transitions["Go"] = lambda fs: fs.texts.add("System Notice")

    def ack(fs):
        fs.texts.discard("System Notice")
        fs.texts.add("Done")
    s.transitions["Ack"] = ack

    art = artifact(
        steps=[
            Step(id="s1", action=ActionType.CLICK, target=loc("Go")),
            Step(id="s2", action=ActionType.WAIT_FOR,
                 checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Done")),
        ],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Done"),
        recoveries=[RecoveryRule(
            name="dismiss_notice",
            when=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="System Notice"),
            do=[Step(id="r1", action=ActionType.CLICK, target=loc("Ack"))])],
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.SUCCESS
    assert "dismiss_notice" in r.recoveries_applied


def test_policy_violation_on_bad_url(tmp_path):
    s = FakeSurface()
    art = artifact(
        steps=[Step(id="s1", action=ActionType.GOTO, url="http://evil.com/x")],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="x"),
        policy=ArtifactPolicy(allowed_url_prefixes=[BASE]),
    )
    r = engine(s, tmp_path).replay(art, {})
    assert r.status == ResultStatus.FAILED
    assert r.failure.kind == FailureKind.POLICY_VIOLATION


def test_risky_escalation_resolved(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Submit"}
    s.transitions["Submit"] = lambda fs: fs.texts.add("Created")
    art = artifact(
        steps=[Step(id="s1", action=ActionType.CLICK, target=loc("Submit"), risk=RiskLevel.RISKY,
                    checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"))],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"),
    )
    op = MockOperatorHandler(actions=[lambda s: "approved"], decision="resolved")
    r = engine(s, tmp_path, risk_mode=RiskMode.CONFIRM, escalation=op).replay(art, {})
    assert r.status == ResultStatus.SUCCESS


def test_risky_escalation_aborted(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Submit"}
    art = artifact(
        steps=[Step(id="s1", action=ActionType.CLICK, target=loc("Submit"), risk=RiskLevel.RISKY)],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"),
    )
    op = MockOperatorHandler(actions=[], decision="aborted", note="operator declined")
    r = engine(s, tmp_path, risk_mode=RiskMode.CONFIRM, escalation=op).replay(art, {})
    assert r.status == ResultStatus.FAILED
    assert r.failure.kind == FailureKind.NEEDS_HUMAN


def test_risky_needs_human_without_handler(tmp_path):
    s = FakeSurface()
    s.resolvable = {"Submit"}
    art = artifact(
        steps=[Step(id="s1", action=ActionType.CLICK, target=loc("Submit"), risk=RiskLevel.RISKY)],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"),
    )
    r = engine(s, tmp_path, risk_mode=RiskMode.CONFIRM).replay(art, {})
    assert r.status == ResultStatus.FAILED
    assert r.failure.kind == FailureKind.NEEDS_HUMAN
