"""Post-automation verification: LLM judge + human double-review.

This runs AFTER a replay completes successfully. It is deliberately OUTSIDE the
deterministic replay decision loop -- replay decides *what to do*; verification is
an independent *did it actually do the right thing?* check. It never feeds back
into the automation.

Two layers, in order (a bank wants both):

  1. LLM JUDGE  -- an independent model looks at the goal, the declared success
     condition, the outputs, the FINAL PAGE (frontend), and the BACKEND DATA
     (e.g. the record that should now exist), and returns pass / fail / uncertain
     with reasoning and per-check detail. Crucially it can catch a "false success":
     the checkpoint held but the wrong thing happened.

  2. HUMAN REVIEW -- because this is regulated financial activity, the LLM verdict
     is then routed to a human for a second sign-off (approve / reject). If no
     reviewer is attached, the result is left as `needs_human_review` (never
     auto-approved) -- fail-safe for banking.

The combined `overall` status is what a caller should trust:
    approved | rejected | needs_human_review | (skipped)
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..agent.llm import _extract_json, anthropic_complete

VERIFIER_SYSTEM = """\
You are an independent verification judge for an automated action performed on a
back-office banking application. You did NOT perform the action; you are checking it.

You are given: the GOAL, the declared SUCCESS CONDITION, the OUTPUTS the automation
returned, the FINAL PAGE the user would see (frontend), and BACKEND DATA (the stored
record that should reflect the action). Decide whether the goal was truly and
correctly accomplished -- not merely that a page loaded.

Be strict and specific. Watch for "false success": the automation reports success
but the frontend or backend shows the wrong member, wrong amount, wrong account
type, a missing record, or an error banner. Cross-check the outputs against BOTH the
frontend and the backend when possible.

