"""Hash-chained audit log tests."""
from awap.audit import AuditLog


def test_empty_log_verifies():
    log = AuditLog()
    ok, message = log.verify()
    assert ok and "0 entries" in message


def test_chain_links_and_verifies():
    log = AuditLog()
    log.append(1000, "run_1", "run_started", {"workflow_id": "w"})
    log.append(1001, "run_1", "step_succeeded", {"step_id": "s1"})
    ok, message = log.verify()
    assert ok
    assert "2 entries" in message
    assert log.entries[1].prev_hash == log.entries[0].hash
    assert log.entries[0].prev_hash == "0" * 64


def test_tampered_payload_breaks_verification():
    log = AuditLog()
    log.append(1000, "run_1", "run_started", {"workflow_id": "w"})
    log.append(1001, "run_1", "step_succeeded", {"step_id": "s1"})
    log.entries[0].payload["workflow_id"] = "tampered"
    ok, message = log.verify()
    assert not ok
    assert "hash mismatch" in message


def test_reordered_entries_break_verification():
    log = AuditLog()
    log.append(1000, "run_1", "run_started", {})
    log.append(1001, "run_1", "step_succeeded", {})
    log.entries.reverse()
    ok, _ = log.verify()
    assert not ok


def test_for_run_filters():
    log = AuditLog()
    log.append(1000, "run_1", "run_started", {})
    log.append(1001, "run_2", "run_started", {})
    log.append(1002, "run_1", "run_succeeded", {})
    assert len(log.for_run("run_1")) == 2
    assert len(log.for_run("run_2")) == 1


def test_engine_audit_trail_covers_run(engine, onboarding):
    run = engine.start_run(onboarding, {"account_id": "ACCT-1"})
    engine.approve(run.run_id, "review", "ops-lead", "approve")
    events = [e.event for e in engine.audit.for_run(run.run_id)]
    assert "run_started" in events
    assert "approval_requested" in events
    assert "approval_decided" in events
    assert "run_succeeded" in events
    ok, _ = engine.audit.verify()
    assert ok
