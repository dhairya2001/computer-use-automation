# Design write-up — Computer-Use Automation System

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

This is a thin-but-complete vertical slice that touches every core requirement. The three load-bearing pieces — the artifact schema, deterministic replay + error taxonomy, and the safety/escalation model — are built out; everything the brief permits to be stubbed (the operator UI, desktop/legacy surfaces, multi-tenant plumbing) is mocked at a clean, documented seam.

## 1. Architecture

The system is one Python package (`cua`) with sharp module boundaries, driven by a small CLI. Two paths run through the same shared vocabulary:

- **Discovery path** (`agent/`): an LLM-driven `observe → decide → act` loop drives a live surface toward a goal and, on success, emits a typed artifact. The LLM is the *only* place a model is in the decision loop.
- **Production path** (`replay/`): a deterministic engine executes a saved artifact with no LLM, returning a structured result.

Both paths depend on the same three abstractions, which is what keeps them coherent:

- **`schema.py`** — the `CapabilityArtifact` and everything in it. The contract both paths speak.
- **`surface/`** — the perceive/act boundary (`Surface`). `observe()` returns a model-readable snapshot; actions take a schema `Locator`. The recorded flow never mentions Playwright, a DOM, or coordinates. One implementation today: `PlaywrightWebSurface`.
- **`safety/`** — allowlist + risk policy + redaction, enforced in *both* paths.

Supporting modules: `escalation/` (human handoff + control-transfer), `observability/` (redacted structured logs + evidence), `config.py`/`cli.py` (wiring).

**Key decisions & trade-offs.** (a) *One process, synchronous.* The brief explicitly discourages building scaling infrastructure; a queue/service split buys nothing for a vertical slice and would obscure the design. The abstractions (a serializable artifact, a stateless replay engine, a routed intervention request) are shaped so a queue/worker deployment is a packaging change, not a redesign. (b) *Accessibility/label/text-first targeting over CSS/DOM.* This is the single most consequential choice for the real environment (legacy apps with no clean DOM), so it is baked into the locator model rather than left to convention. (c) *Discovery emits the artifact incrementally* (one distilled `Step` per successful action) rather than post-hoc parsing the transcript, so the artifact is decoupled from the model's reasoning by construction. (d) *Provider seam* (`LLMProvider`) with an Anthropic implementation and a scripted `MockProvider`, so the whole pipeline is testable and demoable offline while the real discovery run uses Claude.

## 2. Artifact schema

The artifact (`schema.py`) is designed to read like a **callable function signature with an execution plan attached**, understandable by both a human reviewer and a calling agent. Top-level shape:

- **`capability`** — id, name, description, semantic `version`, and a `status` (`draft`/`approved`) that gates unattended risky replay.
- **`target`** — `TargetBinding` deliberately separates the *vendor product identity* (`app_id`, `app_version`) from the *tenant instance* (`base_url`, `tenant_id`). This separation is the hook for cross-tenant reuse (§4).
- **`inputs` / `outputs`** — typed, named parameters. Inputs carry `required`, `example`, and a `secret` flag that drives redaction. Outputs declare name + type + shape.
- **`steps`** — the ordered flow. Each `Step` has a small, surface-agnostic action (`goto/click/fill/select/press/wait_for/extract/assert/dismiss_if_present`), a `Locator`, an optional post-condition `checkpoint`, a `risk` level, and a timeout.
- **`success`** — the overall checkpoint asserted at the end.
- **`business_outcomes`** — detectors for legitimate non-success results (§3).
- **`recoveries`** — known recoverable conditions plus their exact remediation steps.
- **`policy`** — per-capability allowlist + risk rules. **`provenance`** — audit link back to the discovery run and model; deliberately **not** the transcript.

**Why targeting is a strategy, not a string.** A `Locator` is an ordered `primary` + `fallbacks`, each a `Selector(strategy, value, …)` carrying human-readable `reasoning`. Strategies are ordered most→least robust: `role` (ARIA role + accessible name) → `label` → `placeholder` → `text` → `name_attr` → `css`/`xpath`. Replay tries them in order and uses the first that resolves uniquely. This makes robustness explicit and reviewable, and it degrades gracefully: brittle positional selectors exist only as last-resort fallbacks. For reading values out of a legacy id-less table, an xpath anchored on the *visible label text* (`//td[.='Savings Balance']/following-sibling::td[1]`) is used as a primary — it is anchored to meaning, not position.

