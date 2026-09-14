"""Structured replay result returned to the caller (an AI agent, in production)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import ReplayFailure, ResultStatus


@dataclass
class ReplayResult:
    status: ResultStatus
    capability_id: str
    capability_version: str
    run_id: str
    evidence_dir: Optional[str] = None

    # SUCCESS
    outputs: dict[str, Any] = field(default_factory=dict)

    # BUSINESS_OUTCOME
    outcome_code: Optional[str] = None
    outcome_message: Optional[str] = None

    # FAILED
    failure: Optional[ReplayFailure] = None

    # Bookkeeping
    steps_completed: int = 0
    recoveries_applied: list[str] = field(default_factory=list)
    escalation_id: Optional[str] = None

    # Post-run verification (LLM judge + human second sign-off), when enabled.
    verification: Optional[dict] = None

    # Draft overrides proposed from human interventions during this run.
    proposed_overrides: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status.value,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "run_id": self.run_id,
            "evidence_dir": self.evidence_dir,
            "steps_completed": self.steps_completed,
            "recoveries_applied": self.recoveries_applied,
        }
        if self.verification is not None:
            d["verification"] = self.verification
        if self.proposed_overrides:
            d["proposed_overrides"] = self.proposed_overrides
        if self.status == ResultStatus.SUCCESS:
            d["outputs"] = self.outputs
        elif self.status == ResultStatus.BUSINESS_OUTCOME:
            d["outcome_code"] = self.outcome_code
            d["outcome_message"] = self.outcome_message
        elif self.status == ResultStatus.FAILED:
            d["failure"] = self.failure.to_dict() if self.failure else None
            if self.escalation_id:
                d["escalation_id"] = self.escalation_id
        return d
