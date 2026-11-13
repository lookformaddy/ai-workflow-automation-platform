"""Engine tests: happy path, branch, parallel, loop, transforms, resume."""
from awap.engine import evaluate_condition
from awap.models import Step, StepType
from conftest import llm_step, simple_workflow, tool_step


def test_simple_run_succeeds(engine):
    wf = simple_workflow([llm_step("draft", prompt="welcome", output_key="draft"),
                          tool_step("lookup")])
    run = engine.start_run(wf, {"account_id": "ACCT-1"})
    assert run.status.value == "succeeded"
    assert run.context["draft"] == "Welcome aboard!"
    assert run.context["record"]["account_id"] == "ACCT-0001"
    assert run.total_tokens > 0
    assert run.total_cost_usd > 0


def test_triage_high_priority_takes_true_arm(engine, triage):
    run = engine.start_run(triage, {"priority": "high", "followups": []})
    assert run.status.value == "succeeded"
    assert run.step_run("escalate").status.value == "succeeded"
    assert run.step_run("auto_reply").status.value == "skipped"
    assert run.step_run("route").output["arm"] == "true_steps"


def test_triage_low_priority_takes_false_arm(engine, triage):
    run = engine.start_run(triage, {"priority": "low", "followups": []})
    assert run.status.value == "succeeded"
    assert run.step_run("auto_reply").status.value == "succeeded"
    assert run.step_run("escalate").status.value == "skipped"


def test_parallel_fanout_runs_both_branches(engine, triage):
    run = engine.start_run(triage, {"priority": "low", "followups": []})
    assert run.step_run("inventory").status.value == "succeeded"
    assert run.step_run("notify_ops").status.value == "succeeded"
    fanout = run.step_run("fanout")
    assert fanout.status.value == "succeeded"
    assert "branch_0:inventory" in fanout.output
    assert "branch_1:notify_ops" in fanout.output


def test_loop_iterates_over_items(engine, triage):
    run = engine.start_run(triage, {"priority": "low",
                                    "followups": ["a", "b", "c"]})
    loop = run.step_run("followups")
    assert loop.status.value == "succeeded"
    assert loop.output["iterations"] == 3
    note = run.step_run("followup_note")
    assert note.status.value == "succeeded"
    assert note.attempts == 3


def test_loop_capped_at_max_iterations(engine):
    body = Step(id="note", type=StepType.TRANSFORM,
                config={"op": "template", "template": "x {item}"})
    wf = simple_workflow([
        Step(id="loop", type=StepType.LOOP,
             config={"items_key": "items", "body": ["note"], "max_iterations": 2}),
        body,
    ])
    run = engine.start_run(wf, {"items": [1, 2, 3, 4]})
    assert run.step_run("loop").output["iterations"] == 2


def test_transform_template_and_extract(engine):
    wf = simple_workflow([
        tool_step("lookup"),
        Step(id="name", type=StepType.TRANSFORM,
             config={"op": "extract", "source": "record", "field": "tier",
                     "output_key": "tier"}),
        Step(id="msg", type=StepType.TRANSFORM,
             config={"op": "template", "template": "Tier is {tier}",
                     "output_key": "msg"}),
    ])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    assert run.context["tier"] == "standard"
    assert run.context["msg"] == "Tier is standard"


def test_on_error_continue_keeps_running(engine, tools):
    tools.register("flaky", lambda a: (_ for _ in ()).throw(Exception("boom")))
    wf = simple_workflow([
        Step(id="bad", type=StepType.TOOL, config={"tool": "flaky", "args": {}},
             on_error="continue"),
        llm_step("after"),
    ])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    assert run.step_run("bad").status.value == "failed"
    assert run.step_run("after").status.value == "succeeded"


def test_failing_step_fails_run(engine, tools):
    tools.register("always_bad", lambda a: (_ for _ in ()).throw(Exception("boom")))
    wf = simple_workflow([
        Step(id="bad", type=StepType.TOOL, config={"tool": "always_bad", "args": {}}),
        llm_step("after"),
    ])
    run = engine.start_run(wf, {})
    assert run.status.value == "failed"
    assert run.step_run("after") is None


def test_unknown_tool_fails_step(engine):
    wf = simple_workflow([tool_step("t", tool="no_such_tool")])
    run = engine.start_run(wf, {})
    assert run.status.value == "failed"
    assert "unknown tool" in run.step_run("t").error


def test_resume_after_cancel_is_rejected(engine):
    wf = simple_workflow([llm_step()])
    run = engine.start_run(wf, {})
    assert run.status.value == "succeeded"
    import pytest
    with pytest.raises(ValueError):
        engine.resume(run.run_id)


def test_condition_evaluation():
    assert evaluate_condition("score >= 0.7", {"score": 0.8}) is True
    assert evaluate_condition("score >= 0.7", {"score": 0.5}) is False
    assert evaluate_condition("a == 'high' and b > 2", {"a": "high", "b": 3}) is True
    assert evaluate_condition("scored['score'] >= 0.7", {"scored": {"score": 0.9}}) is True
    assert evaluate_condition("not flag", {"flag": False}) is True


def test_condition_rejects_unsafe_syntax():
    import pytest
    with pytest.raises(ValueError):
        evaluate_condition("__import__('os').system('x')", {})
    with pytest.raises(ValueError):
        evaluate_condition("x.y", {"x": object()})
