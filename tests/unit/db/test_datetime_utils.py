from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc


def test_naive_utc_now_returns_naive_datetime() -> None:
    result = naive_utc_now()

    assert result.tzinfo is None


def test_to_naive_utc_preserves_naive_datetime() -> None:
    value = datetime(2026, 5, 27, 12, 0, 0)

    assert to_naive_utc(value) is value


def test_to_naive_utc_converts_aware_datetime_to_naive_utc() -> None:
    value = datetime(2026, 5, 27, 7, 30, 0, tzinfo=timezone(timedelta(hours=-7)))

    assert to_naive_utc(value) == datetime(2026, 5, 27, 14, 30, 0)


def test_to_naive_utc_preserves_none() -> None:
    assert to_naive_utc(None) is None
