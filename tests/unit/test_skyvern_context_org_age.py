from __future__ import annotations

import datetime
from typing import Any

import pytest

from skyvern.forge.sdk.core.skyvern_context import compute_org_age

_NOW = datetime.datetime(2026, 9, 21, 20, 0, 0, tzinfo=datetime.timezone.utc)
_DAY = datetime.timedelta(days=1)


@pytest.mark.parametrize(
    "created_at, now, expected",
    [
        pytest.param(_NOW, _NOW, 0, id="created-now"),
        pytest.param(_NOW - datetime.timedelta(days=7, hours=23), _NOW, 7, id="just-under-eight"),
        pytest.param(_NOW - 8 * _DAY, _NOW, 8, id="eight-days"),
        pytest.param(_NOW + 5 * _DAY, _NOW, 0, id="future-clamps-to-zero"),
        pytest.param(datetime.datetime(2026, 9, 13, 20, 0, 0), _NOW, 8, id="naive-created-is-utc"),
        pytest.param(None, _NOW, None, id="missing"),
        pytest.param("2026-09-21T20:00:00+00:00", _NOW, None, id="string"),
    ],
)
def test_org_age_is_whole_utc_days_and_none_without_a_datetime(
    created_at: Any, now: datetime.datetime, expected: int | None
) -> None:
    # The dashboard reads org_age <= 7 as a new org, so day 7 and day 8 must land on opposite sides.
    assert compute_org_age(created_at, now=now) == expected
