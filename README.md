# Computer-Use Automation System

An LLM discovers how to accomplish a goal in a legacy back-office UI **once**, that
run is captured as a typed, versioned **capability artifact**, and a **deterministic
replay engine** re-runs the artifact in production **without the LLM** — with stable
targeting, an explicit error taxonomy, safety guardrails, and a real human-in-the-loop
handoff.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

See **[REPORT.md](REPORT.md)** for the design write-up (architecture, schema, determinism/error handling, heterogeneity & multi-tenant, escalation, safety, cuts).

---

## What's here

```
mockapp/            A deliberately legacy bank back-office web app (the target surface):
                    table layouts, no test-ids, label-only fields, and injectable
                    runtime errors (not-found, permission denied, interstitial dialog,
                    session timeout, validation error).
src/cua/
  schema.py         The capability artifact schema (the contract). ★ focal point
  surface/          Perceive/act abstraction (Surface) + Playwright web implementation.
  agent/            LLM-driven discovery loop + provider seam (Anthropic + Mock).
  replay/           Deterministic replay engine + error taxonomy/result contract. ★
  safety/           Allowlist + risk policy + secret/PII redaction.
  escalation/       Human handoff + control-transfer model (same live session). ★
  verify/           Post-run verification: independent LLM judge + human sign-off.
  learning/         Turns a human's stuck-step fix into a draft override proposal.
  secrets.py        Resolves secret inputs from env / vault:// / stdin (never the CLI).
  observability/    Redacted structured logging + evidence capture.
  cli.py            `cua discover | replay | catalog | show`
artifacts/          Saved capability artifacts (JSON).
evidence/           Per-run logs, screenshots, DOM snapshots, summaries.
tests/              pytest suite (schema, safety, redaction, replay taxonomy).
scripts/demo.py     End-to-end offline demo across the whole error taxonomy.
```

★ = the load-bearing pieces the evaluation focuses on.

---

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                           # makes the `cua` command available
python -m playwright install chromium      # one-time browser download
```

Config is via env (see `.env.example`); copy it to `.env`:

```bash
cp .env.example .env
# edit .env:
#   ANTHROPIC_API_KEY=sk-ant-...      (needed ONLY for discover / --verify llm / --learn llm)
#   CUA_SECRET_passcode=demo          (the operator passcode; keeps it OFF the command line)
# then, in each terminal:  set -a; source .env; set +a
```

Secret inputs (the passcode) are resolved from `CUA_SECRET_<name>`, a `vault://`
reference, or a stdin prompt — never passed as `--param` (which would leak into
shell history). `replay` needs no API key or network beyond the target app.

---

## Demo path (copy/paste)

### Option A — full offline demo (no API key)

Runs a scripted (mock-provider) discovery for two capabilities, then exercises
**every branch of the error taxonomy** via deterministic replay:

```bash
python scripts/demo.py
```

Expected output:

```
== DISCOVERY (mock provider; scripted stand-in for the LLM) ==
  read_savings_balance : success  (6 steps) -> artifacts/coreserv.read_savings_balance.json
  open_subaccount      : success  (9 steps) -> artifacts/coreserv.open_subaccount.json

== REPLAY (deterministic; no LLM) ==
  [1] success (read balance)          -> SUCCESS           outputs={'savings_balance': '$4,210.55'}
  [2] business: member_not_found      -> BUSINESS_OUTCOME  code=member_not_found
  [3] business: permission_denied     -> BUSINESS_OUTCOME  code=permission_denied
  [4] recovery: dismiss interstitial  -> SUCCESS           recoveries=['dismiss_system_notice']
  [5] failure: session timeout        -> FAILED            failure.kind=session_expired
  [6] failure: element not found      -> FAILED            failure.kind=element_not_found
  [7] escalation: risky step approved -> SUCCESS
  [8] business: invalid_deposit       -> BUSINESS_OUTCOME  code=invalid_deposit
```

### Option B — the real, LLM-driven discovery run (requires `ANTHROPIC_API_KEY`)