**Parameterization.** Values that came from the goal are referenced as `{{param}}` in steps, never as literals, so one artifact serves every invocation. The discovery loop also auto-parameterizes: a fill value equal to a known input value is rewritten to its `{{param}}` reference before recording.

## 3. Determinism & error handling

**Determinism.** Replay is a straight pass over `steps` with no model. Stability comes from (a) the ordered locator strategies above, (b) explicit `checkpoint`s after actions — we assert we reached the expected state rather than assuming a click worked — and (c) checkpoint-based waiting (`wait_for` polls a condition rather than sleeping), which absorbs transient slowness.

**The error taxonomy is the core of this system.** Every replay ends in exactly one of three states, and conflating them is the mistake the brief calls out:

- **`SUCCESS`** — success checkpoint held; declared outputs returned.
- **`BUSINESS_OUTCOME`** — a legitimate result the caller must branch on (`member_not_found`, `permission_denied`, `invalid_deposit`). Carries a stable `code` + message. **Not** a crash. Detected by artifact-declared `BusinessOutcomeDetector`s, which are scanned before each step and whenever a checkpoint fails.
- **`FAILED`** — a hard failure with a structured `ReplayFailure`: `kind` (element_not_found, checkpoint_failed, success_condition_failed, policy_violation, session_expired, timeout, recovery_exhausted, needs_human, unexpected_error), the `step_id`, and `expected` vs `observed` for debugging.

A fourth category, **recoverable conditions**, never surfaces as a result: declared `RecoveryRule`s (e.g. dismiss a "System Notice" interstitial) are applied inline and bounded by `max_attempts`; if remediation is exhausted it becomes `FAILED(recovery_exhausted)`. Session/auth timeout has a built-in detector independent of the artifact.

Crucially, business outcomes and recoveries are part of the *authored contract* — the discovery agent proposes them from domain reasoning and a human confirms them at approval — so replay handles exceptional states the happy-path discovery never encountered. The evidence set demonstrates all branches: success, `member_not_found`, `permission_denied`, `invalid_deposit`, interstitial-recovery→success, session-timeout failure, and element-not-found failure. (UI drift is the secondary concern here per §1; the fallback-locator ladder is the answer to it, and a failed primary that falls through to a fallback is logged as a drift signal.)

## 4. Heterogeneity & multi-tenant

**Surface abstraction (extending to legacy/desktop).** The seam is the `Surface` interface. The artifact is expressed in actions + `Locator`s, never in Playwright specifics, so a new surface is a new `Surface` implementation with the schema and replay engine unchanged:
- A **legacy web** app is largely the same `PlaywrightWebSurface`, leaning harder on `name_attr`/label-anchored-xpath strategies (already the primaries on our intentionally id-less mock).
- A **desktop** app is a `DesktopAXSurface` over the OS accessibility tree: `observe()` returns the AX tree, and `role`/`name` locators map directly onto AX roles and names — which is exactly why role/name strategies are ranked first. A **screenshot+coordinates** surface is the vision fallback for surfaces with no accessibility layer at all.
The only strategies that don't port are `css`/`xpath`; because they're last-resort fallbacks, a flow authored well degrades rather than breaks.

**Multi-tenant reuse.** `TargetBinding` splits `app_id` (vendor product) from `base_url`/`tenant_id` (instance). An artifact is authored against the *product*; running it for another tenant on the same product is, in the clean case, the same artifact with a different `base_url`. The design supports a **base artifact + per-tenant override** model: a tenant record supplies its `base_url` and, where a tenant's build differs, a small overlay of locator/checkpoint overrides keyed by `step.id` — so you specialize the few steps that drift instead of re-recording. **Drift detection**: replay already emits per-selector "primary failed, used fallback" signals and checkpoint mismatches; aggregated per (app_id, tenant, version) these flag which tenants have diverged and need an override or a re-discovery. Building the override store and canonicalization (e.g. `/item/12345` → `/item/:id`) is future work (§7); the abstractions above don't preclude it.

## 5. Escalation & handoff

