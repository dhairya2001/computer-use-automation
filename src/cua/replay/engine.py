"""Deterministic replay engine -- the production execution path.

No LLM is in the decision loop here. Given an artifact and typed params, it:

  * validates params against the artifact contract,
  * enforces the safety policy on every step,
  * executes steps with stable targeting and explicit waits,
  * remediates known recoverable conditions inline (bounded),
  * classifies every non-success ending as either a declared BUSINESS_OUTCOME or a
    structured hard FAILURE,
  * escalates to a human when configured and stuck,
  * returns declared outputs on success.
"""
from __future__ import annotations

from typing import Any, Optional

from ..observability.logging import RunLogger
from ..safety.policy import PolicyDecision, PolicyViolation, RiskMode, SafetyPolicy
from ..schema import (
    ActionType,
    BusinessOutcomeDetector,
    CapabilityArtifact,
    CapabilityStatus,
    Checkpoint,
    CheckpointKind,
    Step,
    render_template,
)
from ..surface.base import ElementNotResolved, Surface
from .errors import FailureKind, ReplayFailure, ResultStatus
from .result import ReplayResult

# Built-in detector for auth/session timeout, independent of the artifact.
_SESSION_EXPIRED = Checkpoint(
    kind=CheckpointKind.TEXT_PRESENT,
    value="session has timed out",
    description="Auth/session timeout",
)


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        logger: RunLogger,
        risk_mode: RiskMode = RiskMode.CONFIRM,
        escalation: Optional[Any] = None,
        manage_surface_lifecycle: bool = True,
        verifier: Optional[Any] = None,
        backend_fetch: Optional[Any] = None,
        learner: Optional[Any] = None,
        proposals_dir: str = "proposals",
    ):
        self.surface = surface
        self.logger = logger
        self.risk_mode = risk_mode
        self.escalation = escalation
        self.manage_surface_lifecycle = manage_surface_lifecycle
        # Post-run verification (runs after a successful replay, before the surface
        # closes). `verifier` is a cua.verify.Verifier; `backend_fetch(artifact,
        # params) -> data` supplies the backend truth to cross-check against.
        self.verifier = verifier
        self.backend_fetch = backend_fetch
        # Learning: when a human fixes a stuck step, capture it as a draft override.
        # `learner` is an optional cua.learning.SelectorSynthesizer.
        self.learner = learner
        self.proposals_dir = proposals_dir

        self._outputs: dict[str, Any] = {}
        self._recoveries_applied: list[str] = []
        self._steps_completed: int = 0
        self._verification: Optional[dict] = None
        self._proposed_overrides: list[dict] = []
        self._last_resolution: Optional[Any] = None
        self._policy: Optional[SafetyPolicy] = None
        self._artifact: Optional[CapabilityArtifact] = None
        self._params: dict[str, Any] = {}

    # ================================================================== #
    def replay(self, artifact: CapabilityArtifact, params: dict[str, Any]) -> ReplayResult:
        self._artifact = artifact
        self._params = params
        self._outputs = {}
        self._recoveries_applied = []
        self._steps_completed = 0
        self._verification = None
        self._proposed_overrides = []
        self._last_resolution = None

        artifact.validate_params(params)
        for name in artifact.secret_input_names():
            if name in params:
                self.logger.redactor.add_secret(str(params[name]))

        # Safety policy: enforce a URL allowlist even if the artifact declared none.
        from urllib.parse import urlparse

        _p = urlparse(artifact.target.base_url)
        _origin = f"{_p.scheme}://{_p.netloc}/"
        allow = artifact.policy.allowed_url_prefixes or [_origin]
        # An approved capability may run risky steps unattended; a draft may not.
        effective_risk_mode = (
            RiskMode.ALLOW
            if artifact.capability.status == CapabilityStatus.APPROVED and self.risk_mode != RiskMode.BLOCK
            else self.risk_mode
        )
        self._policy = SafetyPolicy(
            allowed_url_prefixes=allow,
            allowed_action_types=artifact.policy.allowed_action_types,
            risk_mode=effective_risk_mode,
        )

        self.logger.log(
            "replay_start",
            capability_id=artifact.capability.id,
            capability_version=artifact.capability.version,
            status=artifact.capability.status.value,
            params={k: ("<secret>" if k in artifact.secret_input_names() else v)
                    for k, v in params.items()},
            risk_mode=effective_risk_mode.value,
        )

        if self.manage_surface_lifecycle:
            self.surface.start()
        try:
            return self._run(artifact, params)
        except Exception as e:  # defensive: never let replay crash the caller
            self.logger.capture_failure(self.surface, "unexpected")
            return self._failed(FailureKind.UNEXPECTED_ERROR, None, detail=str(e))
        finally:
            if self.manage_surface_lifecycle:
                self.surface.close()

    # ================================================================== #
    def _run(self, artifact: CapabilityArtifact, params: dict[str, Any]) -> ReplayResult:
        # Entry point.
        try:
            self._policy.check_url(artifact.target.base_url)
        except PolicyViolation as e:
            return self._failed(FailureKind.POLICY_VIOLATION, None, detail=str(e))
        self.surface.goto(artifact.target.base_url)

        for step in artifact.steps:
            # Before each step: remediate known conditions, then check for a business
            # outcome that may already be on screen (e.g. a not-found page).
            self._auto_recover(artifact, params)
            bo = self._scan_business_outcomes(artifact, params)
            if bo is not None:
                return self._business_outcome(bo)

            terminal = self._run_step(artifact, step, params)
            if terminal is not None:
                return terminal
            self._steps_completed += 1

        # End of flow: final business-outcome scan, then the success condition.
        self._auto_recover(artifact, params)
        bo = self._scan_business_outcomes(artifact, params)
        if bo is not None:
            return self._business_outcome(bo)

        success = Checkpoint(
            kind=artifact.success.kind,
            value=render_template(artifact.success.value, params) or artifact.success.value,
            description=artifact.success.description,
        )
        held = self.surface.wait_for(success, 8000)
        self.logger.log("success_check", kind=success.kind, value=success.value, held=held)
        if held:
            self.logger.capture_step(self.surface, "success")
            # Verify BEFORE the surface closes: the LLM judge + human review look at
            # the final page and the backend record to confirm it truly worked.
            self._run_verification(artifact, params, success)
            return self._success()

        # Success did not hold and no business outcome matched -> hard failure.
        self.logger.capture_failure(self.surface, "success_failed")
        return self._failed(
            FailureKind.SUCCESS_CONDITION_FAILED, None,
            expected=f"{success.kind.value}={success.value}",
            observed=self._observed_summary(),
        )

    # ================================================================== #
    def _run_step(self, artifact, step: Step, params) -> Optional[ReplayResult]:
        """Execute one step. Return a terminal ReplayResult, or None to continue."""
        # Safety gate.
        url_for_check = (
            render_template(step.url, params) if step.action == ActionType.GOTO
            else self.surface.current_url()
        )
        try:
            decision = self._policy.enforce(step.action, step.risk, step.description or step.id,
                                            url_for_check)
        except PolicyViolation as e:
            self.logger.capture_failure(self.surface, "policy")
            return self._failed(FailureKind.POLICY_VIOLATION, step.id, detail=str(e))

        if decision == PolicyDecision.NEEDS_CONFIRMATION:
            # Risky step, unattended, capability not approved -> escalate to a human.
            res = self._escalate(
                artifact, step,
                reason=f"Risky/irreversible step '{step.description or step.id}' "
                       f"requires human confirmation.",
            )
            if res is not None:
                return res  # human aborted -> terminal
            # else: human took control / approved -> fall through and execute.

        self.logger.next_step_index()
        self.logger.log("step_begin", step_id=step.id, action=step.action.value,
                        risk=step.risk.value)

        # Execute the action, with one recover-and-retry on a resolve failure.
        try:
            self._do_action(step, params)
        except ElementNotResolved as e:
            # Maybe a known interstitial is covering the control, or a business
            # outcome page replaced the expected screen.
            bo = self._scan_business_outcomes(artifact, params)
            if bo is not None:
                return self._business_outcome(bo)
            if self._detect_session_expired():
                self.logger.capture_failure(self.surface, "session_expired")
                return self._failed(FailureKind.SESSION_EXPIRED, step.id,
                                    detail="Session/auth timeout detected.")
            if self._auto_recover(artifact, params):
                try:
                    self._do_action(step, params)
                except ElementNotResolved as e2:
                    return self._maybe_escalate_or_fail(artifact, step, e2)
            else:
                return self._maybe_escalate_or_fail(artifact, step, e)

        # Verify the step's checkpoint.
        if step.checkpoint is not None:
            cp = Checkpoint(
                kind=step.checkpoint.kind,
                value=render_template(step.checkpoint.value, params) or step.checkpoint.value,
                description=step.checkpoint.description,
            )
            held = self.surface.wait_for(cp, step.timeout_ms)
            self.logger.log("checkpoint", step_id=step.id, kind=cp.kind.value,
                            value=cp.value, held=held)
            if not held:
                # A failed checkpoint is where business-outcome-vs-failure is decided.
                bo = self._scan_business_outcomes(artifact, params)
                if bo is not None:
                    return self._business_outcome(bo)
                if self._detect_session_expired():
                    self.logger.capture_failure(self.surface, "session_expired")
                    return self._failed(FailureKind.SESSION_EXPIRED, step.id,
                                        detail="Session/auth timeout detected.")
                if self._auto_recover(artifact, params) and self.surface.wait_for(cp, 3000):
                    pass  # recovered
                else:
                    self.logger.capture_failure(self.surface, "checkpoint")
                    return self._failed(
                        FailureKind.CHECKPOINT_FAILED, step.id,
                        expected=f"{cp.kind.value}={cp.value}",
                        observed=self._observed_summary(),
                    )

        self.logger.capture_step(self.surface, step.id)
        return None

    def _do_action(self, step: Step, params) -> None:
        act = step.action
        value = render_template(step.value, params) if step.value else None
        if act == ActionType.GOTO:
            url = render_template(step.url, params)
            self._policy.check_url(url)
            self.surface.goto(url)
        elif act == ActionType.CLICK:
            self.surface.click(step.target)
        elif act == ActionType.FILL:
            self.surface.fill(step.target, value or "")
        elif act == ActionType.SELECT:
            self.surface.select(step.target, value or "")
        elif act == ActionType.PRESS:
            self.surface.press(value or "Enter", step.target)
        elif act == ActionType.WAIT_FOR:
            if step.checkpoint:
                self.surface.wait_for(step.checkpoint, step.timeout_ms)
        elif act == ActionType.EXTRACT:
            val = self.surface.extract(step.target, step.extract_attr)
            self._outputs[step.extract_as] = val
            self.logger.log("extracted", name=step.extract_as, value=val)
        elif act == ActionType.DISMISS_IF_PRESENT:
            if self.surface.is_present(step.target):
                self.surface.click(step.target)
        elif act == ActionType.ASSERT:
            if step.checkpoint and not self.surface.check(step.checkpoint):
                raise ElementNotResolved(step.target) if step.target else RuntimeError("assert failed")

    # ================================================================== #
    # Business outcomes / recovery / session
    # ================================================================== #
    def _scan_business_outcomes(self, artifact, params) -> Optional[BusinessOutcomeDetector]:
        for bo in artifact.business_outcomes:
            cp = Checkpoint(kind=bo.when.kind,
                            value=render_template(bo.when.value, params) or bo.when.value)
            if self.surface.check(cp):
                self.logger.log("business_outcome_detected", code=bo.code, when=cp.value)
                return bo
        return None

    def _auto_recover(self, artifact, params) -> bool:
        """Run any matching recovery rules (bounded). Return True if something applied."""
        applied_any = False
        for rule in artifact.recoveries:
            cp = Checkpoint(kind=rule.when.kind,
                            value=render_template(rule.when.value, params) or rule.when.value)
            attempts = 0
            while attempts < rule.max_attempts and self.surface.check(cp):
                attempts += 1
                self.logger.log("recovery_apply", rule=rule.name, attempt=attempts)
                for s in rule.do:
                    try:
                        self._do_action(s, params)
                    except Exception as e:
                        self.logger.log("recovery_step_error", rule=rule.name, error=str(e))
                self._recoveries_applied.append(rule.name)
                applied_any = True
        return applied_any

    def _detect_session_expired(self) -> bool:
        return self.surface.check(_SESSION_EXPIRED) or self.surface.check(
            Checkpoint(kind=CheckpointKind.TEXT_PRESENT, value="Session Expired")
        )

    # ================================================================== #
    # Escalation
    # ================================================================== #
    def _maybe_escalate_or_fail(self, artifact, step, err) -> ReplayResult:
        # With no operator configured, an unresolved control is a clean, debuggable
        # hard failure -- not a human escalation.
        if self.escalation is None:
            self.logger.capture_failure(self.surface, "element_not_found")
            return self._failed(
                FailureKind.ELEMENT_NOT_FOUND, step.id,
                expected=f"resolve control: {step.target.description if step.target else '?'}",
                observed=self._observed_summary(),
                detail=str(err),
            )
        before_url = self.surface.current_url()
        res = self._escalate(artifact, step, reason=f"Could not resolve control for step "
                                                    f"'{step.id}': {err}")
        if res is not None:
            return res
        # Human took control; re-attempt the step once more.
        try:
            self._do_action(step, self._params)
        except Exception as e2:
            self.logger.capture_failure(self.surface, "post_handoff_failed")
            return self._failed(FailureKind.ELEMENT_NOT_FOUND, step.id, detail=str(e2))
        # The human's manual fix succeeded -> capture it as a draft override so the
        # capability can learn from it (bounded, policy-checked, needs approval).
        self._capture_override_from_fix(artifact, step, before_url)
        return None  # resume

    def _capture_override_from_fix(self, artifact, step, before_url: str) -> None:
        """Turn a human's stuck-step fix into a draft ProposedOverride."""
        try:
            from ..learning.overrides import ProposedOverride, save_proposal
        except Exception:
            return
        try:
            obs = self.surface.observe()
            after_url = obs.url
            obs_text = obs.render_for_llm()
        except Exception:
            after_url, obs_text = self.surface.current_url(), ""

        human_actions = (self._last_resolution.actions_recorded
                         if self._last_resolution is not None else [])
        old_selector = (step.target.primary.model_dump()
                        if (step.target and step.target.primary) else None)

        new_selector = None
        source = "human"
        if self.learner is not None:
            new_selector = self.learner.propose(
                step_description=(step.target.description if step.target else step.id),
                old_selector=old_selector,
                observation_text=obs_text,
                human_actions=human_actions,
            )
            if new_selector is not None:
                source = "llm-assist"

        proposal = ProposedOverride(
            capability_id=artifact.capability.id,
            capability_version=artifact.capability.version,
            step_id=step.id,
            reason="Human resolved a stuck step; proposing a durable locator override.",
            source=source,
            old_selector=old_selector,
            new_selector=new_selector,
            human_actions=human_actions,
            before_url=before_url,
            after_url=after_url,
        )
        try:
            path = save_proposal(proposal, self.proposals_dir)
        except Exception as e:
            self.logger.log("override_proposal_error", error=str(e))
            return
        self._proposed_overrides.append(proposal.to_dict())
        self.logger.log("override_proposed", step_id=step.id, path=path,
                        source=source, has_candidate=new_selector is not None)

    def _escalate(self, artifact, step, reason: str) -> Optional[ReplayResult]:
        """Raise a human intervention on the LIVE session. Returns a terminal
        ReplayResult if the human aborted / no handler; None if resolved (resume)."""
        if self.escalation is None:
            self.logger.log("escalation_unavailable", reason=reason)
            return self._failed(FailureKind.NEEDS_HUMAN, step.id if step else None, detail=reason)

        context = {
            "capability_id": artifact.capability.id,
            "goal": artifact.capability.description,
            "current_step": step.id if step else None,
            "reason": reason,
            "current_url": self.surface.current_url(),
        }
        self.logger.log("escalation_raised", **context)
        self.logger.capture_failure(self.surface, "escalation")
        resolution = self.escalation.request_intervention(context, self.surface, self.logger)
        self._last_resolution = resolution
        self.logger.log("escalation_resolved", status=resolution.status,
                        actions=resolution.actions_recorded)
        if resolution.status == "resolved":
            return None  # resume the flow on the same live session
        # aborted / timed out
        res = self._failed(FailureKind.NEEDS_HUMAN, step.id if step else None,
                           detail=f"Human aborted: {resolution.note}")
        res.escalation_id = resolution.intervention_id
        return res

    # ================================================================== #
    # Post-run verification (LLM judge + human second sign-off)
    # ================================================================== #
    def _run_verification(self, artifact, params, success: Checkpoint) -> None:
        if self.verifier is None:
            return
        # Frontend evidence: the final page the automation left behind.
        try:
            obs = self.surface.observe()
            frontend = f"URL: {obs.url}\nTITLE: {obs.title}\nVISIBLE TEXT: {obs.text_excerpt}"
        except Exception:
            frontend = "(frontend unavailable)"
        # Backend evidence: the stored record that should reflect the action.
        backend: Any = None
        if self.backend_fetch is not None:
            try:
                backend = self.backend_fetch(artifact, params)
            except Exception as e:
                backend = {"error": f"backend_fetch failed: {e}"}
        self.logger.log("verification_start", has_backend=backend is not None)
        vr = self.verifier.run(
            goal=artifact.capability.description,
            success_condition=f"{success.kind.value}={success.value} ({success.description})",
            outputs=self._outputs,
            frontend_text=frontend,
            backend_data=backend,
            capability_id=artifact.capability.id,
        )
        self._verification = vr.to_dict()
        self.logger.log("verification_done", overall=vr.overall,
                        llm_verdict=vr.llm_verdict, human=vr.human_review.get("status"))

    # ================================================================== #
    # Result builders
    # ================================================================== #
    def _success(self) -> ReplayResult:
        r = ReplayResult(
            status=ResultStatus.SUCCESS,
            capability_id=self._artifact.capability.id,
            capability_version=self._artifact.capability.version,
            run_id=self.logger.run_id,
            evidence_dir=self.logger.evidence_dir,
            outputs=dict(self._outputs),
            steps_completed=self._steps_completed,
            recoveries_applied=list(self._recoveries_applied),
            verification=self._verification,
            proposed_overrides=list(self._proposed_overrides),
        )
        self._write_summary(r)
        return r

    def _business_outcome(self, bo: BusinessOutcomeDetector) -> ReplayResult:
        r = ReplayResult(
            status=ResultStatus.BUSINESS_OUTCOME,
            capability_id=self._artifact.capability.id,
            capability_version=self._artifact.capability.version,
            run_id=self.logger.run_id,
            evidence_dir=self.logger.evidence_dir,
            outcome_code=bo.code,
            outcome_message=bo.message,
            outputs=dict(self._outputs),
            steps_completed=self._steps_completed,
            recoveries_applied=list(self._recoveries_applied),
            proposed_overrides=list(self._proposed_overrides),
        )
        self._write_summary(r)
        return r

    def _failed(self, kind: FailureKind, step_id, expected="", observed="", detail="") -> ReplayResult:
        r = ReplayResult(
            status=ResultStatus.FAILED,
            capability_id=self._artifact.capability.id if self._artifact else "?",
            capability_version=self._artifact.capability.version if self._artifact else "?",
            run_id=self.logger.run_id,
            evidence_dir=self.logger.evidence_dir,
            failure=ReplayFailure(kind=kind, step_id=step_id, expected=expected,
                                  observed=observed, detail=detail),
            steps_completed=self._steps_completed,
            recoveries_applied=list(self._recoveries_applied),
            proposed_overrides=list(self._proposed_overrides),
        )
        self._write_summary(r)
        return r

    def _observed_summary(self) -> str:
        try:
            obs = self.surface.observe()
            return f"url={obs.url} title={obs.title!r} text={obs.text_excerpt[:200]!r}"
        except Exception:
            return "(observation unavailable)"

    def _write_summary(self, r: ReplayResult) -> None:
        self.logger.write_summary(r.to_dict())
        self.logger.log("replay_end", status=r.status.value)
