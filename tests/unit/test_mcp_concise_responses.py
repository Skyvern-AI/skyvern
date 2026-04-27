"""Tests for the concise MCP response mode in make_result()."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

import skyvern.cli.mcp_tools.workflow as workflow_tools
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


def test_workflow_status_summary_stays_bounded_for_heavy_payload() -> None:
    long_url = "https://artifacts.skyvern.example/" + ("x" * 1450)
    heavy_run = {
        "workflow_run_id": "wr_heavy",
        "status": "terminated",
        "failure_reason": "Execution terminated after repeated navigation failures",
        "workflow_title": "Heavy workflow",
        "recording_url": long_url,
        "screenshot_urls": [f"{long_url}-{idx}" for idx in range(6)],
        "downloaded_files": [{"url": f"{long_url}-download", "filename": "case-export.csv"}],
        "outputs": {
            "collect_customer_data": {
                "task_screenshot_artifact_ids": [f"art_task_{idx}" for idx in range(12)],
                "workflow_screenshot_artifact_ids": [f"art_workflow_{idx}" for idx in range(12)],
                "task_screenshots": [f"{long_url}-task-{idx}" for idx in range(4)],
                "workflow_screenshots": [f"{long_url}-workflow-{idx}" for idx in range(4)],
                "extracted_information": [{"account_id": "acct_123", "status": "terminated"}],
            },
            "submit_case": [
                {
                    "task_screenshot_artifact_ids": [f"art_nested_{idx}" for idx in range(6)],
                    "workflow_screenshot_artifact_ids": [f"art_followup_{idx}" for idx in range(6)],
                    "task_screenshots": [f"{long_url}-nested-task-{idx}" for idx in range(3)],
                    "workflow_screenshots": [f"{long_url}-nested-workflow-{idx}" for idx in range(4)],
                }
            ],
            "extracted_information": [{"duplicated_rollup": True}],
        },
        "run_with": "code",
    }

    full_payload = workflow_tools._serialize_run_full(heavy_run)
    summary_payload = workflow_tools._serialize_run_summary(heavy_run)

    assert len(json.dumps(full_payload)) > 20_000
    assert len(json.dumps(summary_payload)) < 2_000

    result = make_result("skyvern_workflow_status", data=summary_payload)
    assert len(json.dumps(result)) < 2_200
    assert "recording_url" not in result["data"]
    assert "output" not in result["data"]
    assert result["data"]["artifact_summary"]["artifact_id_count"] == 36


def test_workflow_status_summary_shrinks_simple_payload() -> None:
    long_url = "https://artifacts.skyvern.example/" + ("y" * 1450)
    simple_run = {
        "workflow_run_id": "wr_simple",
        "status": "completed",
        "workflow_title": "Simple workflow",
        "recording_url": long_url,
        "screenshot_urls": [f"{long_url}-shot"],
        "outputs": {
            "result": "success",
            "order_id": "ord_123",
        },
        "run_with": "code",
    }

    full_payload = workflow_tools._serialize_run_full(simple_run)
    summary_payload = workflow_tools._serialize_run_summary(simple_run)

    assert len(json.dumps(full_payload)) > 1_500
    assert len(json.dumps(summary_payload)) < 800
    assert summary_payload["output_summary"]["scalar_preview"] == {"result": "success", "order_id": "ord_123"}
