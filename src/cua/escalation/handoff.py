"""Human-in-the-loop escalation and control handoff.

The load-bearing idea is the *control-transfer model*, not the operator UI (which
the brief lets us mock):

  * There is always exactly one holder of control: AUTOMATION or HUMAN. `SessionControl`
    tracks it, and every transfer is logged, so at any moment you can answer
    "who is (or should be) in control?".

  * Escalation is synchronous *pause*. When replay gets stuck it constructs an
    `InterventionRequest` (carrying capability, step, reason, url, and evidence),
    routes it through an `InterventionQueue`, and blocks. Control is ceded to the
    human.

  * The human operates the SAME live session -- the same `Surface` object wrapping
    the same browser page the automation was using. Nothing is re-created. Whatever
    they do lands on that page; the automation resumes from the resulting state.

  * On resume, control returns to AUTOMATION and the engine re-checks its step, so
    the human's manual work is picked up seamlessly.

Two handlers implement the operator side:
  * HumanConsoleHandler -- a real (headed) handoff: it prints the request, then
    waits for a human to act in the visible browser and press Enter.
  * MockOperatorHandler -- a scripted stand-in operator that performs a given set
    of actions on the live surface. This is the "bare/mock operator surface" used
    in automated demos and tests; the mechanism it exercises is the real one.
"""
from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


class ControlHolder(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class SessionControl:
    """Single source of truth for who controls the live session."""

    def __init__(self, logger=None):
        self.holder = ControlHolder.AUTOMATION
        self._logger = logger
        self.history: list[dict[str, Any]] = []

    def _record(self, holder: ControlHolder, reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "holder": holder.value,
            "reason": reason,
        }
        self.history.append(entry)
        if self._logger:
            self._logger.log("control_transfer", **entry)

    def cede_to_human(self, reason: str) -> None:
        self.holder = ControlHolder.HUMAN
        self._record(self.holder, reason)

    def return_to_automation(self, reason: str = "handoff complete") -> None:
        self.holder = ControlHolder.AUTOMATION
        self._record(self.holder, reason)


@dataclass
class InterventionRequest:
    intervention_id: str
    capability_id: str
    goal: str
    current_step: Optional[str]
    reason: str
    current_url: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evidence_dir: Optional[str] = None

    @classmethod
    def from_context(cls, context: dict[str, Any], evidence_dir: Optional[str]) -> "InterventionRequest":
        return cls(
            intervention_id=f"int-{uuid.uuid4().hex[:8]}",
            capability_id=context.get("capability_id", "?"),
            goal=context.get("goal", ""),
            current_step=context.get("current_step"),
            reason=context.get("reason", ""),
            current_url=context.get("current_url", ""),
            evidence_dir=evidence_dir,
        )


@dataclass
class InterventionResolution:
    intervention_id: str
    status: str                      # "resolved" | "aborted"
    actions_recorded: list[str] = field(default_factory=list)
    note: str = ""


class InterventionQueue:
    """Routing surface for intervention requests.

    In production this would be a durable queue / API that an operator console
    consumes and that fans out to a real notification channel (Slack, email, pager).
    Here it does two concrete things so the handoff is inspectable:
      * keeps in-memory pending/resolved lists, and
      * if `inbox_dir` is given, writes ONE JSON file per request into that folder
        (status "pending"), then flips it to "resolved" when the human is done.
    That folder IS "where the request goes" -- a human (or an operator UI) watches
    it, opens the JSON (which points at the screenshot/evidence), and acts.
    """

    def __init__(self, logger=None, inbox_dir: Optional[str] = None):
        self._logger = logger
        self.inbox_dir = inbox_dir
        self.pending: list[InterventionRequest] = []
        self.resolved: list[InterventionResolution] = []
        if inbox_dir:
            os.makedirs(inbox_dir, exist_ok=True)

    def _path(self, intervention_id: str) -> str:
        return os.path.join(self.inbox_dir, f"{intervention_id}.json")

    def enqueue(self, req: InterventionRequest) -> None:
        self.pending.append(req)
        if self.inbox_dir:
            record = asdict(req)
            record["status"] = "pending"
            record["resolution"] = None
            try:
                with open(self._path(req.intervention_id), "w", encoding="utf-8") as fh:
                    json.dump(record, fh, indent=2)
            except OSError:
                pass
        if self._logger:
            self._logger.log("intervention_enqueued",
                             intervention_id=req.intervention_id,
                             capability_id=req.capability_id,
                             step=req.current_step, reason=req.reason,
                             inbox=self._path(req.intervention_id) if self.inbox_dir else None)

    def mark_resolved(self, res: InterventionResolution) -> None:
        self.pending = [r for r in self.pending if r.intervention_id != res.intervention_id]
        self.resolved.append(res)
        if self.inbox_dir:
            try:
                path = self._path(res.intervention_id)
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
                record["status"] = res.status
                record["resolution"] = asdict(res)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, indent=2)
            except (OSError, json.JSONDecodeError):
                pass


