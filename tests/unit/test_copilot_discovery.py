"""Tests for Copilot build-time entrypoint discovery."""

from __future__ import annotations

from typing import Any

import pytest

from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.runtime import PendingBrowserInteractionObservation
from skyvern.forge.sdk.copilot.tools import _discovery_walk, _inspect_page_for_composition_impl
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence


class _Ctx:
    def __init__(self, server: object) -> None:
        self.discovery_mcp_server = server
        self.discovery_started_monotonic = None
        self.discovery_step_count = 0
        self.prior_page_inspection_calls_made = 0
        self.page_inspection_calls_this_turn = 0
        self.flow_evidence: list[dict[str, Any]] = []
        self.composition_page_evidence = None
        self.pending_browser_interaction_observation = None
        self.workflow_verification_evidence = WorkflowVerificationEvidence()


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


class _EmbeddedChallengeUsefulPageServer:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            if self.urls[-1] == "https://certboard.test":
                return {
                    "ok": True,
                    "data": {
                        "html": """
                        <html><head><title>Certification Board</title></head>
                        <body><a href="/registry/search">Find a Certificant</a></body></html>
                        """
                    },
                }
            return {
                "ok": True,
                "data": {
                    "html": """
                    <html>
                      <head>
                        <title>Certificant Registry</title>
                        <script src="https://challenges.example.test/turnstile/api.js"></script>
                      </head>
                      <body>
                        <form>
                          <label for="first-name">First Name</label>
                          <input id="first-name" name="first_name">
                          <label for="last-name">Last Name</label>
                          <input id="last-name" name="last_name">
                        </form>
                      </body>
                    </html>
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


class _TargetThenCurrentPageServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.current_url = ""

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        if tool_name == "skyvern_navigate":
            self.current_url = arguments["url"]
            return {"ok": True, "data": {"url": self.current_url}}
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
async def test_discovery_keeps_candidate_when_challenge_markup_is_embedded_in_useful_page() -> None:
    server = _EmbeddedChallengeUsefulPageServer()

    result = await _discovery_walk(
        _Ctx(server),
        entry_url="https://certboard.test",
        intent_hint="find certificant lookup page",
    )

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://certboard.test/registry/search"
    assert result["data"]["failure_reason"] is None
    assert result["data"]["candidate_form_fields"] == [
        {"label": "First Name", "name": "first_name", "type": "input", "value_hint": ""},
        {"label": "Last Name", "name": "last_name", "type": "input", "value_hint": ""},
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


@pytest.mark.asyncio
async def test_post_run_current_page_inspection_budget_bypass_does_not_consume_chat_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.prior_page_inspection_calls_made = 6
    ctx.page_inspection_calls_this_turn = 0
    ctx.last_run_blocks_workflow_run_id = "wr_123"  # type: ignore[attr-defined]
    ctx.last_test_ok = True  # type: ignore[attr-defined]
    ctx.composition_page_evidence = None  # type: ignore[attr-defined]

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["data"]["workflow_run_id"] == "wr_123"
    assert result["data"]["observed_after_workflow_run"] is True
    assert ctx.page_inspection_calls_this_turn == 0
    assert ctx.post_run_current_page_inspection_workflow_run_id == "wr_123"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_current_page_inspection_without_earned_interaction_is_not_click_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["reached_via"] == "current_page"
    assert ctx.flow_evidence[0]["reached_via"] == "current_page"


@pytest.mark.asyncio
async def test_current_page_inspection_after_browser_action_is_click_reached_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _CurrentPageServer()
    ctx = _Ctx(server)
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name="click",
        url="https://www.example.com/results",
    )

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module, "_fallback_page_info", fake_fallback_page_info)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["reached_via"] == "interaction"
    assert ctx.flow_evidence[0]["reached_via"] == "interaction"
    assert ctx.pending_browser_interaction_observation is None


@pytest.mark.asyncio
async def test_inspection_budget_steers_progress_check_instead_of_authoring() -> None:
    ctx = _Ctx(server=object())
    ctx.page_inspection_calls_this_turn = 999

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is False
    assert "not evidence that scouting is complete" in result["error"]
    assert "evaluate" in result["error"]
    assert "get_browser_screenshot" in result["error"]
    assert "browser action on the current page" in result["error"]
    assert "Do not author downstream result" in result["error"]
    assert "Compose from existing evidence" not in result["error"]


@pytest.mark.asyncio
async def test_target_url_inspection_clears_pending_interaction_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _TargetThenCurrentPageServer()
    ctx = _Ctx(server)
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name="click",
        url="https://www.example.com/results",
    )

    target_result = await _inspect_page_for_composition_impl(ctx, "https://www.example.com/results")

    assert target_result["ok"] is True
    assert target_result["reached_via"] == "navigate"
    assert ctx.pending_browser_interaction_observation is None

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://www.example.com/results", "Results"

    monkeypatch.setattr(tools_module, "_fallback_page_info", fake_fallback_page_info)

    current_result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert current_result["ok"] is True
    assert current_result["reached_via"] == "current_page"
    assert [entry["reached_via"] for entry in ctx.flow_evidence] == ["navigate", "current_page"]


class _SizeCappedHtmlStrippedFallbackServer:
    """Every page's get_html is dropped by the MCP size cap (a heavy DOM exceeds it).
    The stripped-body evaluate fallback recovers each page, so the resolver can still
    follow the intent anchor to the form and resolve an entrypoint instead of parsing
    empty pages and giving up."""

    def __init__(self) -> None:
        self.tools: list[str] = []
        self.urls: list[str] = []

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.tools.append(tool_name)
        if tool_name == "skyvern_navigate":
            self.urls.append(arguments["url"])
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            assert arguments == {"selector": "body"}
            return {"ok": True, "data": {"size_capped": True}}
        if tool_name == "skyvern_evaluate":
            if self.urls[-1] == "https://www.example.com":
                stripped = "<body><a href='/registry'>Find a Certificant</a></body>"
            else:
                stripped = "<body><form><input name='firstName'><button>Search</button></form></body>"
            return {"ok": True, "data": {"result": stripped}}
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.asyncio
async def test_discovery_recovers_entrypoint_when_get_html_is_size_capped() -> None:
    server = _SizeCappedHtmlStrippedFallbackServer()

    result = await _discovery_walk(_Ctx(server), entry_url="https://www.example.com", intent_hint="find a member")

    assert result["ok"] is True
    assert result["data"]["candidate_url"] == "https://www.example.com/registry"
    assert result["data"]["candidate_form_fields"] == [
        {"label": "", "name": "firstName", "type": "input", "value_hint": ""}
    ]
    assert "skyvern_evaluate" in server.tools


class _StrippedHtmlServer:
    """get_html is size-capped (dropped); the stripped-body evaluate returns a fixed body so
    the truncation flag can be exercised."""

    def __init__(self, stripped: str) -> None:
        self._stripped = stripped

    async def call_internal_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "skyvern_navigate":
            return {"ok": True, "data": {"url": arguments["url"]}}
        if tool_name == "skyvern_get_html":
            return {"ok": True, "data": {"size_capped": True}}
        if tool_name == "skyvern_evaluate":
            return {"ok": True, "data": {"result": self._stripped}}
        raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.asyncio
async def test_composition_get_html_flags_truncation_when_stripped_body_hits_cap() -> None:
    from skyvern.forge.sdk.copilot.tools import _COMPOSITION_STRIPPED_HTML_MAX_CHARS, _composition_get_html

    at_cap = "<body>" + "x" * _COMPOSITION_STRIPPED_HTML_MAX_CHARS
    _, error, truncated = await _composition_get_html(_Ctx(_StrippedHtmlServer(at_cap)))
    assert error is None
    assert truncated is True

    under_cap = "<body><form><input name='x'></form></body>"
    _, error, truncated = await _composition_get_html(_Ctx(_StrippedHtmlServer(under_cap)))
    assert error is None
    assert truncated is False


@pytest.mark.asyncio
async def test_capture_composition_evidence_warns_when_html_sliced_at_cap() -> None:
    from skyvern.forge.sdk.copilot.tools import _COMPOSITION_STRIPPED_HTML_MAX_CHARS, _capture_composition_evidence

    # A real form near the top yields bounded schema (no hollow-recapture loop); the trailing
    # padding pushes the stripped body past the cap so the fallback slice is detected as partial.
    body = (
        "<body><form><input name='firstName'><button>Search</button></form>"
        + "x" * _COMPOSITION_STRIPPED_HTML_MAX_CHARS
    )
    evidence, error = await _capture_composition_evidence(
        _Ctx(_StrippedHtmlServer(body)),
        inspected_url="https://www.example.com/search",
        current_url="https://www.example.com/search",
    )
    assert error is None
    assert evidence is not None
    assert "html_sliced_at_cap" in evidence["inspection_warnings"]