**Detect & route.** The engine escalates when it is stuck in a way a human can resolve: a risky/irreversible step needs confirmation (unattended, unapproved capability), or a control can't be resolved even after recovery. It builds an `InterventionRequest` carrying capability id, goal, current step, reason, current URL, and the evidence dir, and routes it through an `InterventionQueue` (the stand-in for a durable queue/operator-console API).

**Take control of the *same* live session.** The load-bearing idea is the control-transfer model, not the operator UI (mocked, as the brief allows). `SessionControl` tracks the single holder of control (`AUTOMATION`/`HUMAN`) and logs every transfer, so "who is in control?" is always answerable. On escalation the engine **pauses synchronously**, cedes control, and hands the operator the *same* `Surface` object wrapping the *same* browser page — nothing is re-created. `HumanConsoleHandler` is a real headed handoff (a person acts in the visible browser, then types `done`/`abort`); `MockOperatorHandler` is the scripted stand-in used in the automated demo and tests, exercising the identical mechanism.

**Hand back & resume.** When the operator finishes, control returns to `AUTOMATION`, the human's actions are recorded to evidence, and the engine **re-checks its step against the resulting state** and resumes the flow. If the operator aborts, replay ends as `FAILED(needs_human)` with the intervention id. The demo shows a risky sub-account creation escalated, approved by the operator, and the flow resuming to the confirmation screen — with the two control transfers in the log.

## 6. Safety

Three guardrails, enforced in both discovery and replay (`safety/`):

1. **Allowlist.** A configurable URL-prefix allowlist (the agent may not navigate or act outside it) plus an action-type allowlist. Enforced before the entry navigation and before every step; a violation is a hard `FAILED(policy_violation)`, never a silent proceed.
2. **Risk classification.** Steps are `safe` (reversible/read-only) or `risky` (irreversible/state-changing, e.g. a submit that creates an account). Risky steps are handled by a configurable mode: `block`, `confirm` (route to a human — the conservative default), or `allow` (only for an `approved` capability or an explicit attended demo). This is why "open sub-account" escalates by default.
3. **Redaction.** Nothing sensitive is persisted. Secret inputs are stored only as `{{param}}` references (never concrete values, and their `example` is dropped); logs, evidence, and summaries pass through a `Redactor` that masks both concrete secret values and pattern-matched sensitive shapes (SSN, card-like runs, tokens, emails). Verified: the evidence for the login-bearing runs contains zero occurrences of the passcode.

**Limits.** The allowlist is prefix-based (origin-level), not a per-field data-egress policy; redaction patterns are best-effort and would need tightening to a bank's specific PII catalog; and "risky" is currently a per-step author annotation rather than an inferred property. These are deliberate scope cuts, not oversights.

## 7. Cuts

Deliberately left out, each at a clean seam:

- **Operator console UI** — mocked (`MockOperatorHandler`) / console (`HumanConsoleHandler`). The control-transfer mechanism is real; the co-browsing UI is not built.
- **Desktop & legacy-specific surfaces** — designed (§4) but not implemented; one web surface is built. The intentionally hostile mock (table layouts, no ids/test-ids, label-only fields) exercises the legacy targeting story on a web surface.
- **Multi-tenant override store, canonicalization, cross-tenant demo** — designed; not built.
- **Real Anthropic evidence in this bundle** — the pipeline runs a genuine Claude-driven discovery via `cua discover` (a dependency-free HTTPS path needs only an API key); the committed offline evidence uses a scripted `MockProvider` and is labeled as such. See the README for the one-command real run.
- **Durable queue / services / retries-with-backoff / auth-session layer** — single-process and synchronous by choice; auth is folded into the demo flow (which conveniently exercises secret redaction) rather than factored into a session/precondition layer.

**What I'd build next**, in order: (1) the real Anthropic discovery evidence into `/evidence`; (2) assisted single-step LLM fallback on replay failure (bounded, policy-checked, recorded) — the highest-value robustness win; (3) the per-tenant override store + route/value canonicalization with a two-variant reuse demo; (4) a multi-run stability/flakiness score gating `draft → approved`; (5) an agent-facing capability catalog served as a typed tool/function-calling surface (the `catalog` CLI command is the seed).