This is the genuine `observe → decide → act` run against the live surface. Start the
mock app, then run discovery and replay the artifact it produced:

```bash
# terminal 1 — the target application
python -m mockapp.server            # serves http://127.0.0.1:5000

# terminal 2 — load env (key + secret), then discover and replay
set -a; source .env; set +a            # ANTHROPIC_API_KEY + CUA_SECRET_passcode

cua discover \
  --goal "Log in with operator id op1 and passcode demo, look up member 12345, and read their savings balance" \
  --url http://127.0.0.1:5000 \
  --app-id coreserv \
  --out artifacts/coreserv.read_savings_balance.json

cua replay \
  --artifact artifacts/coreserv.read_savings_balance.json \
  --param operator_id=op1 --param member_id=12345
```

(No `passcode` on the command line — it comes from `CUA_SECRET_passcode`.)

> Evidence (logs + screenshots + a redacted summary) for every run lands in
> `evidence/<run_id>/`. The committed evidence in this repo is from Option A and is
> labeled as mock-provider; produce a real-run artifact with Option B.

---

## CLI reference

```bash
cua discover --goal "..." --url URL --app-id ID [--out PATH] [--headed] [--allow-risky]
cua replay   --artifact PATH --param k=v [--param k=v ...] \
             [--risk confirm|block|allow] \
             [--escalate auto|none|mock|console] \   # human handoff; auto (default) = a
                                                     # human if at a terminal, else fail-safe
             [--verify none|llm|mock] \              # post-run correctness check (LLM judge)
             [--review none|console|mock] \          # human second sign-off after --verify
             [--learn none|llm] \                    # propose a draft override from a human fix
             [--headed]
cua catalog  [--dir artifacts]      # agent-facing view of saved capabilities
cua show     --artifact PATH        # print an artifact as JSON
```

Replay exit codes: `0` success or business outcome, `2` hard failure. Secrets are
never `--param`s — set `CUA_SECRET_passcode` in your env (see Setup).

Try the exceptional states yourself against a running mock app:

```bash
cua replay --artifact artifacts/coreserv.read_savings_balance.json \
  --param operator_id=op1 --param member_id=00000        # -> business_outcome: member_not_found

cua replay --artifact artifacts/coreserv.open_subaccount.json \
  --param operator_id=op1 --param member_id=12345 --param initial_deposit=250
  # -> risky step: pauses for human approval automatically (auto -> console).
  #    Type 'approve' and DON'T touch the browser; the automation performs the step.
```

Mock app data: members `12345`/`22222` exist, `00000` is not-found, `99999` is
permission-denied; passcode is `demo`.

---

## Tests

```bash
pytest -q
```

Covers the artifact schema (round-trip, param validation, locator rules), the safety
policy (allowlist, risk modes), redaction (secrets + PII patterns), and the full
replay error taxonomy (success, business outcome, element-not-found, checkpoint
failure, recovery→success, policy violation, and all three escalation paths) — the
last via an in-memory fake surface, so the suite needs no browser.

---

## Beyond the core

- **Post-run verification** (`--verify llm`): an independent model checks the run
  truly worked by cross-examining the final page against the backend record.
- **Human double-review** (`--review console`): a second sign-off after the LLM
  verdict; with no reviewer the result stays `needs_human_review` (never auto-approved).
- **Learning from fixes** (`--learn llm`): a human's stuck-step fix becomes a bounded,
  policy-checked *draft* locator override in `proposals/` for approval — never self-applied.
- **Agent-facing catalog** (`cua catalog`): the seed of an API an agent could use to
  discover and invoke capabilities by name with typed args.


## Notes & safety

- No real bank system is involved; the target is a local mock. Never point discovery
  at a site whose terms it would violate, and never use real credentials or PII.
- Secrets are never persisted: secret inputs are stored only as `{{param}}` references
  and are redacted from all logs/evidence.
- `.env` is git-ignored. Keep keys (and `CUA_SECRET_*`) out of the repo.
