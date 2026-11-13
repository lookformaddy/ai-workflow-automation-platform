"""Budget tests: fail-closed caps per workflow and per tenant."""
import pytest

from awap.budgets import BudgetExceeded, BudgetManager, BudgetTracker
from awap.models import Budget, Step, StepType
from conftest import llm_step, simple_workflow


def test_tiny_token_budget_halts_run(engine, budget_demo):
    run = engine.start_run(budget_demo, {})
    assert run.status.value == "failed"
    assert "budget exceeded" in run.error
    # First step ran; second never produced output (halted at the pre-check).
    assert run.step_run("first").status.value == "succeeded"
    second = run.step_run("second")
    assert second.status.value == "failed"
    assert second.output is None


def test_step_budget_prevents_overrun(engine):
    # A loop re-executes its body, so step executions can exceed the
    # declared step count even though the definition itself is valid.
    body = Step(id="note", type=StepType.TRANSFORM,
                config={"op": "template", "template": "x {item}"})
    wf = simple_workflow([
        llm_step("a"),
        Step(id="loop", type=StepType.LOOP,
             config={"items_key": "items", "body": ["note"], "max_iterations": 5}),
        body,
    ], budget=Budget(max_steps=3, max_tokens=100_000, max_cost_usd=100.0,
                     max_latency_ms=1_000_000))
    run = engine.start_run(wf, {"items": [1, 2, 3, 4, 5]})
    assert run.status.value == "failed"
    assert "steps" in run.error


def test_tracker_check_before_step():
    tracker = BudgetTracker("test", Budget(max_tokens=10))
    tracker.check_before_step(est_tokens=5)
    tracker.record(tokens=8)
    with pytest.raises(BudgetExceeded, match="tokens"):
        tracker.check_before_step(est_tokens=5)


def test_tracker_check_after_step():
    tracker = BudgetTracker("test", Budget(max_cost_usd=0.01))
    tracker.record(cost_usd=0.02)
    with pytest.raises(BudgetExceeded, match="cost_usd"):
        tracker.check_after_step()


def test_tenant_budget_shared_across_runs(engine):
    # Tenant cap of 20 tokens: one LLM call uses 11 tokens (8 in + 3 out),
    # so the second run must halt fail-closed once the cap is crossed.
    manager = BudgetManager(default_tenant_budget=Budget(max_tokens=20))
    engine.budget_manager = manager
    wf = simple_workflow([llm_step("a", prompt="hello world, please draft a note")])
    run1 = engine.start_run(wf, {})
    assert run1.status.value == "succeeded"
    assert run1.total_tokens == 11
    run2 = engine.start_run(wf, {}, idempotency_key="other")
    assert run2.status.value == "failed"
    assert "tenant:default" in run2.error
    assert "budget exceeded" in run2.error


def test_budget_exceeded_exception_carries_scope():
    exc = BudgetExceeded("tenant:acme", "tokens", 100, 150)
    assert exc.scope == "tenant:acme"
    assert exc.dimension == "tokens"
    assert "150 > limit 100" in str(exc)
