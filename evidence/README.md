# Evidence

One directory per run, named `<discovery|replay-<scenario>>-<timestamp>`. Each contains:

- `run.jsonl` — a redacted, structured event stream (what happened and why): every
  agent/step action, checkpoint result, business-outcome detection, recovery,
  control transfer, and the final result.
- `summary.json` — the final structured result (status + outputs / outcome code /
  failure detail).
- `step_NN_*.png` — screenshots per step, and a richer screenshot + `*.html` DOM
  snapshot on failure/escalation/final state.

## What the runs demonstrate

Discovery (`discovery-*`): the `observe → decide → act` loop authoring an artifact.

Replay (`replay-*`), covering the full result contract / error taxonomy:

| scenario dir            | result            | shows                                            |
|-------------------------|-------------------|--------------------------------------------------|
| `replay-success-*`      | SUCCESS           | happy path, `savings_balance` extracted          |
| `replay-notfound-*`     | BUSINESS_OUTCOME  | `member_not_found` — a result, not a crash       |
| `replay-forbidden-*`    | BUSINESS_OUTCOME  | `permission_denied`                              |
| `replay-recovery-*`     | SUCCESS           | unexpected interstitial dismissed, flow resumes  |
| `replay-session-*`      | FAILED            | `session_expired` detected and reported          |
| `replay-notfound-hard-*`| FAILED            | `element_not_found` with expected/observed detail|
| `replay-escalation-*`   | SUCCESS           | risky step → human takes control → resume        |
| `replay-baddeposit-*`   | BUSINESS_OUTCOME  | `invalid_deposit` validation outcome             |

> `discovery-20260911T054306` is a GENUINE Claude-driven discovery run (see `"model": "claude-sonnet-4-5"` in its `run.jsonl`). The `replay-20260911T0604xx` runs replay that real artifact (success / member_not_found / member_restricted). The remaining `replay-<scenario>-*` runs come from `scripts/demo.py` and cover the rest of the taxonomy (recovery, escalation, session timeout, invalid deposit, element-not-found). Replay never uses an LLM, so every replay run here is fully deterministic regardless of how its artifact was seeded.
