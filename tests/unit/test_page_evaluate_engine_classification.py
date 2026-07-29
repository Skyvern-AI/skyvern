"""``SkyvernFrame`` evaluate / navigation-context-loss recovery must key on the per-run selected
browser engine's error family, not a hard-coded stock-Playwright ``except``. A run pinned to a
non-Playwright engine must recover from ITS native context-loss error and settle-noise, while a
foreign error (including another engine's error, or an unrelated exception) still propagates and
cancellation is never swallowed. With no engine pinned, the exact stock-Playwright behavior is
preserved.

These stay driver-agnostic: they pin fake engine selections and script the evaluate callable, so
they hold on an image shipping only stock Playwright.
"""

from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.utils.page import JS_FUNCTION_DEFS, SkyvernFrame, _wait_for_navigation_settle

_NAV_LOST = "Execution context was destroyed"


class _EngineAError(Exception):
    pass


class _EngineATimeout(_EngineAError):
    pass


async def _never_start():  # pragma: no cover - never awaited in these tests
    raise AssertionError("start_driver must not be called")


def _selection(name: str, error_type: type[BaseException], timeout_type: type[BaseException]) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=_never_start,
        error_type=error_type,
        timeout_error_type=timeout_type,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0"),
        selection_reason="test",
    )


class _SettleFrame:
    """Stands in for a Page/Frame during recovery: only ``wait_for_load_state`` is exercised."""

    def __init__(self, on_settle: BaseException | None = None) -> None:
        self.load_state_calls = 0
        self._on_settle = on_settle

    async def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        self.load_state_calls += 1
        if self._on_settle is not None:
            raise self._on_settle


class _ScriptedEval:
    def __init__(self, seq: list[object]) -> None:
        self.seq = list(seq)
        self.calls = 0

    async def __call__(self) -> object:
        item = self.seq[self.calls]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


async def _run_evaluate(
    seq: list[object],
    engine_selection: BrowserEngineSelection | None,
    *,
    frame: _SettleFrame | None = None,
    timeout_ms: float = 5000,
) -> tuple[object, _ScriptedEval]:
    scripted = _ScriptedEval(seq)
    result = await SkyvernFrame._evaluate_expression(
        frame=frame or _SettleFrame(),  # type: ignore[arg-type]
        expression=JS_FUNCTION_DEFS,  # bootstrap expression skips the re-injection pass
        evaluate_expression=scripted,
        timeout_ms=timeout_ms,
        engine_selection=engine_selection,
    )
    return result, scripted


# -- evaluate/navigation-context-loss recovery -------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_recovers_on_selected_engine_native_context_loss() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    result, scripted = await _run_evaluate([_EngineAError(_NAV_LOST), 42], sel)
    assert result == 42
    assert scripted.calls == 2


@pytest.mark.asyncio
async def test_evaluate_reraises_selected_engine_error_that_is_not_context_loss() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    with pytest.raises(_EngineAError):
        await _run_evaluate([_EngineAError("some unrelated boom")], sel)


@pytest.mark.asyncio
async def test_evaluate_propagates_foreign_error_even_when_message_is_context_loss() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    # A stock-Playwright error under a non-Playwright selection is foreign: it must propagate raw,
    # never be routed into engine-a's recovery, even though its message matches the recovery text.
    with pytest.raises(PlaywrightError):
        await _run_evaluate([PlaywrightError(_NAV_LOST)], sel)


@pytest.mark.asyncio
async def test_evaluate_retries_multiple_selected_engine_context_losses() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    result, scripted = await _run_evaluate(
        [_EngineAError(_NAV_LOST), _EngineAError("Cannot find context with specified id"), 99],
        sel,
    )
    assert result == 99
    assert scripted.calls == 3


@pytest.mark.asyncio
async def test_evaluate_recovers_on_stock_context_loss_when_selection_missing() -> None:
    result, scripted = await _run_evaluate([PlaywrightError(_NAV_LOST), 7], None)
    assert result == 7
    assert scripted.calls == 2


@pytest.mark.asyncio
async def test_evaluate_reraises_stock_error_that_is_not_context_loss() -> None:
    with pytest.raises(PlaywrightError):
        await _run_evaluate([PlaywrightError("boom")], None)


@pytest.mark.asyncio
async def test_evaluate_propagates_nonplaywright_error_under_stock_selection() -> None:
    with pytest.raises(_EngineAError):
        await _run_evaluate([_EngineAError(_NAV_LOST)], None)


