"""The LLM-driven discovery loop: observe -> decide -> act, then emit an artifact.

The loop is the ONLY place the LLM is in the decision path. It:
  * asks the provider for the next action (given goal + running transcript),
  * enforces safety BEFORE executing,
  * executes on the live surface and verifies any checkpoint,
  * records the distilled Step (with robust targeting) into a growing artifact,
  * captures redacted evidence.

On `finish` it assembles a typed, versioned CapabilityArtifact. On `give_up`, a
stopping condition, or an unrecoverable error it returns a failure with evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..observability.logging import RunLogger
from ..safety.policy import PolicyDecision, PolicyViolation, SafetyPolicy
from ..schema import (
    ActionType,
    BusinessOutcomeDetector,
    CapabilityArtifact,
    CapabilityMeta,
    Checkpoint,
    InputParam,
    Locator,
    OutputField,
    ParamType,
    Provenance,
    RecoveryRule,
    RiskLevel,
    Selector,
    Step,
    TargetBinding,
    render_template,
    template_param_names,
)
from ..surface.base import ElementNotResolved, Surface
from .llm import LLMProvider


@dataclass
class DiscoveryResult:
    status: str                       # "success" | "gave_up" | "failed"
    message: str = ""
    artifact: Optional[CapabilityArtifact] = None
    artifact_path: Optional[str] = None
    evidence_dir: Optional[str] = None
    steps_taken: int = 0


def _parse_selector(d: dict[str, Any]) -> Selector:
    return Selector(
        strategy=d["strategy"],
        value=d["value"],
        role=d.get("role"),
        exact=bool(d.get("exact", False)),
    )


def _parse_locator(d: dict[str, Any]) -> Locator:
    return Locator(
        description=d.get("description", "control"),
        primary=_parse_selector(d["primary"]),
        fallbacks=[_parse_selector(s) for s in d.get("fallbacks", [])],
        reasoning=d.get("reasoning", ""),
    )


def _parse_checkpoint(d: Optional[dict[str, Any]]) -> Optional[Checkpoint]:
    if not d:
        return None
    return Checkpoint(kind=d["kind"], value=d["value"], description=d.get("description", ""))


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        surface: Surface,
        goal: str,
        target: TargetBinding,
        policy: SafetyPolicy,
        logger: RunLogger,
        max_steps: int = 25,
    ):
        self.provider = provider
        self.surface = surface
        self.goal = goal
        self.target = target
        self.policy = policy
        self.logger = logger
        self.max_steps = max_steps

        self._transcript: list[dict[str, str]] = []
        self._run_params: dict[str, Any] = {}
        self._inputs: list[InputParam] = []
        self._outputs: list[OutputField] = []
        self._steps: list[Step] = []
        self._capability_meta: Optional[CapabilityMeta] = None

    # ------------------------------------------------------------------ #
    def _record_assistant(self, action: dict[str, Any]) -> None:
        import json

        self._transcript.append({"role": "assistant", "content": json.dumps(action)})

    def _record_observation(self, obs_text: str) -> None:
        self._transcript.append(
            {"role": "user", "content": f"OBSERVATION:\n{obs_text}\n\nEmit the next action as JSON."}
        )

    def _parameterize(self, value: Optional[str]) -> Optional[str]:
        """Turn a literal that equals a run-param value into a {{param}} reference,
        so the recorded step is reusable. If the LLM already used {{param}}, keep it."""
        if value is None:
            return None
        if template_param_names(value):
            return value
        for name, concrete in self._run_params.items():
            if str(concrete) and value == str(concrete):
                return "{{" + name + "}}"
        return value

    # ------------------------------------------------------------------ #
    def run(self) -> DiscoveryResult:
        self.logger.log("discovery_start", goal=self.goal, target=self.target.model_dump(),
                        model=self.provider.model_name())
        # Entry point.
        try:
            self.policy.check_url(self.target.base_url)
            self.surface.goto(self.target.base_url)
        except PolicyViolation as e:
            return self._fail(f"Entry URL blocked by policy: {e}")

        for turn in range(self.max_steps):
            obs = self.surface.observe()
            self._record_observation(obs.render_for_llm())
            try:
                action = self.provider.next_action(self.goal, self._transcript)
            except Exception as e:
                return self._fail(f"LLM provider error: {e}")
            self._record_assistant(action)

            kind = action.get("action")
            self.logger.log("agent_action", turn=turn, action=kind,
                            thought=action.get("thought", ""),
                            step_id=action.get("step_id"))

            if kind == "plan":
                self._apply_plan(action)
                continue
            if kind == "finish":
                return self._finish(action)
            if kind == "give_up":
                return self._gave_up(action.get("reason", "no reason given"))

            # An acting step.
            try:
                self._execute_step(action)
            except PolicyViolation as e:
                self.logger.capture_failure(self.surface, "policy_block")
                return self._fail(f"Policy violation: {e}")
            except ElementNotResolved as e:
                self.logger.capture_failure(self.surface, "element_not_resolved")
                # Tell the model so it can try a different targeting next turn.
                self._transcript.append(
                    {"role": "user", "content": f"ACTION FAILED: {e}. Try a different target."}
                )
                continue
            except Exception as e:
                self.logger.capture_failure(self.surface, "action_error")
                self._transcript.append(
                    {"role": "user", "content": f"ACTION ERROR: {e}. Reconsider."}
                )
                continue

        self.logger.capture_failure(self.surface, "max_steps")
        return self._fail(f"Hit max_steps ({self.max_steps}) without finishing.")

    # ------------------------------------------------------------------ #
    def _apply_plan(self, action: dict[str, Any]) -> None:
        cap = action.get("capability", {})
        self._capability_meta = CapabilityMeta(
            id=cap.get("id", f"{self.target.app_id}.capability"),
            name=cap.get("name", "Discovered capability"),
            description=cap.get("description", self.goal),
        )
        self._inputs = []
        for p in action.get("inputs", []):
            is_secret = bool(p.get("secret", False))
            self._inputs.append(
                InputParam(
                    name=p["name"],
                    type=ParamType(p.get("type", "string")),
                    required=bool(p.get("required", True)),
                    description=p.get("description", ""),
                    # Never persist an example for a secret input.
                    example=None if is_secret else p.get("example"),
                    secret=is_secret,
                )
            )
        self._run_params = dict(action.get("run_params", {}))
        # Register secret concrete values for redaction.
        for ip in self._inputs:
            if ip.secret and ip.name in self._run_params:
                self.logger.redactor.add_secret(str(self._run_params[ip.name]))
        self.logger.log("plan_applied",
                        capability_id=self._capability_meta.id,
                        inputs=[ip.model_dump() for ip in self._inputs])

    def _execute_step(self, action: dict[str, Any]) -> None:
        act = ActionType(action["action"])
        step_id = action.get("step_id", f"s{len(self._steps)+1}")
        risk = RiskLevel(action.get("risk", "safe"))
        target = _parse_locator(action["target"]) if action.get("target") else None
        checkpoint = _parse_checkpoint(action.get("checkpoint"))

        recorded_value = self._parameterize(action.get("value"))
        concrete_value = render_template(recorded_value, self._run_params) if recorded_value else None

        # Safety: enforce action type, url allowlist, risk handling.
        url_for_check = action.get("url") if act == ActionType.GOTO else self.surface.current_url()
        decision = self.policy.enforce(act, risk, action.get("description", step_id), url_for_check)
        if decision == PolicyDecision.NEEDS_CONFIRMATION:
            # During discovery we record that a human confirmation would be needed,
            # but proceed (this is a controlled demo surface). Replay will honor it.
            self.logger.log("risky_action_confirmation_noted", step_id=step_id,
                            description=action.get("description", ""))

        idx = self.logger.next_step_index()

        # Execute.
        if act == ActionType.GOTO:
            url = render_template(action["url"], self._run_params)
            self.policy.check_url(url)
            self.surface.goto(url)
        elif act == ActionType.CLICK:
            self.surface.click(target)
        elif act == ActionType.FILL:
            self.surface.fill(target, concrete_value or "")
        elif act == ActionType.SELECT:
            self.surface.select(target, concrete_value or "")
        elif act == ActionType.PRESS:
            self.surface.press(concrete_value or "Enter", target)
        elif act == ActionType.EXTRACT:
            val = self.surface.extract(target, action.get("extract_attr"))
            name = action["extract_as"]
            self._outputs.append(
                OutputField(
                    name=name,
                    type=ParamType(action.get("extract_type", "string")),
                    description=action.get("extract_description", ""),
                )
            )
            self.logger.log("extracted", name=name, value=val)
        else:
            raise ValueError(f"Unsupported action in discovery: {act}")

        # Verify the checkpoint if present. IMPORTANT: the action above already
        # executed, so its side effects (a navigation, a submit) are real whether or
        # not the agent's post-condition guess was correct. If the checkpoint does
        # NOT hold, we must still record the step -- dropping it would leave a hole in
        # the flow (e.g. a missing "Sign In" click) that breaks replay. We keep the
        # step and simply discard the unverified checkpoint rather than baking a wrong
        # assertion into the artifact, and we tell the model so it can adapt.
        recorded_checkpoint = checkpoint
        if checkpoint is not None:
            cp = Checkpoint(
                kind=checkpoint.kind,
                value=render_template(checkpoint.value, self._run_params) or checkpoint.value,
                description=checkpoint.description,
            )
            ok = self.surface.wait_for(cp, action.get("timeout_ms", 8000))
            self.logger.log("checkpoint", step_id=step_id, kind=cp.kind, value=cp.value, held=ok)
            if not ok:
                recorded_checkpoint = None
                self.logger.log("checkpoint_unverified", step_id=step_id,
                                kind=cp.kind, value=cp.value)
                self._transcript.append({"role": "user", "content":
                    f"NOTE: step {step_id} executed successfully, but your checkpoint "
                    f"({cp.kind.value}={cp.value}) did not hold against the actual page. "
                    f"It has been dropped from the recording. Observe the current state and "
                    f"continue; if a later step needs a post-condition, base it on what you "
                    f"actually see."})

        # Record the distilled step (with parameterized value, not the concrete one).
        self._steps.append(
            Step(
                id=step_id,
                action=act,
                description=action.get("description", ""),
                target=target,
                url=action.get("url"),
                value=recorded_value,
                extract_as=action.get("extract_as"),
                extract_attr=action.get("extract_attr"),
                checkpoint=recorded_checkpoint,
                risk=risk,
                timeout_ms=action.get("timeout_ms", 10000),
            )
        )
        self.logger.capture_step(self.surface, step_id)
        self.logger.log("step_recorded", step_id=step_id, action=str(act),
                        checkpoint_verified=recorded_checkpoint is not None)

    # ------------------------------------------------------------------ #
    def _finish(self, action: dict[str, Any]) -> DiscoveryResult:
        if self._capability_meta is None:
            return self._fail("Agent finished without a plan (no capability metadata).")
        success = _parse_checkpoint(action.get("success"))
        if success is None:
            return self._fail("Agent finished without declaring a success condition.")

        business_outcomes = [
            BusinessOutcomeDetector(
                code=b["code"],
                when=_parse_checkpoint(b["when"]),
                message=b.get("message", ""),
                terminal=bool(b.get("terminal", True)),
            )
            for b in action.get("business_outcomes", [])
        ]
        recoveries = []
        for r in action.get("recoveries", []):
            do_steps = []
            for sd in r.get("do", []):
                do_steps.append(
                    Step(
                        id=sd.get("step_id", "r_step"),
                        action=ActionType(sd["action"]),
                        description=sd.get("description", ""),
                        target=_parse_locator(sd["target"]) if sd.get("target") else None,
                        value=sd.get("value"),
                    )
                )
            recoveries.append(
                RecoveryRule(
                    name=r["name"],
                    when=_parse_checkpoint(r["when"]),
                    do=do_steps,
                    max_attempts=int(r.get("max_attempts", 1)),
                )
            )

        # Final success verification against the live surface.
        held = self.surface.check(
            Checkpoint(kind=success.kind,
                       value=render_template(success.value, self._run_params) or success.value)
        )
        self.logger.log("final_success_check", held=held, kind=success.kind, value=success.value)

        from ..schema import ArtifactPolicy

        try:
            artifact = CapabilityArtifact(
                capability=self._capability_meta,
                target=self.target,
                inputs=self._inputs,
                outputs=self._outputs,
                steps=self._steps,
                success=success,
                business_outcomes=business_outcomes,
                recoveries=recoveries,
                policy=ArtifactPolicy(allowed_url_prefixes=self.policy.allowed_url_prefixes),
                provenance=Provenance(
                    created_by="discovery-agent",
                    model=self.provider.model_name(),
                    discovery_run_id=self.logger.run_id,
                    notes=f"Goal: {self.goal}",
                ),
            )
        except ValueError as e:
            # The assembled artifact failed integrity validation (e.g. an undeclared
            # {{param}} the model referenced). Fail the run clearly rather than crash.
            return self._fail(f"Assembled artifact failed validation: {e}")
        self.logger.capture_failure(self.surface, "final_state")  # capture final screen/dom too
        return DiscoveryResult(
            status="success" if held else "failed",
            message="Discovery completed and success condition held."
            if held else "Flow completed but final success condition did not hold.",
            artifact=artifact,
            evidence_dir=self.logger.evidence_dir,
            steps_taken=len(self._steps),
        )

    def _fail(self, msg: str) -> DiscoveryResult:
        self.logger.log("discovery_failed", message=msg)
        return DiscoveryResult(status="failed", message=msg,
                               evidence_dir=self.logger.evidence_dir, steps_taken=len(self._steps))

    def _gave_up(self, reason: str) -> DiscoveryResult:
        self.logger.log("discovery_gave_up", reason=reason)
        self.logger.capture_failure(self.surface, "gave_up")
        return DiscoveryResult(status="gave_up", message=reason,
                               evidence_dir=self.logger.evidence_dir, steps_taken=len(self._steps))
