"""Approval gate tests: pause, approve, deny, expiry, escalation."""
import pytest

from awap.models import Step, StepType
from conftest import llm_step, simple_workflow


def approval_workflow(**overrides):
    cfg = {"approvers": ["ops-lead"], "expires_in_s": 3600,
           "on_expiry": "escalate", "escalate_to": ["duty-manager"]}
    cfg.update(overrides)
    return simple_workflow([
        llm_step("draft"),
        Step(id="review", type=StepType.APPROVAL, config=cfg),
        llm_step("after", prompt="done"),
    ])


def test_run_pauses_at_approval(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    assert run.status.value == "waiting_approval"
    assert run.step_run("review").status.value == "waiting_approval"
    assert run.step_run("notify") is None  # not executed yet


def test_approve_resumes_and_completes(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    run = engine.approve(run.run_id, "review", "ops-lead", "approve", note="ok")
    assert run.status.value == "succeeded"
    assert run.step_run("notify").status.value == "succeeded"


def test_deny_fails_run(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    run = engine.approve(run.run_id, "review", "ops-lead", "deny", note="wrong draft")
    assert run.status.value == "failed"
    assert "denied" in run.error
    assert run.step_run("notify") is None


def test_non_approver_cannot_decide(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    with pytest.raises(ValueError, match="not an approver"):
        engine.approve(run.run_id, "review", "mallory", "approve")


def test_double_decision_rejected(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    engine.approve(run.run_id, "review", "ops-lead", "approve")
    with pytest.raises(ValueError, match="already"):
        engine.approve(run.run_id, "review", "ops-lead", "approve")


def test_expiry_escalates_to_duty_manager(engine, clock):
    run = engine.start_run(approval_workflow(), {})
    assert run.status.value == "waiting_approval"
    clock.advance_s(3601)
    expired = engine.sweep_expired()
    assert len(expired) == 1
    ap = expired[0]
    assert ap.status.value == "escalated"
    assert ap.escalated_to == ["duty-manager"]
    assert ap.escalation_count == 1
    assert ap.approvers == ["duty-manager"]
    # Escalated approver can now decide; original approver cannot.
    with pytest.raises(ValueError, match="not an approver"):
        engine.approve(run.run_id, "review", "ops-lead", "approve")
    run = engine.approve(run.run_id, "review", "duty-manager", "approve")
    assert run.status.value == "succeeded"


def test_expiry_with_deny_policy_fails_run(engine, clock):
    run = engine.start_run(approval_workflow(on_expiry="deny"), {})
    clock.advance_s(3601)
    expired = engine.sweep_expired()
    assert expired[0].status.value == "expired"
    run = engine.store.get_run(run.run_id)
    assert run.status.value == "failed"
    assert "expired" in run.error


def test_no_expiry_before_deadline(engine, clock):
    engine.start_run(approval_workflow(), {})
    clock.advance_s(3599)
    assert engine.sweep_expired() == []


def test_checkpoint_preserved_across_pause(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    stored = engine.store.get_run(run.run_id)
    assert stored.checkpoint_step_index == 2  # paused at the review step
    assert stored.step_run("draft").status.value == "succeeded"
