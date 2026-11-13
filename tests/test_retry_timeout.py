"""Retry, timeout and backoff tests (deterministic, scripted failures)."""
from awap.models import RetryPolicy, Step, StepType
from awap.providers import ScriptedLLMProvider
from conftest import llm_step, simple_workflow, tool_step


def test_llm_retries_then_succeeds(engine):
    engine.llm = ScriptedLLMProvider({"hello": "hi there"}, fail_times=2)
    wf = simple_workflow([llm_step("s1", prompt="hello")])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    sr = run.step_run("s1")
    assert sr.attempts == 3
    assert sr.backoff_delays_ms == [100, 200]


def test_retry_exhaustion_fails_step(engine):
    engine.llm = ScriptedLLMProvider({}, fail_times=10)
    wf = simple_workflow([llm_step("s1")])
    run = engine.start_run(wf, {})
    assert run.status.value == "failed"
    sr = run.step_run("s1")
    assert sr.attempts == 3  # default max_attempts
    assert "after 3 attempts" in sr.error


def test_custom_retry_policy_backoff_schedule():
    policy = RetryPolicy(max_attempts=4, backoff_base_ms=200, backoff_multiplier=3.0,
                         max_backoff_ms=1000)
    assert policy.delays_ms() == [200, 600, 1000]


def test_backoff_capped_at_max():
    policy = RetryPolicy(max_attempts=5, backoff_base_ms=100, backoff_multiplier=10.0,
                         max_backoff_ms=250)
    assert policy.delays_ms() == [100, 250, 250, 250]


def test_step_timeout_retries_then_fails(engine):
    # Provider reports a 5s duration; step timeout is 1s.
    engine.llm = ScriptedLLMProvider({"hello": "hi"}, fixed_duration_ms=5000)
    wf = simple_workflow([llm_step("s1", prompt="hello", timeout_ms=1000)])
    run = engine.start_run(wf, {})
    assert run.status.value == "failed"
    sr = run.step_run("s1")
    assert sr.attempts == 3
    assert "timeout" in sr.error


def test_tool_retries_then_succeeds(engine, tools):
    tools.register("flaky", lambda a: {"ok": True}, fail_times=1)
    wf = simple_workflow([tool_step("t1", tool="flaky")])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    assert run.step_run("t1").attempts == 2


def test_retry_audit_events_recorded(engine):
    engine.llm = ScriptedLLMProvider({}, fail_times=1)
    wf = simple_workflow([llm_step("s1")])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    retries = [e for e in engine.audit.entries if e.event == "step_retried"]
    assert len(retries) == 1
    assert retries[0].payload["backoff_ms"] == 100
