"""CLI for the workflow platform: validate, list, run, approve, audit.

Usage (from the repo root, `PYTHONPATH=src` is set by the Makefile):
    python -m awap.cli demo            # run the bundled demo workflows
    python -m awap.cli list            # list registered workflows
    python -m awap.cli run <id> --input '{"account_id": "ACCT-1"}'
    python -m awap.cli approve <run_id> <step_id> --by ops-lead --decision approve
    python -m awap.cli audit-verify    # verify the hash-chained audit log
"""
from __future__ import annotations

import argparse
import json
import sys

from .audit import AuditLog
from .demo_data import demo_workflows
from .engine import Engine
from .providers import ScriptedLLMProvider, demo_tool_registry
from .store import WorkflowStore
from .validation import validate_definition


def build_engine() -> Engine:
    return Engine(
        store=WorkflowStore(),
        audit=AuditLog(),
        llm=ScriptedLLMProvider({
            "welcome": "Welcome aboard! We're glad to have you.",
            "escalation": "Escalation summary: high-priority case.",
            "auto-reply": "Thanks for reaching out — we're on it.",
        }),
        tools=demo_tool_registry(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="awap", description="AI Workflow Automation Platform")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="run the bundled demo workflows")
    sub.add_parser("list", help="list registered workflows")
    run_p = sub.add_parser("run", help="start a workflow run")
    run_p.add_argument("workflow_id")
    run_p.add_argument("--input", default="{}", help="JSON input data")
    run_p.add_argument("--idempotency-key", default="")
    ap_p = sub.add_parser("approve", help="decide a pending approval")
    ap_p.add_argument("run_id")
    ap_p.add_argument("step_id")
    ap_p.add_argument("--by", required=True)
    ap_p.add_argument("--decision", choices=["approve", "deny"], required=True)
    ap_p.add_argument("--note", default="")
    sub.add_parser("audit-verify", help="verify the hash-chained audit log")
    sweep_p = sub.add_parser("sweep", help="expire/escalate overdue approvals")
    sweep_p.add_argument("--advance-s", type=int, default=0,
                         help="advance the virtual clock before sweeping (demo only)")

    args = parser.parse_args(argv)
    engine = build_engine()
    for workflow in demo_workflows():
        engine.store.register(workflow)

    if args.command == "demo":
        return _cmd_demo(engine)
    if args.command == "list":
        for w in engine.store.list_workflows():
            print(f"{w.id}  v{w.version}  {w.name}  ({len(w.steps)} steps)")
        return 0
    if args.command == "run":
        defn = engine.store.get(args.workflow_id)
        if defn is None:
            print(f"unknown workflow: {args.workflow_id}", file=sys.stderr)
            return 2
        run = engine.start_run(defn, json.loads(args.input), args.idempotency_key)
        print(json.dumps({"run_id": run.run_id, "status": run.status.value,
                          "tokens": run.total_tokens,
                          "cost_usd": round(run.total_cost_usd, 6)}, indent=2))
        return 0
    if args.command == "approve":
        try:
            run = engine.approve(args.run_id, args.step_id, args.by,
                                 args.decision, args.note)
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"run {run.run_id}: {run.status.value}")
        return 0
    if args.command == "audit-verify":
        ok, message = engine.audit.verify()
        print(message)
        return 0 if ok else 1
    if args.command == "sweep":
        expired = engine.sweep_expired()
        print(f"swept {len(expired)} overdue approval(s)")
        for a in expired:
            print(f"  {a.run_id}:{a.step_id} -> {a.status.value}")
        return 0
    return 0


def _cmd_demo(engine: Engine) -> int:
    from .engine import VirtualClock  # noqa: F401  (demo uses the engine clock)
    triage = engine.store.get("support-triage")
    assert triage is not None
    run = engine.start_run(triage, {"priority": "high",
                                    "followups": ["call", "email"]})
    print(f"triage run {run.run_id}: {run.status.value} "
          f"({run.total_tokens} tokens, ${run.total_cost_usd:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
