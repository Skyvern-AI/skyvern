"""Tests for the concise MCP response mode in make_result()."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skyvern.cli.core.result import Artifact, BrowserContext, make_result, set_concise_responses


@pytest.fixture(autouse=True)
def _enable_concise() -> Iterator[None]:
    """Enable concise mode for every test; restore after."""
    set_concise_responses(True)
    yield
    set_concise_responses(False)


# -- Helpers ------------------------------------------------------------------

_CTX = BrowserContext(mode="cdp", session_id="pbs_1", cdp_url="wss://example.com/devtools")

_CLICK_DATA = {
    "selector": "#btn",
    "intent": "the Submit button",
    "ai_mode": "proactive",
    "resolved_selector": "xpath=/*[name()='html'][1]/*[name()='body'][1]/*[name()='button'][1]",
    "sdk_equivalent": 'await page.click("xpath=...", prompt="the Submit button")',
}


# -- Stripped fields ----------------------------------------------------------


def test_concise_strips_action_and_browser_context() -> None:
    result = make_result("skyvern_click", browser_context=_CTX, data=_CLICK_DATA)
    assert "action" not in result
    assert "browser_context" not in result


def test_concise_strips_timing() -> None:
    result = make_result("skyvern_click", data=_CLICK_DATA, timing_ms={"sdk": 500, "total": 500})
    assert "timing_ms" not in result


@pytest.mark.parametrize("key", ["sdk_equivalent", "ai_mode", "selector", "intent"])
def test_concise_strips_debug_data_keys(key: str) -> None:
    data = {key: "some_value", "url": "https://example.com"}
    result = make_result("skyvern_navigate", data=data)
    assert key not in result.get("data", {})


def test_concise_strips_none_values_from_data() -> None:
    data = {"url": "https://example.com", "title": None}
    result = make_result("skyvern_navigate", data=data)
    assert "title" not in result.get("data", {})


def test_concise_omits_data_when_all_keys_stripped() -> None:
    """When every key in data is strippable, the data key should be omitted entirely."""
    data = {"sdk_equivalent": "await page.click(...)", "ai_mode": "proactive", "selector": "#x", "intent": "foo"}
    result = make_result("skyvern_click", data=data)
    assert "data" not in result


# -- Minimal response --------------------------------------------------------


def test_concise_minimal_response() -> None:
    """No data, no error, no artifacts — should return just {"ok": True}."""
    result = make_result("skyvern_click")
    assert result == {"ok": True}


# -- Omitted empty collections -----------------------------------------------


def test_concise_omits_empty_artifacts() -> None:
    result = make_result("skyvern_click", data=_CLICK_DATA, artifacts=[])
    assert "artifacts" not in result


def test_concise_omits_empty_warnings() -> None:
    result = make_result("skyvern_click", data=_CLICK_DATA, warnings=[])
    assert "warnings" not in result


def test_concise_omits_null_error() -> None:
    result = make_result("skyvern_click", data=_CLICK_DATA, error=None)
    assert "error" not in result


# -- Preserved fields ---------------------------------------------------------


def test_concise_click_preserves_resolved_selector() -> None:
    """resolved_selector is actionable feedback — shows what the AI resolver matched."""
    result = make_result("skyvern_click", data=_CLICK_DATA)
    assert result["data"]["resolved_selector"] == _CLICK_DATA["resolved_selector"]


def test_concise_click_strips_other_echoed_fields() -> None:
    result = make_result("skyvern_click", data=_CLICK_DATA)
    data = result.get("data", {})
    assert "selector" not in data
    assert "intent" not in data
    assert "ai_mode" not in data
    assert "sdk_equivalent" not in data


def test_concise_preserves_meaningful_data() -> None:
    data = {"extracted": {"price": 42.0}, "sdk_equivalent": "await page.extract(...)"}
    result = make_result("skyvern_extract", data=data)
    assert result["data"] == {"extracted": {"price": 42.0}}


def test_concise_preserves_error() -> None:
    err = {"code": "SELECTOR_NOT_FOUND", "message": "Not found", "hint": "Try another selector"}
    result = make_result("skyvern_click", ok=False, error=err)
    assert result["ok"] is False
    assert result["error"] == err


def test_concise_preserves_nonempty_warnings() -> None:
    result = make_result("skyvern_click", ok=False, warnings=["Element hidden"])
    assert result["warnings"] == ["Element hidden"]


def test_concise_preserves_nonempty_artifacts() -> None:
    artifact = Artifact(kind="screenshot", path="/tmp/shot.png", mime="image/png", bytes=1024)
    result = make_result("skyvern_screenshot", artifacts=[artifact])
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["path"] == "/tmp/shot.png"


# -- Partial failure with data ------------------------------------------------


def test_concise_preserves_data_on_failure() -> None:
    err = {"code": "TIMEOUT", "message": "Timed out", "hint": "Increase timeout"}
    data = {"partial_result": {"items": 3}, "sdk_equivalent": "await page.extract(...)"}
    result = make_result("skyvern_extract", ok=False, error=err, data=data)
    assert result["ok"] is False
    assert result["error"] == err
    assert result["data"] == {"partial_result": {"items": 3}}


# -- None-preserving keys (result, extracted) ---------------------------------


def test_concise_preserves_none_result_for_evaluate() -> None:
    """JS returning null is a meaningful answer — must not be stripped."""
    data = {"result": None, "sdk_equivalent": "await page.evaluate(...)"}
    result = make_result("skyvern_evaluate", data=data)
    assert "data" in result
    assert result["data"]["result"] is None


def test_concise_preserves_none_extracted() -> None:
    """Extraction returning None means 'found nothing' — must not be stripped."""
    data = {"extracted": None, "sdk_equivalent": "await page.extract(...)"}
    result = make_result("skyvern_extract", data=data)
    assert "data" in result
    assert result["data"]["extracted"] is None


# -- Verbose mode (flag off) --------------------------------------------------


def test_verbose_returns_all_fields() -> None:
    set_concise_responses(False)
    result = make_result(
        "skyvern_click",
        browser_context=_CTX,
        data=_CLICK_DATA,
        timing_ms={"sdk": 500, "total": 500},
    )
    assert result["action"] == "skyvern_click"
    assert "browser_context" in result
    assert result["timing_ms"] == {"sdk": 500, "total": 500}
    assert result["data"]["sdk_equivalent"] is not None
    assert result["data"]["resolved_selector"] is not None
    assert result["artifacts"] == []
    assert result["warnings"] == []
