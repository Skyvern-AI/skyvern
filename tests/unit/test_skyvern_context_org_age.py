from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core.skyvern_context import compute_org_age

_NOW = datetime.datetime(2026, 9, 21, 20, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (datetime.timedelta(0), 0),
        (datetime.timedelta(hours=23, minutes=59), 0),
        (datetime.timedelta(days=7), 7),
        (datetime.timedelta(days=7, hours=23), 7),
        (datetime.timedelta(days=8), 8),
        (datetime.timedelta(days=400), 400),
    ],
    ids=["created-now", "partial-first-day", "seven-days", "just-under-eight", "eight-days", "established"],
)
def test_age_is_whole_days_since_creation(elapsed: datetime.timedelta, expected: int) -> None:
    # The dashboard reads org_age <= 7 as a new org, so day 7 and day 8 must land on opposite sides.
    assert compute_org_age(_NOW - elapsed, now=_NOW) == expected


def test_future_created_at_clamps_to_zero() -> None:
    assert compute_org_age(_NOW + datetime.timedelta(days=5), now=_NOW) == 0


@pytest.mark.parametrize(
    "created_at, now",
    [
        pytest.param(datetime.datetime(2026, 9, 13, 20, 0, 0), _NOW, id="naive-created-aware-now"),
        pytest.param(_NOW - datetime.timedelta(days=8), _NOW.replace(tzinfo=None), id="aware-created-naive-now"),
        pytest.param(datetime.datetime(2026, 9, 13, 20, 0, 0), _NOW.replace(tzinfo=None), id="both-naive"),
    ],
)
def test_naive_timestamps_are_read_as_utc(created_at: datetime.datetime, now: datetime.datetime) -> None:
    # The organizations table stores created_at as naive UTC (datetime.utcnow) while the reference clock is aware.
    assert compute_org_age(created_at, now=now) == 8


@pytest.mark.parametrize(
    "created_at",
    [
        pytest.param(None, id="missing"),
        pytest.param("2026-09-21T20:00:00+00:00", id="string"),
        pytest.param(datetime.date(2026, 9, 21), id="date"),
        pytest.param(0, id="integer"),
        pytest.param(MagicMock(), id="magic-mock"),
        pytest.param(AsyncMock(), id="async-mock"),
    ],
)
def test_missing_or_invalid_created_at_has_no_age(created_at: Any) -> None:
    assert compute_org_age(created_at, now=_NOW) is None
