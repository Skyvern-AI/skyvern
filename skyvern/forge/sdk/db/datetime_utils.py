"""Normalize repository timestamps for DB columns that store naive UTC.

SQL staleness checks should match the column representation here. API and UI
layers can convert UTC values to aware or local-time representations at the edge.
"""

from __future__ import annotations

from datetime import datetime, timezone


def naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
