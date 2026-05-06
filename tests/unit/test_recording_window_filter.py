from datetime import UTC, datetime, timedelta, timezone

import pytest

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.service import _as_utc, _select_recording_urls_in_window


def _rec(url: str, modified_at: datetime | None) -> FileInfo:
    return FileInfo(url=url, checksum=None, filename=None, modified_at=modified_at)


@pytest.mark.parametrize(
    "dt,expected",
    [
        (datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        (datetime(2026, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        (
            datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=-4))),
            datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
    ],
)
def test_as_utc_normalizes(dt: datetime, expected: datetime) -> None:
    assert _as_utc(dt) == expected


def test_select_empty_list_returns_empty() -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    assert _select_recording_urls_in_window([], lower, upper) == []


def test_select_drops_undated() -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    recs = [
        _rec("undated", None),
        _rec("in", datetime(2026, 1, 1, 10, 30, tzinfo=UTC)),
    ]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["in"]


def test_select_sorts_oldest_first() -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    recs = [
        _rec("new", datetime(2026, 1, 1, 11, 30, tzinfo=UTC)),
        _rec("old", datetime(2026, 1, 1, 10, 15, tzinfo=UTC)),
        _rec("mid", datetime(2026, 1, 1, 10, 45, tzinfo=UTC)),
    ]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["old", "mid", "new"]


def test_select_excludes_out_of_window() -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    recs = [
        _rec("before", datetime(2026, 1, 1, 9, 30, tzinfo=UTC)),
        _rec("after", datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        _rec("in", datetime(2026, 1, 1, 10, 30, tzinfo=UTC)),
    ]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["in"]


@pytest.mark.parametrize("boundary", ["lower", "upper"])
def test_select_includes_exact_boundary_match(boundary: str) -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    ts = lower if boundary == "lower" else upper
    recs = [_rec("edge", ts)]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["edge"]


def test_select_handles_naive_modified_at() -> None:
    # FileInfo.modified_at can still be naive from legacy code; _as_utc must normalize.
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    recs = [_rec("naive", datetime(2026, 1, 1, 10, 30))]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["naive"]


def test_select_handles_non_utc_aware_modified_at() -> None:
    lower = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    upper = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    est = timezone(timedelta(hours=-5))
    # 2026-01-01 05:30 EST == 10:30 UTC
    recs = [_rec("est", datetime(2026, 1, 1, 5, 30, tzinfo=est))]
    assert _select_recording_urls_in_window(recs, lower, upper) == ["est"]