class EscalationHandler(ABC):
    """Operator side of the handoff."""

    def __init__(self, queue: Optional[InterventionQueue] = None):
        self.queue = queue or InterventionQueue()

    @abstractmethod
    def operate(self, req: InterventionRequest, surface, logger) -> InterventionResolution:
        """Do the human work on the SAME live `surface`. Return a resolution."""

    def request_intervention(self, context: dict[str, Any], surface, logger) -> InterventionResolution:
        req = InterventionRequest.from_context(context, getattr(logger, "evidence_dir", None))
        self.queue.enqueue(req)
        control = SessionControl(logger=logger)
        control.cede_to_human(reason=req.reason)
        try:
            resolution = self.operate(req, surface, logger)
        finally:
            control.return_to_automation()
        self.queue.mark_resolved(resolution)
        return resolution


class MockOperatorHandler(EscalationHandler):
    """A scripted stand-in operator.

    `actions` is a list of callables `(surface) -> str` (the return string describes
    what was done, for the audit trail). Each runs against the SAME live session.
    `decision` controls the final outcome: "resolved" (default) or "aborted".
    """

    def __init__(self, actions: Optional[list[Callable[[Any], str]]] = None,
                 decision: str = "resolved", note: str = "handled by mock operator",
                 queue: Optional[InterventionQueue] = None):
        super().__init__(queue)
        self.actions = actions or []
        self.decision = decision
        self.note = note

    def operate(self, req: InterventionRequest, surface, logger) -> InterventionResolution:
        recorded: list[str] = []
        for act in self.actions:
            try:
                desc = act(surface) or "operator action"
            except Exception as e:
                desc = f"operator action error: {e}"
            recorded.append(desc)
            if logger:
                logger.log("human_action", intervention_id=req.intervention_id, description=desc)
        return InterventionResolution(
            intervention_id=req.intervention_id,
            status=self.decision,
            actions_recorded=recorded,
            note=self.note,
        )


class HumanConsoleHandler(EscalationHandler):
    """A real handoff for a headed browser: pause, let a person act, then resume.

    Blocks on input(), so it is only appropriate for an attended/headed session.
    """

    def operate(self, req: InterventionRequest, surface, logger) -> InterventionResolution:
        # Two kinds of pause need different human instructions:
        #  - APPROVAL: a risky/irreversible step is waiting for a yes/no. The human
        #    should NOT click it themselves (the automation performs it on approval).
        #  - STUCK: the automation can't proceed; the human completes the step by
        #    hand in the live window, then hands control back.
        is_approval = "confirmation" in (req.reason or "").lower()

        print("\n" + "=" * 70)
        print("  HUMAN INTERVENTION REQUESTED"
              + ("  (approval)" if is_approval else "  (take over)"))
        print(f"  Intervention: {req.intervention_id}")
        print(f"  Capability:   {req.capability_id}")
        print(f"  Step:         {req.current_step}")
        print(f"  Reason:       {req.reason}")
        print(f"  Current URL:  {req.current_url}")
        if req.evidence_dir:
            print(f"  Evidence:     {req.evidence_dir}")
        print("-" * 70)
        if is_approval:
            print("  A risky/irreversible step is awaiting YOUR APPROVAL.")
            print("  Review the page in the browser window. Do NOT click it yourself --")
            print("  on approval the automation performs the step for you.")
            prompt = "  [approve/reject] > "
        else:
            print("  The automation is STUCK. You now control the SAME live browser window.")
            print("  Complete this step by hand, then hand control back.")
            prompt = "  [done/abort] > "
        print("=" * 70)

        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""

        if is_approval:
            # [approve / reject]. Anything not clearly 'approve'/'yes' is a safe reject.
            if answer.startswith(("approve", "yes", "y")) and not answer.startswith("n"):
                return InterventionResolution(req.intervention_id, "resolved",
                                              actions_recorded=["operator approved the risky action"],
                                              note="approved at console")
            return InterventionResolution(req.intervention_id, "aborted",
                                          note="operator rejected the risky action")
        # stuck: [done / abort]. Anything not clearly 'done'/'resume' is a safe abort.
        if answer.startswith(("done", "resume", "d", "yes", "y")):
            return InterventionResolution(req.intervention_id, "resolved",
                                          actions_recorded=["operator completed the step manually in the live session"],
                                          note="operator completed manual steps")
        return InterventionResolution(req.intervention_id, "aborted",
                                      note="operator aborted at console")
