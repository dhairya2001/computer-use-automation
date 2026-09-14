"""The result contract and error taxonomy for replay.

This is the single most important distinction in the system (see the glossary:
"Business outcome vs. failure" is called out as the most common design mistake).

A replay ends in exactly one of three top-level states:

  SUCCESS
      The flow completed and the success checkpoint held. Declared outputs are
      returned to the caller.

  BUSINESS_OUTCOME
      A legitimate, expected result the caller needs to know about -- e.g.
      "no such member", "permission denied". This is NOT a crash. It carries a
      stable `code` and message. The caller branches on it.

  FAILED
      A hard failure. Carries a structured `ReplayFailure` with enough detail to
      debug: which step, what was expected, what was observed, and a FailureKind.

Recoverable conditions (a known interstitial, transient slowness) are a fourth
category, but they never surface as a result: replay remediates them inline
(bounded) and continues. If remediation is exhausted, that becomes a FAILED with
kind RECOVERY_EXHAUSTED.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ResultStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"


class FailureKind(str, Enum):
    ELEMENT_NOT_FOUND = "element_not_found"       # no selector resolved a control
    CHECKPOINT_FAILED = "checkpoint_failed"       # a step's post-condition did not hold
    SUCCESS_CONDITION_FAILED = "success_condition_failed"
    POLICY_VIOLATION = "policy_violation"         # blocked by allowlist / risk policy
    SESSION_EXPIRED = "session_expired"           # detected auth/session timeout
    TIMEOUT = "timeout"                           # slow load never resolved
    RECOVERY_EXHAUSTED = "recovery_exhausted"     # known condition, remediation failed
    NEEDS_HUMAN = "needs_human"                   # escalated to a human operator
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass
class ReplayFailure:
    kind: FailureKind
    step_id: Optional[str]
    expected: str = ""
    observed: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "step_id": self.step_id,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }
