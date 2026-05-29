"""Tests for Copilot build-time entrypoint discovery."""

from __future__ import annotations

from typing import Any

import pytest

from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.tools import _discovery_walk, _inspect_page_for_composition_impl


class _Ctx:
    def __init__(self, server: object) -> None:
        self.discovery_mcp_server = server
        self.discovery_started_monotonic = None
        self.discovery_step_count = 0
        self.prior_page_inspection_calls_made = 0
        self.page_inspection_calls_this_turn = 0


class _FailingNavigateServer:
    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == "skyvern_navigate"
        assert arguments == {"url": "https://www.example.com"}
        return {"ok": False, "error": "Failed to create browser session"}


class _InspectableNoCandidateServer:
    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {"ok": True, "data": {"html": "<html><body><p>Welcome</p></body></html>"}}
        raise AssertionError(f"unexpected tool: {tool_name}")


class _AnchorBeatsTitleServer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            if self.urls[-1] == "https://www.example.com":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Example Certification</title></head>
                        <body><a href="/registry">Find a Certificant</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html><head><title>Find a Member</title></head>
                    <body><form><input name="firstName"><button>Search</button></form></body></html>
                    """
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _DeepLinkAntiBotRecoveryServer:
    def __init__(self) -> None:
        self.navigated_urls: list[str] = []
        self.clicked_selectors: list[str] = []
        self.current_url = ""

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.current_url = arguments["url"]
            self.navigated_urls.append(self.current_url)
            return {"ok": True, "data": {"url": self.current_url}}
        if tool_name == "skyvern_click":
            self.clicked_selectors.append(arguments["selector"])
            self.current_url = "https://certboard.test/registry/search"
            return {"ok": True, "data": {"url": self.current_url}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            if self.current_url == "https://certboard.test/registry/search" and not self.clicked_selectors:
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Just a moment</title></head>
                        <body>Verify you are human before continuing.</body></html>
                        """
                    },
                }
            if self.current_url == "https://certboard.test/":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Example Certifications</title></head>
                        <body><a href="/find-a-member/">Find a Member</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html><head><title>Example Certification Registry</title></head>
                    <body><form><input name="first_name"><input name="last_name"><button>Search</button></form></body></html>
                    """
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


class _CurrentPageServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {
                "ok": True,
                "data": {
                    "html": "<html><body><form><input name='firstName'><button>Search</button></form></body></html>"
                },
            }
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.asyncio
async def test_discovery_navigation_failure_falls_back_to_entry_url() -> None:
    result = await _discovery_walk(
        _Ctx(_FailingNavigateServer()),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["error"] is None
    assert result["data"]["candidate_url"] == "https://www.example.com"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["confidence"] == 0.2
    assert result["data"]["evidence_trail"][0]["transition_reason"].startswith("navigate_failed:")


@pytest.mark.asyncio
async def test_discovery_successful_inspection_without_match_still_returns_no_candidate() -> None:
    result = await _discovery_walk(
        _Ctx(_InspectableNoCandidateServer()),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] is None
    assert result["data"]["failure_reason"] == "no_candidate"


@pytest.mark.asyncio
async def test_discovery_follows_stronger_intent_anchor_before_settling_on_broad_title() -> None:
    server = _AnchorBeatsTitleServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://www.example.com",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://www.example.com/registry"
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "firstName", "type": "input", "value_hint": ""}
    ]
    assert server.urls == ["https://www.example.com", "https://www.example.com/registry"]


@pytest.mark.asyncio
async def test_discovery_recovers_from_deep_link_anti_bot_by_clicking_from_origin() -> None:
    server = _DeepLinkAntiBotRecoveryServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://certboard.test/registry/search",
        intent_hint="find a member",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://certboard.test/registry/search"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "first_name", "type": "input", "value_hint": ""},
        {"label": "", "name": "last_name", "type": "input", "value_hint": ""},
    ]
    assert server.navigated_urls == [
        "https://certboard.test/registry/search",
        "https://certboard.test/",
    ]
    assert server.clicked_selectors == ['a[href="/find-a-member/"]']
    assert [item["transition_reason"] for item in result["data"]["evidence_trail"]] == [
        "direct_deep_link_anti_bot",
        "anchor_match",
        "anchor_match",
    ]


@pytest.mark.asyncio
async def test_inspect_current_page_uses_existing_browser_page(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.last_run_blocks_workflow_run_id = "wr_123"  # type: ignore[attr-defined]
    ctx.composition_page_evidence = None  # type: ignore[attr-defined]

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert server.calls == ["skyvern_get_html"]
    assert result["data"]["current_url"] == "https://www.example.com/results"
    assert result["data"]["workflow_run_id"] == "wr_123"
    assert result["data"]["observed_after_workflow_run"] is True
