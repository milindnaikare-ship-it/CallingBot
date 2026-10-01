"""Time helpers.

Convention used across the codebase: **all datetimes stored in the database are naive UTC**
(SQLite drops tzinfo anyway). Convert to the business timezone (IST by default) only for
policy checks (calling windows) and display.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (the storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt_utc_naive: datetime, tz: str) -> datetime:
    """Naive-UTC -> aware datetime in ``tz``."""
    return dt_utc_naive.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))


def to_utc_naive(dt: datetime, tz: str | None = None) -> datetime:
    """Aware datetime (or naive datetime interpreted in ``tz``) -> naive UTC."""
    if dt.tzinfo is None:
        if tz is None:
            return dt
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC).replace(tzinfo=None)
