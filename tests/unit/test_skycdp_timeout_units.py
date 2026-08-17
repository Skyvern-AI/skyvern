"""Timeouts crossing the facade boundary are milliseconds, as Playwright's are.

This is the least visible way an engine swap can go wrong. Every Skyvern call site passes
milliseconds -- `settings.BROWSER_ACTION_TIMEOUT_MS` is 5000 and 24 call sites in
`webeye/actions/handler.py` forward it -- so an engine that reads the number as seconds turns a
five-second action budget into eighty-three minutes. Nothing errors; the run simply hangs until some
outer timeout kills it, and the cause is invisible in the logs.
"""

from __future__ import annotations

import pytest

from skyvern.config import settings
from skyvern.webeye.skycdp.facade.timeouts import DEFAULT_ACTION_TIMEOUT_MS, seconds_from_ms


def test_a_millisecond_budget_becomes_the_right_number_of_seconds() -> None:
    assert seconds_from_ms(5000) == 5.0
    assert seconds_from_ms(250) == 0.25
    assert seconds_from_ms(0) == 0.0


def test_the_production_action_budget_is_five_seconds_not_five_thousand() -> None:
    """The exact value production passes, asserted end to end."""
    assert settings.BROWSER_ACTION_TIMEOUT_MS == 5000
    assert seconds_from_ms(settings.BROWSER_ACTION_TIMEOUT_MS) == 5.0


def test_an_omitted_timeout_falls_back_to_the_default_budget() -> None:
    assert seconds_from_ms(None) == DEFAULT_ACTION_TIMEOUT_MS / 1000


def test_the_default_matches_playwrights_thirty_seconds() -> None:
    assert DEFAULT_ACTION_TIMEOUT_MS == 30_000


def test_a_negative_budget_is_rejected_rather_than_waited_out() -> None:
    with pytest.raises(ValueError):
        seconds_from_ms(-1)


@pytest.mark.parametrize(
    "method",
    [
        "click",
        "fill",
        "press",
        "text_content",
        "input_value",
        "get_attribute",
        "is_checked",
        "select_option",
        "wait_for",
    ],
)
def test_locator_methods_take_milliseconds(method: str) -> None:
    """Any public method with a timeout must name it in milliseconds, so no call site converts."""
    import inspect

    from skyvern.webeye.skycdp.facade.locator import Locator

    signature = inspect.signature(getattr(Locator, method))
    parameter = signature.parameters.get("timeout")
    assert parameter is not None, f"Locator.{method} has no timeout parameter"
    assert parameter.default in (None, DEFAULT_ACTION_TIMEOUT_MS), (
        f"Locator.{method} defaults its timeout to {parameter.default!r}; "
        f"expected milliseconds (None or {DEFAULT_ACTION_TIMEOUT_MS})"
    )
