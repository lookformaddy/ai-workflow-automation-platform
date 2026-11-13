"""Validation tests: definitions fail fast with clear errors."""
from awap.models import Budget, Step, StepType, WorkflowDefinition
from awap.validation import assert_valid, validate_definition
from conftest import llm_step, simple_workflow, tool_step


def test_valid_simple_workflow():
    wf = simple_workflow([llm_step(), tool_step("t1")])
    assert validate_definition(wf) == []


def test_empty_steps_rejected():
    assert any("at least one step" in e for e in validate_definition(simple_workflow([])))


def test_duplicate_step_ids():
    wf = simple_workflow([llm_step("s1"), llm_step("s1")])
    assert any("duplicate step id: s1" in e for e in validate_definition(wf))


def test_unknown_depends_on():
    wf = simple_workflow([llm_step("s1", depends_on=["nope"])])
    assert any("unknown depends_on" in e for e in validate_definition(wf))


def test_self_dependency():
    wf = simple_workflow([llm_step("s1", depends_on=["s1"])])
    errors = validate_definition(wf)
    assert any("depends on itself" in e for e in errors)


def test_dependency_cycle():
    a = llm_step("a", depends_on=["b"])
    b = llm_step("b", depends_on=["a"])
    errors = validate_definition(simple_workflow([a, b]))
    assert any("cycle" in e for e in errors)


def test_llm_requires_prompt():
    wf = simple_workflow([Step(id="x", type=StepType.LLM, config={})])
    assert any("requires config.prompt" in e for e in validate_definition(wf))


def test_tool_requires_tool_name():
    wf = simple_workflow([Step(id="x", type=StepType.TOOL, config={})])
    assert any("requires config.tool" in e for e in validate_definition(wf))


def test_approval_requires_approvers():
    wf = simple_workflow([Step(id="x", type=StepType.APPROVAL, config={})])
    assert any("requires config.approvers" in e for e in validate_definition(wf))


def test_branch_unknown_arm_reference():
    wf = simple_workflow([
        Step(id="b", type=StepType.BRANCH,
             config={"condition": "x > 1", "true_steps": ["ghost"], "false_steps": []}),
    ])
    assert any("unknown true_steps reference" in e for e in validate_definition(wf))


def test_parallel_requires_two_branches():
    wf = simple_workflow([
        Step(id="p", type=StepType.PARALLEL, config={"branches": [["s1"]]}),
        llm_step("s1"),
    ])
    assert any("requires >= 2 branches" in e for e in validate_definition(wf))


def test_loop_requires_items_and_body():
    wf = simple_workflow([Step(id="l", type=StepType.LOOP, config={})])
    errors = validate_definition(wf)
    assert any("requires config.items_key" in e for e in errors)
    assert any("requires a non-empty config.body" in e for e in errors)


def test_invalid_budget_rejected():
    wf = simple_workflow([llm_step()], budget=Budget(max_tokens=0))
    assert any("max_tokens" in e for e in validate_definition(wf))


def test_budget_smaller_than_step_count():
    wf = simple_workflow([llm_step("a"), llm_step("b")], budget=Budget(max_steps=1))
    assert any("max_steps" in e for e in validate_definition(wf))


def test_assert_valid_raises():
    import pytest
    with pytest.raises(ValueError, match="invalid workflow definition"):
        assert_valid(simple_workflow([]))
