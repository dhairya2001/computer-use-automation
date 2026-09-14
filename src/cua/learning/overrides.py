"""Learn from human interventions.

When a human takes over a stuck replay and fixes it, that fix should not evaporate
into a log line -- it should become a *durable, reviewable improvement* to the
capability. This module turns an intervention into a **proposed override**: a
bounded, policy-checked patch to a single step's locator, saved as a DRAFT for a
human to approve (draft -> approved), never silently self-applied.

Two ingredients:
  * capture: what step was stuck, its failed selector, what the human did, and the
    page state after the fix;
  * synthesis (optional, LLM-assisted and bounded): given that context, propose ONE
    replacement selector. It is policy-checked (allowed strategy, non-empty) before
    being recorded. The model proposes; it never acts on the live session and never
    edits the artifact directly.

This is the safe version of "the automation learns": every learned change is a
draft override a reviewer promotes, matching the artifact's draft/approved gate.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..agent.llm import _extract_json, anthropic_complete
from ..schema import LocatorStrategy

_ALLOWED_STRATEGIES = {s.value for s in LocatorStrategy}


def selector_policy_ok(selector: dict) -> bool:
    """A proposed selector must use an allowed strategy and carry a value; a
    'role' selector must name a role. This bounds what a model can propose."""
    if not isinstance(selector, dict):
        return False
    strat = selector.get("strategy")
    if strat not in _ALLOWED_STRATEGIES or not selector.get("value"):
        return False
    if strat == "role" and not selector.get("role"):
        return False
    return True


@dataclass
class ProposedOverride:
    capability_id: str
    capability_version: str
    step_id: str
    reason: str
    source: str                       # "human" | "llm-assist"
    old_selector: Optional[dict]
    new_selector: Optional[dict]
    human_actions: list
    before_url: str
    after_url: str
    status: str = "draft"             # draft -> approved (a reviewer promotes it)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


def save_proposal(p: ProposedOverride, proposals_dir: str = "proposals") -> str:
    d = os.path.join(proposals_dir, p.capability_id)
    os.makedirs(d, exist_ok=True)
    stamp = p.created_at.replace(":", "").replace("-", "")[:15]
    path = os.path.join(d, f"{p.step_id}-{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(p.to_dict(), fh, indent=2)
    return path


_SYNTH_SYSTEM = """\
You maintain UI automation for a back-office banking app. A recorded step's locator
failed and a human operator fixed it by hand. Given the step's description, the OLD
(failed) selector, the CURRENT page (accessibility tree + form-field cues), and what
the human did, propose ONE robust replacement selector for that same control.

Prefer human-meaningful strategies in this order: role (ARIA role + accessible
name) > label > placeholder > text > name_attr > css > xpath. Only fall back to
css/xpath if nothing better identifies the control.

Return ONLY JSON, no prose:
{"selector": {"strategy": "...", "value": "...", "role": null_or_string, "exact": false},
 "reasoning": "why this is robust"}
"""


class SelectorSynthesizer(ABC):
    @abstractmethod
    def propose(self, *, step_description: str, old_selector: Optional[dict],
                observation_text: str, human_actions: list) -> Optional[dict]:
        """Return a policy-valid replacement selector dict, or None."""

    def name(self) -> str:
        return "synthesizer"


class AnthropicSynthesizer(SelectorSynthesizer):
    def __init__(self, model: Optional[str] = None):
        self.model = model

    def name(self) -> str:
        return "anthropic"

    def propose(self, *, step_description, old_selector, observation_text, human_actions):
        user = (
            f"STEP: {step_description}\n"
            f"OLD (failed) SELECTOR: {json.dumps(old_selector)}\n"
            f"WHAT THE HUMAN DID: {json.dumps(human_actions)}\n\n"
            f"CURRENT PAGE:\n{observation_text[:3000]}\n\n"
            "Propose ONE replacement selector. Return ONLY the JSON object."
        )
        try:
            text = anthropic_complete(_SYNTH_SYSTEM, [{"role": "user", "content": user}],
                                      model=self.model, max_tokens=400)
            data = _extract_json(text)
        except Exception:
            return None
        sel = data.get("selector", data)
        return sel if selector_policy_ok(sel) else None


class MockSynthesizer(SelectorSynthesizer):
    """Returns a preset selector (for tests/offline demo)."""

    def __init__(self, selector: Optional[dict]):
        self._sel = selector

    def name(self) -> str:
        return "mock"

    def propose(self, *, step_description, old_selector, observation_text, human_actions):
        return self._sel if (self._sel and selector_policy_ok(self._sel)) else None