@pytest.mark.asyncio
async def test_evaluate_recovers_on_runtime_error_context_loss_regardless_of_engine() -> None:
    for sel in (None, _selection("engine-a", _EngineAError, _EngineATimeout)):
        result, scripted = await _run_evaluate([RuntimeError(_NAV_LOST), 5], sel)
        assert result == 5
        assert scripted.calls == 2


@pytest.mark.asyncio
async def test_evaluate_maps_asyncio_timeout_to_page_analysis_timeout() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)

    async def slow() -> object:
        await asyncio.sleep(10)
        return None

    with pytest.raises(SkyvernPageAnalysisTimeout):
        await SkyvernFrame._evaluate_expression(
            frame=_SettleFrame(),  # type: ignore[arg-type]
            expression=JS_FUNCTION_DEFS,
            evaluate_expression=slow,
            timeout_ms=10,
            engine_selection=sel,
        )


# -- navigation settle noise-tolerance ---------------------------------------------------------


@pytest.mark.asyncio
async def test_navigation_settle_swallows_selected_engine_error() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    frame = _SettleFrame(on_settle=_EngineAError("still navigating"))
    await _wait_for_navigation_settle(frame, timeout_ms=100, engine_selection=sel)  # type: ignore[arg-type]
    assert frame.load_state_calls == 1


@pytest.mark.asyncio
async def test_navigation_settle_propagates_foreign_error() -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    frame = _SettleFrame(on_settle=PlaywrightError("still navigating"))
    with pytest.raises(PlaywrightError):
        await _wait_for_navigation_settle(frame, timeout_ms=100, engine_selection=sel)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_navigation_settle_swallows_stock_playwright_error_when_selection_missing() -> None:
    frame = _SettleFrame(on_settle=PlaywrightError("still navigating"))
    await _wait_for_navigation_settle(frame, timeout_ms=100, engine_selection=None)  # type: ignore[arg-type]
    assert frame.load_state_calls == 1


@pytest.mark.asyncio
async def test_navigation_settle_propagates_nonplaywright_error_under_stock() -> None:
    frame = _SettleFrame(on_settle=_EngineAError("still navigating"))
    with pytest.raises(_EngineAError):
        await _wait_for_navigation_settle(frame, timeout_ms=100, engine_selection=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_navigation_settle_propagates_cancellation() -> None:
    frame = _SettleFrame(on_settle=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _wait_for_navigation_settle(frame, timeout_ms=100, engine_selection=None)  # type: ignore[arg-type]


# -- instance threading ------------------------------------------------------------------------


def test_skyvern_frame_stores_engine_selection_default_none() -> None:
    assert SkyvernFrame(frame=object()).engine_selection is None  # type: ignore[arg-type]
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    assert SkyvernFrame(frame=object(), engine_selection=sel).engine_selection is sel  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_instance_evaluate_forwards_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    captured: dict[str, object] = {}

    async def fake_eval_expr(
        *,
        frame: object,
        expression: str,
        evaluate_expression: object,
        timeout_ms: float,
        engine_selection: object = None,
    ) -> str:
        captured["engine_selection"] = engine_selection
        return "ok"

    monkeypatch.setattr(SkyvernFrame, "_evaluate_expression", staticmethod(fake_eval_expr))
    sf = SkyvernFrame(frame=object(), engine_selection=sel)  # type: ignore[arg-type]
    result = await sf.get_scroll_x_y()
    assert result == "ok"
    assert captured["engine_selection"] is sel


@pytest.mark.asyncio
async def test_instance_evaluate_expression_forwards_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _selection("engine-a", _EngineAError, _EngineATimeout)
    captured: dict[str, object] = {}

    async def fake_eval_expr(
        *,
        frame: object,
        expression: str,
        evaluate_expression: object,
        timeout_ms: float,
        engine_selection: object = None,
    ) -> bool:
        captured["engine_selection"] = engine_selection
        return True

    monkeypatch.setattr(SkyvernFrame, "_evaluate_expression", staticmethod(fake_eval_expr))

    class _Loc:
        async def count(self) -> int:
            return 1

        async def evaluate(self, _js: str) -> bool:
            return True

    sf = SkyvernFrame(frame=object(), engine_selection=sel)  # type: ignore[arg-type]
    result = await sf.get_element_visible(_Loc())  # type: ignore[arg-type]
    assert result is True
    assert captured["engine_selection"] is sel
