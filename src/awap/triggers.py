"""Triggers: cron schedules and event matching.

The cron matcher supports the standard 5-field form
`minute hour day-of-month month day-of-week`, with `*`, lists, ranges,
steps (`*/15`) and weekday names (`MON-FRI`). Event triggers match on
event type plus an optional exact-match payload filter.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_WEEKDAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _num(token: str, names: dict[str, int] | None) -> int:
    token = token.strip().upper()
    if names and token in names:
        return names[token]
    return int(token)


def _parse_field(field: str, lo: int, hi: int,
                 names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip().upper()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start = _num(a, names)
            end = _num(b, names)
        else:
            start = end = _num(part, names)
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                values.add(v)
    return values


def cron_matches(expression: str, when: datetime) -> bool:
    """True when `when` (UTC) matches the 5-field cron expression."""
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expression!r}")
    minute, hour, dom, month, dow = fields
    if when.minute not in _parse_field(minute, 0, 59):
        return False
    if when.hour not in _parse_field(hour, 0, 23):
        return False
    if when.day not in _parse_field(dom, 1, 31):
        return False
    if when.month not in _parse_field(month, 1, 12, _MONTHS):
        return False
    # Python: Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
    cron_dow = (when.weekday() + 1) % 7
    if cron_dow not in _parse_field(dow, 0, 6, _WEEKDAYS):
        return False
    return True


def event_matches(trigger_expression: str, event_type: str,
                  payload: dict[str, Any],
                  payload_filter: dict[str, Any] | None = None) -> bool:
    if trigger_expression != event_type:
        return False
    for key, expected in (payload_filter or {}).items():
        if payload.get(key) != expected:
            return False
    return True


def next_due(expression: str, after: datetime, limit_days: int = 370) -> datetime | None:
    """Brute-force next matching minute after `after` (minute precision)."""
    candidate = after.replace(second=0, microsecond=0)
    for _ in range(limit_days * 24 * 60):
        candidate = candidate.replace(tzinfo=timezone.utc) if candidate.tzinfo is None else candidate
        from datetime import timedelta
        candidate = candidate + timedelta(minutes=1)
        if cron_matches(expression, candidate):
            return candidate
    return None
