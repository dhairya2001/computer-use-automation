"""Command-line interface.

    cua discover  --goal "..." --url ... --app-id coreserv --out artifacts/x.json
    cua replay    --artifact artifacts/x.json --param member_id=12345 ...
    cua catalog   [--dir artifacts]
    cua show      --artifact artifacts/x.json

Discovery uses an LLM (Anthropic by default) and needs ANTHROPIC_API_KEY.
Replay never touches an LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .agent.llm import AnthropicProvider
from .agent.loop import AgentLoop
from .config import (
    ARTIFACTS_ROOT,
    DEFAULT_BASE_URL,
    build_target,
    load_artifact,
    make_logger,
    save_artifact,
)
from .replay.engine import ReplayEngine
from .safety.policy import RiskMode
from .surface.web import PlaywrightWebSurface


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param must be key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
def cmd_discover(args) -> int:
    target = build_target(app_id=args.app_id, base_url=args.url or DEFAULT_BASE_URL,
                          tenant_id=args.tenant)
    logger = make_logger("discovery")
    surface = PlaywrightWebSurface(headless=not args.headed)
    surface.start()

    provider = AnthropicProvider(model=args.model)

    from .safety.policy import SafetyPolicy
    policy = SafetyPolicy(
        allowed_url_prefixes=[args.allow or _prefix(target.base_url)],
        risk_mode=RiskMode.ALLOW if args.allow_risky else RiskMode.CONFIRM,
    )

    loop = AgentLoop(provider, surface, args.goal, target, policy, logger,
                     max_steps=args.max_steps)
    try:
        result = loop.run()
    finally:
        surface.close()

    print(f"[discovery] status={result.status} steps={result.steps_taken}")
    print(f"[discovery] evidence -> {result.evidence_dir}")
    if result.artifact is not None:
        out = args.out or os.path.join(ARTIFACTS_ROOT, f"{result.artifact.capability.id}.json")
        save_artifact(result.artifact, out)
        print(f"[discovery] artifact -> {out}")
        return 0
    print(f"[discovery] no artifact produced: {result.message}")
    return 1


def cmd_replay(args) -> int:
    from .secrets import SecretError, resolve_secret

    artifact = load_artifact(args.artifact)
    raw = _kv(args.param)
    secret_names = artifact.secret_input_names()

    # Non-secret params come from --param; secrets are resolved out-of-band
    # (env CUA_SECRET_<name> / vault:// / stdin) so they never sit in history.
    params = {k: v for k, v in raw.items() if k not in secret_names}
    for name in secret_names:
        try:
            params[name] = resolve_secret(name, raw.get(name))
        except SecretError as e:
            print(f"[secret] {e}")
            return 2

    secret_vals = [params[n] for n in secret_names if n in params]
    logger = make_logger("replay", secret_values=secret_vals)

    # Resolve the escalation mode FIRST (before building the browser). Default is
    # "auto": if a human is present at a real terminal, route interventions to them
    # automatically (no flag needed); if unattended (no TTY, e.g. an agent/CI run),
    # fall back to a safe stop that records the request and returns needs_human.
    mode = args.escalate
    if mode == "auto":
        mode = "console" if (sys.stdin is not None and sys.stdin.isatty()) else "none"
        print(f"[escalate] auto -> {mode} "
              f"({'interactive terminal' if mode == 'console' else 'unattended: fail-safe'})")

    # A console handoff needs a VISIBLE browser so the human can actually take over
    # the same live session -- force headed for it (unless already headed).
    headed = args.headed or (mode == "console")
    if mode == "console" and not args.headed:
        print("[escalate] console handoff -> opening a visible browser window so you "
              "can take control when it pauses.")
    surface = PlaywrightWebSurface(headless=not headed)

    escalation = None
    if mode in ("console", "mock"):
        from .escalation.handoff import InterventionQueue
        # Every raised request is written as a JSON file into ./interventions/
        # (an inspectable "operator inbox"), then flipped to resolved when handled.
        queue = InterventionQueue(logger=logger, inbox_dir="interventions")
        if mode == "console":
            from .escalation.handoff import HumanConsoleHandler
            escalation = HumanConsoleHandler(queue=queue)
        else:
            from .escalation.handoff import MockOperatorHandler
            escalation = MockOperatorHandler(
                actions=[lambda s: "operator acknowledged and completed the blocked step"],
                queue=queue,
            )

    # Optional post-run verification: LLM judge (--verify) + human review (--review).
    verifier = None
    backend_fetch = None
    if args.verify != "none" or args.review != "none":
        from .verify import (
            AnthropicJudge, ConsoleReviewer, MockJudge, MockReviewer, Verifier,
        )
        judge = None
        if args.verify == "llm":
            judge = AnthropicJudge()
        elif args.verify == "mock":
            judge = MockJudge()
        reviewer = None
        if args.review == "console":
            reviewer = ConsoleReviewer()
        elif args.review == "mock":
            reviewer = MockReviewer()
        verifier = Verifier(judge=judge, reviewer=reviewer)
        backend_fetch = _backend_fetch

    # Optional learning: turn a human's stuck-step fix into a draft override.
    learner = None
    if args.learn == "llm":
        from .learning import AnthropicSynthesizer
        learner = AnthropicSynthesizer()

    engine = ReplayEngine(surface, logger, risk_mode=RiskMode(args.risk), escalation=escalation,
                          verifier=verifier, backend_fetch=backend_fetch, learner=learner)
    result = engine.replay(artifact, params)

    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status.value in ("success", "business_outcome") else 2


def _backend_fetch(artifact, params) -> dict:
    """Supply backend truth for the verifier to cross-check against.

    For this mock that means the JSON sub-account store; a real integration would
    query the core system's API or DB read-replica instead.
    """
    member_id = params.get("member_id", "")
    out = {"member_id": member_id}
    try:
        with open("subaccounts.json", "r", encoding="utf-8") as fh:
            store = json.load(fh)
        out["subaccounts_for_member"] = store.get(member_id, [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        out["subaccounts_for_member"] = []
    return out


def cmd_catalog(args) -> int:
    d = args.dir or ARTIFACTS_ROOT
    if not os.path.isdir(d):
        print(f"(no artifacts dir at {d})")
        return 0
    rows = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            a = load_artifact(os.path.join(d, fn))
        except Exception as e:
            print(f"  ! {fn}: unreadable ({e})")
            continue
        inputs = ", ".join(f"{p.name}:{p.type.value}{'*' if p.secret else ''}" for p in a.inputs)
        outputs = ", ".join(f"{o.name}:{o.type.value}" for o in a.outputs)
        rows.append((a.capability.id, a.capability.version, a.capability.status.value, inputs, outputs))
    if not rows:
        print("(catalog empty)")
        return 0
    print("CAPABILITY CATALOG")
    for cid, ver, status, inp, out in rows:
        print(f"  - {cid}  v{ver}  [{status}]")
        print(f"      inputs : {inp or '(none)'}")
        print(f"      outputs: {out or '(none)'}")
    return 0


def cmd_show(args) -> int:
    artifact = load_artifact(args.artifact)
    print(artifact.to_json())
    return 0


# --------------------------------------------------------------------------- #
def _prefix(url: str) -> str:
    # http://host:port  -> allowlist prefix
    parts = url.split("/")
    return "/".join(parts[:3]) + "/"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cua", description="Computer-Use Automation System")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run; emits an artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--url", help=f"entry point (default {DEFAULT_BASE_URL})")
    d.add_argument("--app-id", default="coreserv")
    d.add_argument("--tenant", default=None)
    d.add_argument("--model", default=None, help="Anthropic model id")
    d.add_argument("--out", default=None, help="artifact output path")
    d.add_argument("--allow", default=None, help="URL allowlist prefix")
    d.add_argument("--allow-risky", action="store_true", help="permit risky actions in discovery")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--headed", action="store_true", help="show the browser")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of an artifact (no LLM)")
    r.add_argument("--artifact", required=True)
    r.add_argument("--param", action="append", default=[], help="key=value (repeatable)")
    r.add_argument("--risk", choices=[m.value for m in RiskMode], default="confirm")
    r.add_argument("--escalate", choices=["auto", "none", "mock", "console"], default="auto",
                   help="who handles an intervention: auto (default: a human if at a "
                        "terminal, else fail-safe) / console / mock / none")
    r.add_argument("--verify", choices=["none", "llm", "mock"], default="none",
                   help="post-run LLM verification of correctness (llm needs ANTHROPIC_API_KEY)")
    r.add_argument("--review", choices=["none", "console", "mock"], default="none",
                   help="human second sign-off after verification (banking double-review)")
    r.add_argument("--learn", choices=["none", "llm"], default="none",
                   help="on a human-fixed stuck step, propose a draft locator override (llm needs a key)")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("catalog", help="list saved capabilities (agent-facing view)")
    c.add_argument("--dir", default=None)
    c.set_defaults(func=cmd_catalog)

    s = sub.add_parser("show", help="print an artifact")
    s.add_argument("--artifact", required=True)
    s.set_defaults(func=cmd_show)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
