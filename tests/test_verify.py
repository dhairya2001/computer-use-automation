"""Tests for post-run verification (LLM judge + human second sign-off)."""
from __future__ import annotations

import tempfile

from cua.observability.logging import RunLogger
from cua.replay.engine import ReplayEngine
from cua.replay.errors import ResultStatus
from cua.safety.redaction import Redactor
from cua.schema import (
    ActionType, ArtifactPolicy, CapabilityArtifact, CapabilityMeta, Checkpoint,
    CheckpointKind, Locator, LocatorStrategy, Selector, Step, TargetBinding,
)
from cua.verify import MockJudge, MockReviewer, Verifier
from fakes import FakeSurface

BASE = "http://h:5000/"


def _artifact():
    return CapabilityArtifact(
        capability=CapabilityMeta(id="app.cap", name="c", description="open a sub-account"),
        target=TargetBinding(app_id="app", base_url=BASE),
        steps=[Step(id="s1", action=ActionType.CLICK,
                    target=Locator(description="Go",
                                   primary=Selector(strategy=LocatorStrategy.TEXT, value="Go")),
                    checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"))],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Created"),
        policy=ArtifactPolicy(allowed_url_prefixes=[BASE]),
    )


def _surface():
    s = FakeSurface()
    s.resolvable = {"Go"}
    s.transitions["Go"] = lambda fs: fs.texts.add("Created")
    return s


def _run(verifier, backend=None):
    eng = ReplayEngine(
        _surface(), RunLogger(tempfile.mkdtemp(), "run", Redactor()),
        manage_surface_lifecycle=False, verifier=verifier,
        backend_fetch=(lambda a, p: backend) if backend is not None else None,
    )
    return eng.replay(_artifact(), {})


def test_llm_pass_and_human_approve():
    r = _run(Verifier(MockJudge("pass", 0.95), MockReviewer("approved")),
             backend={"subaccounts_for_member": [{"id": "X-1"}]})
    assert r.status == ResultStatus.SUCCESS
    v = r.verification
    assert v["llm_verdict"] == "pass"
    assert v["human_review"]["status"] == "approved"
    assert v["overall"] == "approved"


def test_no_reviewer_is_never_auto_approved():
    # Banking fail-safe: LLM pass alone must NOT approve.
    r = _run(Verifier(MockJudge("pass", 0.99), None))
    assert r.verification["overall"] == "needs_human_review"


def test_human_reject_overrides_llm():
    r = _run(Verifier(MockJudge("pass", 0.9), MockReviewer("rejected", "ops", "looks wrong")))
    assert r.verification["overall"] == "rejected"


def test_no_verifier_leaves_verification_none():
    r = _run(None)
    assert r.verification is None
