"""Truthful tool-result transport for a stale-target batch stop.

When a batched action is not executed because its target went stale, the tool caller (Yutori
Navigator) must be told the action did NOT run and the page must be re-observed -- never a generic
"executed" / "Clicked 1x" description that would make the next planner believe the click happened.
"""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.api.llm.yutori_navigator_llm_caller import (
    YutoriNavigatorLLMCaller,
    derive_navigator_pending_result,
)
from skyvern.webeye.actions.actions import ClickAction, WaitAction
from skyvern.webeye.actions.responses import (
    STALE_TARGET_TOOL_RESULT,
    ActionFailure,
    ActionSuccess,
    StaleActionAbort,
)


def _click() -> ClickAction:
    return ClickAction(element_id="AAA")


def test_stale_abort_maps_to_explicit_not_executed_result() -> None:
    result_str = derive_navigator_pending_result(_click(), StaleActionAbort())
    assert result_str == STALE_TARGET_TOOL_RESULT
    assert "not executed" in result_str.lower()
    assert "re-observe" in result_str.lower()
    # Never a generic executed description.
    assert "Clicked" not in result_str


def test_non_stale_derivations_are_unchanged() -> None:
    assert derive_navigator_pending_result(_click(), ActionSuccess(data={"x": 1})) == str({"x": 1})
    # A browser action with no explicit data still yields None so the flush uses its generic description.
    assert derive_navigator_pending_result(_click(), ActionSuccess()) is None
    assert derive_navigator_pending_result(_click(), ActionFailure(Exception("boom"))).startswith("ERROR:")
    assert derive_navigator_pending_result(WaitAction(seconds=3), ActionFailure(Exception("x"))) == "Waited 3s"


def _caller_with_pending(result: str | None) -> YutoriNavigatorLLMCaller:
    caller = object.__new__(YutoriNavigatorLLMCaller)
    caller.message_history = []
    caller._pending_tool_calls = [{"id": "tc1", "name": "left_click", "arguments": "{}", "result": result}]
    return caller


def _flushed_text(caller: YutoriNavigatorLLMCaller) -> str:
    content = caller.message_history[-1]["content"]
    return content[0]["text"] if isinstance(content, list) else content


def test_flush_uses_explicit_stale_result_not_clicked_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.forge.sdk.api.llm.yutori_navigator_llm_caller as yutori_module

    monkeypatch.setattr(yutori_module, "screenshot_to_data_url", lambda _b: "data:image/png;base64,Zm8=")
    caller = _caller_with_pending(STALE_TARGET_TOOL_RESULT)
    caller.flush_pending_tool_results(b"x", "https://example.test/x")

    text = _flushed_text(caller)
    assert STALE_TARGET_TOOL_RESULT in text
    assert "Clicked 1x" not in text


def test_flush_without_result_still_falls_back_to_clicked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contrast / guard: a None pending result (the pre-fix stale shape) is exactly what produced the
    # false "Clicked 1x with left" -- so the derivation must never return None for a stale abort.
    import skyvern.forge.sdk.api.llm.yutori_navigator_llm_caller as yutori_module

    monkeypatch.setattr(yutori_module, "screenshot_to_data_url", lambda _b: "data:image/png;base64,Zm8=")
    caller = _caller_with_pending(None)
    caller.flush_pending_tool_results(b"x", "https://example.test/x")

    assert "Clicked 1x with left" in _flushed_text(caller)
