"""End-to-end demo of the AI Workflow Automation Platform.

Runs on synthetic data with scripted providers (no network, no keys) and
prints verifiable numbers for each scenario:

1. Support triage: score -> branch -> parallel fan-out -> loop.
2. Customer onboarding: LLM draft -> CRM lookup -> approval gate (blocks,
   then releases on approve) -> notify.
3. Budget cap: a 10-token budget halts the run fail-closed.
4. Approval expiry: an unanswered gate escalates to the duty manager.
5. Idempotency: the same key returns the same run, no re-execution.
6. Audit chain verification.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from awap.audit import AuditLog
from awap.demo_data import budget_demo_workflow, demo_workflows, onboarding_workflow, triage_workflow
from awap.engine import Engine, VirtualClock
from awap.providers import ScriptedLLMProvider, demo_tool_registry
from awap.store import WorkflowStore


def build_engine() -> Engine:
    return Engine(
        store=WorkflowStore(),
        audit=AuditLog(),
        llm=ScriptedLLMProvider({
            "welcome": "Welcome aboard! We're glad to have you.",
            "escalation": "Escalation summary: high-priority case routed to senior support.",
            "auto-reply": "Thanks for reaching out — we've logged your case and will reply shortly.",
        }),
        tools=demo_tool_registry(),
        clock=VirtualClock(),
    )


def main() -> int:
    engine = build_engine()
    for workflow in demo_workflows():
        engine.store.register(workflow)
    print("AI Workflow Automation Platform — demo")
    print("=" * 60)

    # 1. Triage: branch + parallel + loop.
    triage = triage_workflow()
    run = engine.start_run(triage, {"priority": "high", "followups": ["call", "email"]})
    step_summary = ", ".join(f"{s.step_id}:{s.status.value}" for s in run.step_runs)
    print(f"\n[1] support-triage run {run.run_id}: {run.status.value}")
    print(f"    steps: {step_summary}")
    print(f"    tokens={run.total_tokens} cost=${run.total_cost_usd:.4f} "
          f"latency={run.total_latency_ms}ms")
    assert run.status.value == "succeeded"
    assert run.step_run("escalate").status.value == "succeeded"
    assert run.step_run("auto_reply").status.value == "skipped"
    assert run.step_run("followups").output["iterations"] == 2

    # 2. Onboarding: approval gate blocks, then releases.
    onboarding = onboarding_workflow()
    run2 = engine.start_run(onboarding, {"account_id": "ACCT-1001"})
    print(f"\n[2] customer-onboarding run {run2.run_id}: {run2.status.value}")
    assert run2.status.value == "waiting_approval"
    print("    approval gate blocked the run at step 'review' (as designed)")
    run2 = engine.approve(run2.run_id, "review", "ops-lead", "approve",
                          note="Welcome message looks good.")
    print(f"    after approval by ops-lead: {run2.status.value}")
    assert run2.status.value == "succeeded"
    print(f"    tokens={run2.total_tokens} cost=${run2.total_cost_usd:.4f}")

    # 3. Budget cap halts fail-closed.
    budget_wf = budget_demo_workflow()
    run3 = engine.start_run(budget_wf, {})
    print(f"\n[3] budget-demo run {run3.run_id}: {run3.status.value}")
    print(f"    error: {run3.error}")
    assert run3.status.value == "failed"
    assert "budget exceeded" in run3.error
    assert run3.step_run("second").status.value == "failed"  # never produced output
    assert run3.step_run("second").output is None

    # 4. Approval expiry escalates.
    run4 = engine.start_run(onboarding, {"account_id": "ACCT-1002"})
    assert run4.status.value == "waiting_approval"
    engine.clock.advance_s(3601)  # past the 3600s expiry
    expired = engine.sweep_expired()
    print(f"\n[4] approval expiry sweep: {len(expired)} expired")
    for ap in expired:
        print(f"    {ap.run_id}:{ap.step_id} -> {ap.status.value} "
              f"(escalated to {ap.escalated_to}, count={ap.escalation_count})")
    assert expired and expired[0].status.value == "escalated"

    # 5. Idempotency: same key, same run.
    run5a = engine.start_run(triage, {"priority": "low", "followups": []},
                             idempotency_key="demo-key-1")
    run5b = engine.start_run(triage, {"priority": "low", "followups": []},
                             idempotency_key="demo-key-1")
    print(f"\n[5] idempotency: first={run5a.run_id} second={run5b.run_id} "
          f"same={run5a.run_id == run5b.run_id}")
    assert run5a.run_id == run5b.run_id

    # 6. Audit chain.
    ok, message = engine.audit.verify()
    print(f"\n[6] audit chain: {message} (ok={ok})")
    assert ok

    print("\nDemo complete: 6/6 scenarios behaved as designed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
