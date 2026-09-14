"""The capability artifact schema.

This is the contract between three audiences:

  1. The discovery agent, which *emits* an artifact after a successful LLM run.
  2. The replay engine, which *executes* it deterministically with no LLM.
  3. A human reviewer (and a calling AI agent), which *reads* it to understand
     what the capability does, what it needs, and what it returns.

Design principles baked into the schema
---------------------------------------
* Decoupled from the transcript. The artifact records the *distilled flow*, not
  the model's chain of thought. Provenance links back to the run for audit, but
  replay never needs the transcript.

* Targeting is a strategy, not a string. Every element is located by an ordered
  list of strategies (primary + fallbacks), each carrying the *reasoning* for why
  it is robust. Accessibility/role/label/text strategies come first because they
  survive markup churn and work on surfaces with no clean DOM; brittle CSS/XPath
  are last-resort fallbacks. See REPORT.md 'Determinism & error handling'.

* Errors are first-class. The schema distinguishes, up front:
    - `business_outcomes`: legitimate results the caller must know about
      ("no such member"). NOT failures.
    - `recoveries`: known recoverable conditions (dismiss an interstitial,
      wait out slowness) with the exact remediation steps.
    - everything else surfaces as a hard, debuggable failure at replay time.

* Typed I/O. Inputs and outputs are typed and named so the artifact reads like a
  function signature an agent can call. Inputs can be marked `secret` for redaction.

* Versioned + reviewable + approvable. Semantic capability version, a schema
  version, and a draft/approved status gate for unattended replay.

* Multi-tenant aware. The target binding separates the *vendor app identity* from
  the *tenant instance*, and locators/values can be parameterized so one artifact
  generalizes across tenants running the same product (see REPORT.md 3.7).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ActionType(str, Enum):
    """The (small, deliberate) vocabulary of actions replay can perform.

    Kept intentionally minimal and surface-agnostic: every one of these maps
    cleanly onto a web DOM, an accessibility tree, or OS-level automation, which
    is what lets the same artifact shape extend to legacy/desktop surfaces.
    """
    GOTO = "goto"                # navigate to a URL (entry point only)
    CLICK = "click"              # activate a control
    FILL = "fill"               # type text into a field
    SELECT = "select"           # choose an option in a dropdown
    PRESS = "press"             # press a key (e.g. Enter)
    WAIT_FOR = "wait_for"       # wait until a checkpoint holds (no side effect)
    EXTRACT = "extract"         # read a value out of the page into an output
    ASSERT = "assert"           # assert a checkpoint; fail hard if it does not hold
    DISMISS_IF_PRESENT = "dismiss_if_present"  # optional click if a target exists


class LocatorStrategy(str, Enum):
    """Ordered, roughly, from most robust to least. Accessibility-first."""
    ROLE = "role"               # ARIA role + accessible name (most robust)
    LABEL = "label"             # form control by its visible label text
    PLACEHOLDER = "placeholder"
    TEXT = "text"               # visible text content
    ALT_TEXT = "alt_text"
    TITLE = "title"
    NAME_ATTR = "name_attr"     # HTML name="" attribute (common in legacy forms)
    CSS = "css"                 # brittle: last-resort fallback
    XPATH = "xpath"             # brittle: last-resort fallback


class RiskLevel(str, Enum):
    SAFE = "safe"               # reversible / read-only (navigate, read, search)
    RISKY = "risky"             # irreversible or state-changing (create, submit, delete)


class CheckpointKind(str, Enum):
    URL_CONTAINS = "url_contains"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    ELEMENT_VISIBLE = "element_visible"


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class CapabilityStatus(str, Enum):
    DRAFT = "draft"             # discovered, not yet approved for unattended replay
    APPROVED = "approved"       # reviewed; safe to run unattended


# --------------------------------------------------------------------------- #
# Targeting
# --------------------------------------------------------------------------- #
class Selector(BaseModel):
    """One way to locate a control."""
    strategy: LocatorStrategy
    value: str = Field(
        ...,
        description="For ROLE: the accessible name. Otherwise the label/text/selector.",
    )
    role: Optional[str] = Field(
        None, description="ARIA role, required only when strategy == role."
    )
    exact: bool = Field(False, description="Exact vs. substring match for text-ish strategies.")

    @model_validator(mode="after")
    def _check_role(self):
        if self.strategy == LocatorStrategy.ROLE and not self.role:
            raise ValueError("Selector with strategy 'role' must set 'role'.")
        return self


class Locator(BaseModel):
    """An ordered set of strategies to find a single control.

    Replay tries `primary` first, then each fallback in order. `reasoning` is for
    the human reviewer: *why* this targeting is expected to be robust.
    """
    description: str = Field(..., description="Human name for the control, e.g. 'Member ID field'.")
    primary: Selector
    fallbacks: list[Selector] = Field(default_factory=list)
    reasoning: str = Field(
        "",
        description="Why this targeting is robust (e.g. 'label text is user-visible "
        "and stable; CSS nth-child fallback only if markup changes').",
    )

    def all_selectors(self) -> list[Selector]:
        return [self.primary, *self.fallbacks]


# --------------------------------------------------------------------------- #
# Checkpoints / outcomes / recovery
# --------------------------------------------------------------------------- #
class Checkpoint(BaseModel):
    """A condition asserted to confirm we actually reached the expected state."""
    kind: CheckpointKind
    value: str = Field(..., description="URL fragment or text; templated with {{param}} allowed.")
    description: str = ""


class BusinessOutcomeDetector(BaseModel):
    """Recognizes a legitimate, non-failure result the caller needs to know about.

    Example: the 'no such member' page. Detected deterministically at replay time;
    reported as a structured outcome, never as a crash.
    """
    code: str = Field(..., description="Stable machine code, e.g. 'member_not_found'.")
    when: Checkpoint
    message: str = Field("", description="Human-readable explanation for the caller.")
    terminal: bool = Field(
        True, description="If True, replay stops and returns this outcome (not a failure)."
    )


class RecoveryRule(BaseModel):
    """A known, recoverable condition plus the exact remediation.

    Example: an unexpected 'System Notice' interstitial -> click Acknowledge and
    continue. Deterministic and bounded (max_attempts) so replay never loops.
    """
    name: str
    when: Checkpoint
    do: list["Step"] = Field(default_factory=list, description="Remediation steps.")
    max_attempts: int = 1


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
class Step(BaseModel):
    """One ordered action in the flow."""
    id: str = Field(..., description="Stable step id, e.g. 's1_search'.")
    action: ActionType
    description: str = ""

    # Targeting (required for control-bound actions; None for GOTO/WAIT_FOR).
    target: Optional[Locator] = None

    # Payloads (templated with {{param}} where relevant).
    url: Optional[str] = None            # GOTO
    value: Optional[str] = None          # FILL / SELECT / PRESS

    # Extraction (EXTRACT).
    extract_as: Optional[str] = Field(None, description="Output name to store the read value under.")
    extract_attr: Optional[str] = Field(
        None, description="Attribute to read; None means visible text."
    )

    # Post-condition asserted after the action runs.
    checkpoint: Optional[Checkpoint] = None

    # Safety + timing.
    risk: RiskLevel = RiskLevel.SAFE
    timeout_ms: int = 10_000
    optional: bool = Field(False, description="If True, a missing target is not a failure.")

    @model_validator(mode="after")
    def _shape(self):
        if self.action == ActionType.GOTO and not self.url:
            raise ValueError(f"Step {self.id}: GOTO requires 'url'.")
        needs_target = {
            ActionType.CLICK,
            ActionType.FILL,
            ActionType.SELECT,
            ActionType.EXTRACT,
            ActionType.DISMISS_IF_PRESENT,
        }
        if self.action in needs_target and self.target is None:
            raise ValueError(f"Step {self.id}: action {self.action} requires a target.")
        if self.action in {ActionType.FILL, ActionType.SELECT} and self.value is None:
            raise ValueError(f"Step {self.id}: action {self.action} requires a value.")
        if self.action == ActionType.EXTRACT and not self.extract_as:
            raise ValueError(f"Step {self.id}: EXTRACT requires 'extract_as'.")
        return self


RecoveryRule.model_rebuild()  # resolve forward ref to Step


# --------------------------------------------------------------------------- #
# Typed I/O
# --------------------------------------------------------------------------- #
class InputParam(BaseModel):
    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    example: Optional[str] = None
    secret: bool = Field(
        False, description="If True, value is redacted everywhere (logs, evidence, artifact echoes)."
    )


class OutputField(BaseModel):
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""


# --------------------------------------------------------------------------- #
# Target binding, policy, provenance
# --------------------------------------------------------------------------- #
class TargetBinding(BaseModel):
    """Separates the *vendor app identity* from the *tenant instance*.

    This is the seam that makes cross-tenant reuse possible: the flow is recorded
    against `app_id` (the vendor product), while `base_url`/`tenant_id` pin the
    concrete instance. See REPORT.md 3.7.
    """
    app_id: str = Field(..., description="Vendor product identity, e.g. 'coreserv'.")
    app_version: Optional[str] = None
    base_url: str = Field(..., description="Entry point for this tenant instance.")
    tenant_id: Optional[str] = Field(None, description="Institution this instance belongs to.")


class ArtifactPolicy(BaseModel):
    """Per-capability guardrails, enforced by the safety layer at replay time."""
    allowed_action_types: list[ActionType] = Field(
        default_factory=lambda: [
            ActionType.GOTO, ActionType.CLICK, ActionType.FILL, ActionType.SELECT,
            ActionType.PRESS, ActionType.WAIT_FOR, ActionType.EXTRACT,
            ActionType.ASSERT, ActionType.DISMISS_IF_PRESENT,
        ]
    )
    allowed_url_prefixes: list[str] = Field(
        default_factory=list,
        description="Replay may only navigate/act within these URL prefixes (allowlist).",
    )
    require_approval_for_risky: bool = Field(
        True, description="Risky steps need explicit confirmation unless capability is approved."
    )


class Provenance(BaseModel):
    """Audit trail back to discovery. Deliberately NOT the raw transcript."""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: str = "discovery-agent"
    model: Optional[str] = None
    discovery_run_id: Optional[str] = None
    notes: str = ""


class CapabilityMeta(BaseModel):
    id: str = Field(..., description="Stable capability id, e.g. 'coreserv.read_savings_balance'.")
    name: str
    description: str
    version: str = Field("1.0.0", description="Semantic version of this capability.")
    status: CapabilityStatus = CapabilityStatus.DRAFT


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #
class CapabilityArtifact(BaseModel):
    """A typed, versioned, replayable capability."""
    schema_version: str = SCHEMA_VERSION
    capability: CapabilityMeta
    target: TargetBinding

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    steps: list[Step]
    success: Checkpoint = Field(..., description="Overall success condition for the whole flow.")

    business_outcomes: list[BusinessOutcomeDetector] = Field(default_factory=list)
    recoveries: list[RecoveryRule] = Field(default_factory=list)

    policy: ArtifactPolicy = Field(default_factory=ArtifactPolicy)
    provenance: Provenance = Field(default_factory=Provenance)

    # ------------------------------------------------------------------ #
    # Load-time integrity validation
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _validate_references(self):
        """Fail fast, at load time, on a malformed artifact:

          * every {{param}} used anywhere (step values/urls, all checkpoints,
            business-outcome and recovery detectors) must be a declared input,
          * every EXTRACT step's `extract_as` must be a declared output,
          * a secret input must not carry a stored `example` value.

        Without this, a bad reference would only surface at replay as a KeyError ->
        unexpected_error. Here it is a clear, immediate schema error instead.
        """
        input_names = {p.name for p in self.inputs}
        output_names = {o.name for o in self.outputs}

        def check_text(text, where):
            for ref in template_param_names(text):
                if ref not in input_names:
                    raise ValueError(
                        f"{where}: references undeclared input '{{{{{ref}}}}}' "
                        f"(declared inputs: {sorted(input_names)})"
                    )

        def check_cp(cp, where):
            if cp is not None:
                check_text(cp.value, f"{where} checkpoint")

        for s in self.steps:
            check_text(s.value, f"step '{s.id}'")
            check_text(s.url, f"step '{s.id}' url")
            check_cp(s.checkpoint, f"step '{s.id}'")
            if s.action == ActionType.EXTRACT and s.extract_as not in output_names:
                raise ValueError(
                    f"step '{s.id}': extract_as '{s.extract_as}' is not a declared output "
                    f"(declared outputs: {sorted(output_names)})"
                )

        check_cp(self.success, "success")
        for b in self.business_outcomes:
            check_cp(b.when, f"business_outcome '{b.code}'")
        for rule in self.recoveries:
            check_cp(rule.when, f"recovery '{rule.name}'")
            for ds in rule.do:
                check_text(ds.value, f"recovery '{rule.name}' step '{ds.id}'")
                check_text(ds.url, f"recovery '{rule.name}' step '{ds.id}' url")

        for p in self.inputs:
            if p.secret and p.example is not None:
                raise ValueError(
                    f"secret input '{p.name}' must not store an example value "
                    f"(would leak a secret into the artifact)"
                )
        return self

    # ------------------------------------------------------------------ #
    # Convenience / validation
    # ------------------------------------------------------------------ #
    def input_names(self) -> set[str]:
        return {p.name for p in self.inputs}

    def secret_input_names(self) -> set[str]:
        return {p.name for p in self.inputs if p.secret}

    def validate_params(self, params: dict[str, Any]) -> None:
        """Raise ValueError if required inputs are missing or unknown keys supplied."""
        provided = set(params.keys())
        declared = self.input_names()
        missing = {p.name for p in self.inputs if p.required} - provided
        if missing:
            raise ValueError(f"Missing required input(s): {sorted(missing)}")
        unknown = provided - declared
        if unknown:
            raise ValueError(f"Unknown input(s) not declared by artifact: {sorted(unknown)}")

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "CapabilityArtifact":
        return cls.model_validate_json(text)


# --------------------------------------------------------------------------- #
# Templating helper (shared by replay)
# --------------------------------------------------------------------------- #
def render_template(text: Optional[str], params: dict[str, Any]) -> Optional[str]:
    """Substitute {{param}} occurrences. Raises on an unknown reference."""
    if text is None:
        return None

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in params:
            raise KeyError(f"Template references unknown parameter '{{{{{key}}}}}'.")
        return str(params[key])

    return _TEMPLATE_RE.sub(repl, text)


def template_param_names(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    return set(_TEMPLATE_RE.findall(text))