Return ONLY a JSON object, no prose:
{
  "verdict": "pass" | "fail" | "uncertain",
  "confidence": 0.0-1.0,
  "reasoning": "one or two sentences",
  "checks": [ {"name": "short check name", "ok": true/false, "detail": "what you saw"} ]
}
"""


# --------------------------------------------------------------------------- #
# LLM judge
# --------------------------------------------------------------------------- #
class Judge(ABC):
    @abstractmethod
    def judge(self, system: str, user: str) -> dict[str, Any]: ...

    def model_name(self) -> Optional[str]:
        return None


class AnthropicJudge(Judge):
    def __init__(self, model: Optional[str] = None, max_tokens: int = 1024):
        self.model = model
        self.max_tokens = max_tokens

    def model_name(self) -> Optional[str]:
        return self.model or "claude (default)"

    def judge(self, system: str, user: str) -> dict[str, Any]:
        text = anthropic_complete(system, [{"role": "user", "content": user}],
                                  model=self.model, max_tokens=self.max_tokens)
        return _extract_json(text)


class MockJudge(Judge):
    """Deterministic judge for tests/offline demo. Returns a preset verdict."""

    def __init__(self, verdict: str = "pass", confidence: float = 0.9,
                 reasoning: str = "mock judge", checks: Optional[list] = None):
        self._v = {"verdict": verdict, "confidence": confidence,
                   "reasoning": reasoning, "checks": checks or []}

    def model_name(self) -> Optional[str]:
        return "mock-judge"

    def judge(self, system: str, user: str) -> dict[str, Any]:
        return dict(self._v)


# --------------------------------------------------------------------------- #
# Human review (second sign-off)
# --------------------------------------------------------------------------- #
class HumanReviewer(ABC):
    @abstractmethod
    def review(self, context: dict[str, Any]) -> dict[str, Any]:
        """Return {"status": "approved"|"rejected", "reviewer": str, "note": str}."""


class ConsoleReviewer(HumanReviewer):
    """Real second review at the terminal: shows the LLM verdict + evidence."""

    def review(self, context: dict[str, Any]) -> dict[str, Any]:
        print("\n" + "=" * 70)
        print("  HUMAN REVIEW REQUIRED (second sign-off)")
        print(f"  Capability : {context.get('capability_id')}")
        print(f"  Goal       : {context.get('goal')}")
        print(f"  Outputs    : {context.get('outputs')}")
        print(f"  LLM verdict: {context.get('llm_verdict')} "
              f"(confidence {context.get('llm_confidence')})")
        print(f"  LLM reason : {context.get('llm_reasoning')}")
        for c in context.get("llm_checks", []):
            print(f"     - [{'ok' if c.get('ok') else 'XX'}] {c.get('name')}: {c.get('detail')}")
        print(f"  Backend    : {context.get('backend_data')}")
        print("  Approve this action? Type 'approve' or 'reject'.")
        print("=" * 70)
        try:
            ans = input("  [approve/reject] > ").strip().lower()
        except EOFError:
            ans = "reject"
        note = ""
        try:
            note = input("  note (optional) > ").strip()
        except EOFError:
            pass
        status = "approved" if ans.startswith("a") else "rejected"
        return {"status": status, "reviewer": "console-operator", "note": note}


class MockReviewer(HumanReviewer):
    """Scripted reviewer for tests/demo."""

    def __init__(self, status: str = "approved", reviewer: str = "mock-reviewer",
                 note: str = "auto-approved by mock reviewer"):
        self._out = {"status": status, "reviewer": reviewer, "note": note}

    def review(self, context: dict[str, Any]) -> dict[str, Any]:
        return dict(self._out)


# --------------------------------------------------------------------------- #
# Result + orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class VerificationResult:
    llm_verdict: str = "skipped"              # pass | fail | uncertain | skipped
    llm_confidence: float = 0.0
    llm_reasoning: str = ""
    llm_checks: list[dict] = field(default_factory=list)
    model: Optional[str] = None
    human_review: dict = field(default_factory=lambda: {"status": "pending"})
    overall: str = "skipped"                  # approved | rejected | needs_human_review | skipped

    def to_dict(self) -> dict:
        return asdict(self)


class Verifier:
    """Runs the LLM judge, then (optionally) routes to a human for a second review."""

    def __init__(self, judge: Optional[Judge] = None, reviewer: Optional[HumanReviewer] = None):
        self.judge = judge
        self.reviewer = reviewer

    def run(self, *, goal: str, success_condition: str, outputs: dict[str, Any],
            frontend_text: str, backend_data: Any, capability_id: str = "") -> VerificationResult:
        result = VerificationResult()

        # --- Layer 1: LLM judge ---
        if self.judge is not None:
            user = self._build_prompt(goal, success_condition, outputs, frontend_text, backend_data)
            try:
                data = self.judge.judge(VERIFIER_SYSTEM, user)
                result.llm_verdict = str(data.get("verdict", "uncertain"))
                result.llm_confidence = float(data.get("confidence", 0.0) or 0.0)
                result.llm_reasoning = str(data.get("reasoning", ""))
                result.llm_checks = data.get("checks", []) or []
            except Exception as e:
                result.llm_verdict = "uncertain"
                result.llm_reasoning = f"verification error: {e}"
            result.model = self.judge.model_name()

        # --- Layer 2: human second sign-off ---
        if self.reviewer is not None:
            context = {
                "capability_id": capability_id, "goal": goal, "outputs": outputs,
                "llm_verdict": result.llm_verdict, "llm_confidence": result.llm_confidence,
                "llm_reasoning": result.llm_reasoning, "llm_checks": result.llm_checks,
                "backend_data": backend_data,
            }
            try:
                result.human_review = self.reviewer.review(context)
            except Exception as e:
                result.human_review = {"status": "pending", "note": f"review error: {e}"}
        else:
            # No reviewer attached: NEVER auto-approve a banking action.
            result.human_review = {"status": "pending",
                                   "note": "no human reviewer attached"}

        # --- Combined verdict ---
        hs = result.human_review.get("status")
        if hs == "approved":
            result.overall = "approved"
        elif hs == "rejected":
            result.overall = "rejected"
        elif self.judge is None and self.reviewer is None:
            result.overall = "skipped"
        else:
            result.overall = "needs_human_review"
        return result

    @staticmethod
    def _build_prompt(goal, success_condition, outputs, frontend_text, backend_data) -> str:
        return (
            f"GOAL:\n{goal}\n\n"
            f"DECLARED SUCCESS CONDITION:\n{success_condition}\n\n"
            f"OUTPUTS RETURNED BY THE AUTOMATION:\n{json.dumps(outputs, indent=2)}\n\n"
            f"FINAL PAGE (frontend):\n{frontend_text[:2500]}\n\n"
            f"BACKEND DATA:\n{json.dumps(backend_data, indent=2)[:2000]}\n\n"
            "Judge whether the goal was truly accomplished. Return ONLY the JSON object."
        )
