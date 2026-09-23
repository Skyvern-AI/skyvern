from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core.skyvern_context import (
    ORG_AGE_BUCKET_ESTABLISHED,
    ORG_AGE_BUCKET_FIRST_DAY,
    ORG_AGE_BUCKET_FIRST_MONTH,
    ORG_AGE_BUCKET_FIRST_WEEK,
    ORG_AGE_BUCKET_UNKNOWN,
    compute_org_age_bucket,
)

_NOW = datetime.datetime(2026, 9, 21, 20, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    "age_days, expected",
    [
        (0, ORG_AGE_BUCKET_FIRST_DAY),
        (1, ORG_AGE_BUCKET_FIRST_WEEK),  # lower boundary of the first-week bucket
        (6, ORG_AGE_BUCKET_FIRST_WEEK),
        (7, ORG_AGE_BUCKET_FIRST_MONTH),  # lower boundary of the first-month bucket
        (29, ORG_AGE_BUCKET_FIRST_MONTH),
        (30, ORG_AGE_BUCKET_ESTABLISHED),  # lower boundary of the established bucket
        (400, ORG_AGE_BUCKET_ESTABLISHED),
    ],
)
def test_bucket_boundaries(age_days: int, expected: str) -> None:
    created_at = _NOW - datetime.timedelta(days=age_days)
    assert compute_org_age_bucket(created_at, now=_NOW) == expected


def test_partial_first_day_is_first_day() -> None:
    # 23 hours elapsed is < 1 whole day, so it stays in first_day rather than rounding up.
    created_at = _NOW - datetime.timedelta(hours=23)
    assert compute_org_age_bucket(created_at, now=_NOW) == ORG_AGE_BUCKET_FIRST_DAY


def test_future_created_at_clamps_to_first_day() -> None:
    # Clock skew can hand us a created_at after now; the non-negative clamp maps it to first_day.
    created_at = _NOW + datetime.timedelta(days=5)
    assert compute_org_age_bucket(created_at, now=_NOW) == ORG_AGE_BUCKET_FIRST_DAY


def test_missing_created_at_is_unknown() -> None:
    assert compute_org_age_bucket(None, now=_NOW) == ORG_AGE_BUCKET_UNKNOWN


@pytest.mark.parametrize(
    "created_at",
    [
        pytest.param("2026-09-21T20:00:00+00:00", id="string"),
        pytest.param(datetime.date(2026, 9, 21), id="date"),
        pytest.param(0, id="integer"),
        pytest.param(MagicMock(), id="magic-mock"),
        pytest.param(AsyncMock(), id="async-mock"),
    ],
)
def test_invalid_created_at_is_unknown(created_at: Any) -> None:
    assert compute_org_age_bucket(created_at, now=_NOW) == ORG_AGE_BUCKET_UNKNOWN


def test_naive_created_at_does_not_raise_against_aware_now() -> None:
    # The organizations table stores created_at as naive UTC (datetime.utcnow). The auth hot path
    # must bucket it without raising even though the reference clock is tz-aware.
    naive_created = datetime.datetime(2026, 9, 1, 0, 0, 0)
    assert compute_org_age_bucket(naive_created, now=_NOW) == ORG_AGE_BUCKET_FIRST_MONTH
