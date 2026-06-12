"""The task_v2 planner-iteration budget must honor the configured override."""

from __future__ import annotations

import pytest

from skyvern.services.task_v2_service import DEFAULT_MAX_ITERATIONS, _resolve_max_iterations


def test_no_override_uses_default() -> None:
    assert _resolve_max_iterations(None) == DEFAULT_MAX_ITERATIONS


@pytest.mark.parametrize("override", [100, "100"])
def test_explicit_override_above_default_is_honored(override: int | str) -> None:
    assert _resolve_max_iterations(override) == 100


@pytest.mark.parametrize("override", [10, "10", 1, "49"])
def test_override_below_floor_is_clamped_to_default(override: int | str) -> None:
    # DEFAULT_MAX_ITERATIONS is a floor, not just a fallback: an override below it clamps up,
    # never down — a value smaller than the planner's standing budget must not shrink it.
    assert _resolve_max_iterations(override) == DEFAULT_MAX_ITERATIONS


@pytest.mark.parametrize("override", [0, "", "abc", None])
def test_falsy_or_garbage_falls_back_to_default(override: object) -> None:
    assert _resolve_max_iterations(override) == DEFAULT_MAX_ITERATIONS
