"""Trigger tests: cron matching and event matching."""
from datetime import datetime, timezone

import pytest

from awap.triggers import cron_matches, event_matches, next_due


def dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_cron_every_30_minutes():
    assert cron_matches("*/30 * * * *", dt(2026, 11, 13, 9, 30))
    assert cron_matches("*/30 * * * *", dt(2026, 11, 13, 9, 0))
    assert not cron_matches("*/30 * * * *", dt(2026, 11, 13, 9, 15))


def test_cron_weekday_mornings():
    # 2026-11-13 is a Friday.
    assert cron_matches("0 9 * * MON-FRI", dt(2026, 11, 13, 9, 0))
    assert not cron_matches("0 9 * * MON-FRI", dt(2026, 11, 14, 9, 0))  # Saturday
    assert not cron_matches("0 9 * * MON-FRI", dt(2026, 11, 13, 10, 0))


def test_cron_list_and_range():
    assert cron_matches("0 9,17 * * *", dt(2026, 11, 13, 17, 0))
    assert not cron_matches("0 9,17 * * *", dt(2026, 11, 13, 12, 0))
    assert cron_matches("0 9-11 * * *", dt(2026, 11, 13, 10, 0))
    assert not cron_matches("0 9-11 * * *", dt(2026, 11, 13, 12, 0))


def test_cron_month_names():
    assert cron_matches("0 0 1 JAN *", dt(2027, 1, 1, 0, 0))
    assert not cron_matches("0 0 1 JAN *", dt(2027, 2, 1, 0, 0))


def test_cron_invalid_field_count():
    with pytest.raises(ValueError):
        cron_matches("* * * *", dt(2026, 11, 13, 9, 0))


def test_event_match_type_and_filter():
    assert event_matches("ticket.created", "ticket.created", {"priority": "high"},
                         {"priority": "high"})
    assert not event_matches("ticket.created", "ticket.created", {"priority": "low"},
                             {"priority": "high"})
    assert not event_matches("ticket.created", "ticket.closed", {"priority": "high"})


def test_next_due():
    nxt = next_due("0 9 * * MON-FRI", dt(2026, 11, 13, 8, 0))
    assert nxt == dt(2026, 11, 13, 9, 0)
    nxt = next_due("0 9 * * MON-FRI", dt(2026, 11, 13, 10, 0))
    assert nxt == dt(2026, 11, 16, 9, 0)  # next Monday


def test_demo_workflows_have_triggers(triage, onboarding):
    assert any(t.kind == "cron" for t in triage.triggers)
    assert any(t.kind == "event" for t in onboarding.triggers)
    assert cron_matches(triage.triggers[0].expression, dt(2026, 11, 13, 9, 30))
