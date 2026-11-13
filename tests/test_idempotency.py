"""Idempotency and versioning tests."""
from awap.models import Step, StepType
from conftest import llm_step, simple_workflow


def test_same_key_returns_same_run_without_reexecution(engine, llm):
    wf = simple_workflow([llm_step("s1")])
    run1 = engine.start_run(wf, {}, idempotency_key="k-1")
    run2 = engine.start_run(wf, {}, idempotency_key="k-1")
    assert run1.run_id == run2.run_id
    assert len(llm.calls) == 1  # provider called only once


def test_different_keys_create_different_runs(engine):
    wf = simple_workflow([llm_step("s1")])
    run1 = engine.start_run(wf, {}, idempotency_key="k-1")
    run2 = engine.start_run(wf, {}, idempotency_key="k-2")
    assert run1.run_id != run2.run_id


def test_no_key_always_creates_new_run(engine):
    wf = simple_workflow([llm_step("s1")])
    run1 = engine.start_run(wf, {})
    run2 = engine.start_run(wf, {})
    assert run1.run_id != run2.run_id


def test_register_assigns_version_1(engine):
    wf = simple_workflow([llm_step("s1")], id="wf-v")
    stored = engine.store.register(wf)
    assert stored.version == 1


def test_update_bumps_version(engine):
    wf = simple_workflow([llm_step("s1")], id="wf-v")
    engine.store.register(wf)
    updated = engine.store.update(simple_workflow([llm_step("s1"), llm_step("s2")], id="wf-v"))
    assert updated.version == 2
    assert engine.store.version_count("wf-v") == 2


def test_rollback_creates_new_version_with_old_content(engine):
    wf = simple_workflow([llm_step("s1")], id="wf-v")
    engine.store.register(wf)
    engine.store.update(simple_workflow([llm_step("s1"), llm_step("s2")], id="wf-v"))
    restored = engine.store.rollback("wf-v", 1)
    assert restored.version == 3
    assert [s.id for s in restored.steps] == ["s1"]
    latest = engine.store.get("wf-v")
    assert [s.id for s in latest.steps] == ["s1"]


def test_rollback_unknown_version_raises(engine):
    import pytest
    engine.store.register(simple_workflow([llm_step("s1")], id="wf-v"))
    with pytest.raises(KeyError):
        engine.store.rollback("wf-v", 99)


def test_run_pins_workflow_version(engine):
    wf = simple_workflow([llm_step("s1")], id="wf-v")
    engine.store.register(wf)
    engine.store.update(simple_workflow([llm_step("s1"), llm_step("s2")], id="wf-v"))
    v1 = engine.store.get("wf-v", 1)
    run = engine.start_run(v1, {})
    assert run.workflow_version == 1
    assert len(run.step_runs) == 1
