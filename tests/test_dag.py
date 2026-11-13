"""DAG ordering tests."""
import pytest

from awap.dag import flat_order, topological_order
from awap.models import Step, StepType
from conftest import llm_step, simple_workflow


def test_sequence_is_one_level_per_step():
    wf = simple_workflow([llm_step("a"), llm_step("b"), llm_step("c")])
    assert topological_order(wf) == [["a"], ["b"], ["c"]]


def test_explicit_fan_in_groups_levels():
    wf = simple_workflow([
        llm_step("a"),
        llm_step("b", depends_on=[]),
        llm_step("c", depends_on=["a", "b"]),
    ])
    levels = topological_order(wf)
    assert levels[0] == ["a", "b"]
    assert levels[1] == ["c"]


def test_flat_order_respects_dependencies():
    wf = simple_workflow([
        llm_step("a"),
        llm_step("b", depends_on=["a"]),
        llm_step("c", depends_on=["a"]),
    ])
    order = flat_order(wf)
    assert order[0] == "a"
    assert set(order[1:]) == {"b", "c"}


def test_cycle_raises():
    wf = simple_workflow([
        llm_step("a", depends_on=["b"]),
        llm_step("b", depends_on=["a"]),
    ])
    with pytest.raises(ValueError, match="cycle"):
        topological_order(wf)
