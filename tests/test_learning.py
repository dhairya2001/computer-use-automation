"""Learning from human interventions -> draft proposed overrides."""
from __future__ import annotations

import os

from cua.escalation.handoff import MockOperatorHandler
from cua.learning.overrides import MockSynthesizer, selector_policy_ok
from cua.observability.logging import RunLogger
from cua.replay.engine import ReplayEngine
from cua.replay.errors import ResultStatus
from cua.safety.redaction import Redactor
from cua.schema import (
    ActionType, ArtifactPolicy, CapabilityArtifact, CapabilityMeta, Checkpoint,
    CheckpointKind, Locator, LocatorStrategy, Selector, Step, TargetBinding,
)
from fakes import FakeSurface

BASE = "http://h:5000/"


def _artifact():
    return CapabilityArtifact(
        capability=CapabilityMeta(id="app.cap", name="c", description="fill a field"),
        target=TargetBinding(app_id="app", base_url=BASE),
        steps=[Step(id="s1_field", action=ActionType.CLICK,
                    target=Locator(description="Member field",
                                   primary=Selector(strategy=LocatorStrategy.NAME_ATTR, value="old_name")),
                    checkpoint=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Done"))],
        success=Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Done"),
        policy=ArtifactPolicy(allowed_url_prefixes=[BASE]),
    )


def _stuck_surface():
    s = FakeSurface()  # "Member field" starts unresolvable -> stuck
    s.transitions["Member field"] = lambda fs: fs.texts.add("Done")
    return s


def _operator_that_fixes():
    def fix(surface):
        surface.resolvable.add("Member field")
        return "operator re-identified the field and clicked it"
    return MockOperatorHandler(actions=[fix], decision="resolved")


def _engine(tmp_path, learner):
    return ReplayEngine(
        _stuck_surface(), RunLogger(str(tmp_path), "run", Redactor()),
        manage_surface_lifecycle=False, escalation=_operator_that_fixes(),
        learner=learner, proposals_dir=os.path.join(str(tmp_path), "proposals"),
    )


def test_human_fix_produces_llm_candidate_override(tmp_path):
    synth = MockSynthesizer({"strategy": "name_attr", "value": "member_id_v2",
                             "role": None, "exact": False})
    r = _engine(tmp_path, synth).replay(_artifact(), {})
    assert r.status == ResultStatus.SUCCESS
    assert len(r.proposed_overrides) == 1
    ov = r.proposed_overrides[0]
    assert ov["status"] == "draft"                     # never auto-applied
    assert ov["source"] == "llm-assist"
    assert ov["old_selector"]["value"] == "old_name"
    assert ov["new_selector"]["value"] == "member_id_v2"
    assert ov["human_actions"]                          # captured what the human did
    # written to disk for review
    assert os.path.isdir(os.path.join(str(tmp_path), "proposals", "app.cap"))


def test_human_fix_without_learner_still_captures_actions(tmp_path):
    r = _engine(tmp_path, None).replay(_artifact(), {})
    assert r.status == ResultStatus.SUCCESS
    ov = r.proposed_overrides[0]
    assert ov["source"] == "human"
    assert ov["new_selector"] is None                  # no LLM candidate, but captured
    assert ov["human_actions"]


def test_selector_policy_rejects_bad_candidates():
    assert selector_policy_ok({"strategy": "name_attr", "value": "x"})
    assert not selector_policy_ok({"strategy": "made_up", "value": "x"})
    assert not selector_policy_ok({"strategy": "css", "value": ""})
    assert not selector_policy_ok({"strategy": "role", "value": "Search"})  # role needs 'role'
