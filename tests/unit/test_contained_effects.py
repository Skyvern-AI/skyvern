import asyncio
from types import SimpleNamespace

import pytest

from skyvern.utils import contained_effects
from skyvern.utils.contained_effects import contained_effect


def _decide_with_effect(effect: object) -> str:
    """A caller that records something after it has already decided its outcome."""
    decision = "completed"
    with contained_effect("record the decision", run_id="r_1"):
        effect()
    return decision


def test_a_failing_effect_does_not_reach_the_caller() -> None:
    def effect() -> None:
        raise RuntimeError("sink is down")

    assert _decide_with_effect(effect) == "completed"


def test_a_failing_report_path_does_not_reach_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mechanism that defeats the obvious fix: the handler re-enters the sink that failed.

    A try/except around an emit is not containment when the except body logs through the
    same logger that just threw, so the report path has to be able to fail silently."""
    reports: list[str] = []

    def broken_warning(_event: str, **_fields: object) -> None:
        reports.append("attempted")
        raise RuntimeError("the logger itself is down")

    monkeypatch.setattr(contained_effects, "LOG", SimpleNamespace(warning=broken_warning))

    def effect() -> None:
        raise RuntimeError("sink is down")

    assert _decide_with_effect(effect) == "completed"
    assert reports == ["attempted"]


def test_cancellation_still_propagates() -> None:
    """Containment covers failure, not teardown: swallowing cancellation would hang the caller."""

    async def cancelled_effect() -> None:
        with contained_effect("record something"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_effect())
