"""E2E test for iframe MCP tools with a real browser.

Exercises the MCP tool chain (frame_list, frame_switch, frame_main) through
real Playwright + SessionState wiring, without requiring Skyvern's local
browser launcher infrastructure.

Skipped in CI when Playwright browsers are not installed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from mcp.types import Tool as MCPTool
from playwright.async_api import async_playwright

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState, get_current_session, set_current_session
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.browser import (
    skyvern_evaluate,
    skyvern_evaluate_and_screenshot,
    skyvern_execute,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_observe,
    skyvern_type,
)
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot.browser_ablation import CopilotToolSurface
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _has_playwright_browser() -> bool:
    """Check that Playwright's chromium binary exists for the current installed version."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

pytestmark = _skip_no_browser

MAIN_HTML = """\
<!DOCTYPE html>
<html>
<body>
  <h1 id="main-heading">Main Page</h1>
  <div id="main-only-sentinel">main-page</div>
  <input id="main-input" type="text" value="" />
  <button id="parent-action" type="button">Parent action</button>
  <iframe id="pay-frame" name="payment" srcdoc='
    <!DOCTYPE html>
    <html><body>
      <h2 id="frame-heading">Payment</h2>
      <div id="frame-only-sentinel">payment-frame</div>
      <input id="card" type="text" value="" placeholder="Card" />
      <button id="frame-action" type="button"
        onclick="document.getElementById(`frame-status`).textContent = `clicked`">
        Frame action
      </button>
      <div id="frame-status">idle</div>
      <iframe id="editor-frame" name="editor"></iframe>
      <script>
        var editorDoc = document.getElementById(`editor-frame`).contentDocument;
        editorDoc.body.innerHTML = `<div id="editor-only-sentinel">editor-frame</div>`
          + `<div id="editor-root" contenteditable="true">Edit me</div>`;
      </script>
    </body></html>
  '></iframe>
</body>
</html>
"""


class _FakeBrowserContext:
    """Minimal browser context to satisfy get_page() hooks from tab management."""

    def __init__(self, page: Any) -> None:
        self.pages = [page]

    def on(self, event: str, handler: Any) -> None:
        pass  # No-op for tests


class _FakeBrowser:
    """Minimal SkyvernBrowser substitute that wraps a real Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._browser_context = _FakeBrowserContext(page)

    async def get_working_page(self) -> Any:
        # The real page wrapper, so frame routing (_locator_scope) matches production.
        return SkyvernBrowserPage(MagicMock(), self._page)


class _LocalToolResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.structured_content = payload
        self.is_error = payload.get("ok", True) is not True
        self.content: list[Any] = []


class _LocalCopilotMCPClient:
    def __init__(self, tools: list[MCPTool]) -> None:
        self._tools = tools
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._dispatch = {
            "skyvern_frame_list": skyvern_frame_list,
            "skyvern_frame_switch": skyvern_frame_switch,
            "skyvern_frame_main": skyvern_frame_main,
            "skyvern_evaluate": skyvern_evaluate,
            "skyvern_type": skyvern_type,
        }

    async def list_tools(self) -> list[MCPTool]:
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> _LocalToolResult:
        self.calls.append((name, dict(args)))
        return _LocalToolResult(await self._dispatch[name](**args))


async def _copilot_payload(server: SkyvernOverlayMCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await server._call_tool(name, args)
    return json.loads(result.content[0].text)


@pytest_asyncio.fixture
async def mcp_session():
    """Set up a real Playwright browser and wire it into SessionState."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception:
            pytest.skip("Playwright chromium binary not available")
        context = await browser.new_context()
        pw_page = await context.new_page()
        await pw_page.set_content(MAIN_HTML)
        await asyncio.sleep(0.3)

        fake_browser = _FakeBrowser(pw_page)
        ctx = BrowserContext(mode="local")
        state = SessionState(browser=fake_browser, context=ctx)  # type: ignore[arg-type]
        set_current_session(state)

        yield state

        set_current_session(SessionState())
        await context.close()
        await browser.close()


# ---------------------------------------------------------------------------
# MCP tool e2e tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_frame_list_real_browser(mcp_session: SessionState) -> None:
    result = await skyvern_frame_list()
    assert result["ok"] is True
    frames = result["data"]["frames"]
    assert len(frames) >= 2
    names = [f["name"] for f in frames]
    assert "payment" in names
    assert result["data"]["count"] >= 2


@pytest.mark.asyncio
async def test_copilot_advertised_frame_tool_chain_real_browser(
    mcp_session: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _browser_scope(_ctx: AgentContext, *, session_id_override: str | None = None):
        yield

    async def _browser_available(_ctx: AgentContext) -> None:
        return None

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _browser_available)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _browser_scope)

    surface_names = (
        "skyvern_frame_list",
        "skyvern_frame_switch",
        "skyvern_frame_main",
        "evaluate",
        "type_text",
    )
    canonical_aliases = get_skyvern_mcp_alias_map()
    canonical_overlays = _build_skyvern_mcp_overlays()
    aliases = {name: canonical_aliases[name] for name in surface_names}
    overlays = {name: canonical_overlays[name] for name in surface_names}
    registered = await mcp.list_tools(run_middleware=False)
    raw_names = frozenset(aliases.values())
    client = _LocalCopilotMCPClient(
        [
            MCPTool(name=tool.name, description=tool.description, inputSchema=tool.parameters)
            for tool in registered
            if tool.name in raw_names
        ]
    )
    copilot_ctx = make_copilot_ctx(browser_session_id=None)
    server = SkyvernOverlayMCPServer(
        transport=object(),
        overlays=overlays,
        alias_map=aliases,
        allowlist=raw_names,
        ordered_allowlist=tuple(aliases.values()),
        enforce_dispatch_allowlist=True,
        context_provider=lambda: copilot_ctx,
    )
    server._client = client  # type: ignore[assignment]

    advertised = await server.list_tools()
    advertised_names = {tool.name for tool in advertised}
    assert set(surface_names) == advertised_names
    frame_contracts = [tool for tool in advertised if tool.name.startswith("skyvern_frame_")]
    assert all(tool.description for tool in frame_contracts)
    assert all("session_id" not in tool.inputSchema["properties"] for tool in frame_contracts)
    assert all("cdp_url" not in tool.inputSchema["properties"] for tool in frame_contracts)
    surface = CopilotToolSurface(
        native_tools=(),
        alias_map=aliases,
        overlays=overlays,
        ordered_native_names=(),
        ordered_mcp_names=surface_names,
    )
    without_frames = [tool for tool in advertised if not tool.name.startswith("skyvern_frame_")]
    old_surface = CopilotToolSurface(
        native_tools=(),
        alias_map={tool.name: aliases[tool.name] for tool in without_frames},
        overlays={tool.name: overlays[tool.name] for tool in without_frames},
        ordered_native_names=(),
        ordered_mcp_names=tuple(tool.name for tool in without_frames),
    )
    assert surface.advertised_sha256(advertised) != old_surface.advertised_sha256(without_frames)

    listed = await _copilot_payload(server, "skyvern_frame_list", {})
    assert listed["ok"] is True
    assert {"payment", "editor"} <= {frame["name"] for frame in listed["data"]["frames"]}

    host_sentinel = "document.querySelector('#main-only-sentinel')?.textContent ?? null"
    payment_sentinel = "document.querySelector('#frame-only-sentinel')?.textContent ?? null"
    editor_sentinel = "document.querySelector('#editor-only-sentinel')?.textContent ?? null"
    editor_text = "document.querySelector('#editor-root')?.textContent ?? null"

    host_from_main = await _copilot_payload(server, "evaluate", {"expression": host_sentinel})
    assert host_from_main["ok"] is True
    assert host_from_main["data"]["result"] == "main-page"

    entered = await _copilot_payload(server, "skyvern_frame_switch", {"selector": "#pay-frame"})
    assert entered["ok"] is True
    assert entered["data"]["frame_name"] == "payment"

    frame_before_evaluate = mcp_session._working_frame
    in_payment = await _copilot_payload(server, "evaluate", {"expression": payment_sentinel})
    assert in_payment["ok"] is True
    assert in_payment["data"]["result"] == "payment-frame"
    assert mcp_session._working_frame is frame_before_evaluate

    raw_in_payment = await skyvern_evaluate(expression=payment_sentinel)
    assert raw_in_payment["data"]["result"] == "payment-frame"
    assert "page.locator_scope.evaluate(" in raw_in_payment["data"]["sdk_equivalent"]

    host_from_payment = await _copilot_payload(server, "evaluate", {"expression": host_sentinel})
    assert host_from_payment["ok"] is True
    assert host_from_payment["data"]["result"] is None

    card_value = "document.querySelector('#card').value"
    card_before = await _copilot_payload(server, "evaluate", {"expression": card_value})
    card_typed = await _copilot_payload(server, "type_text", {"selector": "#card", "text": "4242"})
    card_after = await _copilot_payload(server, "evaluate", {"expression": card_value})
    assert card_before["data"]["result"] == ""
    assert card_typed["ok"] is True
    assert card_after["data"]["result"] == "4242"

    left_payment = await _copilot_payload(server, "skyvern_frame_main", {})
    assert left_payment["ok"] is True
    host_after_payment = await _copilot_payload(server, "evaluate", {"expression": host_sentinel})
    payment_after_main = await _copilot_payload(server, "evaluate", {"expression": payment_sentinel})
    assert host_after_payment["data"]["result"] == "main-page"
    assert payment_after_main["data"]["result"] is None

    nested = await _copilot_payload(server, "skyvern_frame_switch", {"name": "editor"})
    assert nested["ok"] is True
    assert nested["data"]["frame_name"] == "editor"

    in_editor = await _copilot_payload(server, "evaluate", {"expression": editor_sentinel})
    host_from_editor = await _copilot_payload(server, "evaluate", {"expression": host_sentinel})
    assert in_editor["data"]["result"] == "editor-frame"
    assert host_from_editor["data"]["result"] is None

    editor_before = await _copilot_payload(server, "evaluate", {"expression": editor_text})
    editor_typed = await _copilot_payload(server, "type_text", {"selector": "#editor-root", "text": "updated"})
    editor_after = await _copilot_payload(server, "evaluate", {"expression": editor_text})
    assert editor_before["data"]["result"] == "Edit me"
    assert editor_typed["ok"] is True, editor_typed
    assert editor_after["data"]["result"] == "updated"

    returned = await _copilot_payload(server, "skyvern_frame_main", {})
    assert returned["ok"] is True
    host_after_editor = await _copilot_payload(server, "evaluate", {"expression": host_sentinel})
    editor_after_main = await _copilot_payload(server, "evaluate", {"expression": editor_sentinel})
    assert host_after_editor["data"]["result"] == "main-page"
    assert editor_after_main["data"]["result"] is None

    continued = await _copilot_payload(server, "type_text", {"selector": "#main-input", "text": "continued"})
    main_value = await _copilot_payload(
        server, "evaluate", {"expression": "document.querySelector('#main-input').value"}
    )
    assert continued["ok"] is True
    assert main_value["data"]["result"] == "continued"

    invented_coordinate_count = sum("x" in args or "y" in args for _, args in client.calls)
    assert invented_coordinate_count == 0
    type_dispatches = [args for name, args in client.calls if name == "skyvern_type"]
    assert type_dispatches
    assert all(args["selector_mode"] == "direct" for args in type_dispatches)


@pytest.mark.asyncio
async def test_mcp_evaluate_and_screenshot_uses_working_frame(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(name="editor")

    result = await skyvern_evaluate_and_screenshot(
        expression="document.querySelector('#editor-only-sentinel')?.textContent ?? null",
        inline=True,
    )

    assert result["data"]["result"] == "editor-frame"


@pytest.mark.asyncio
async def test_mcp_frame_switch_by_selector(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(selector="#pay-frame")
    assert result["ok"] is True
    assert result["data"]["frame_name"] == "payment"
    assert result["data"]["switched_by"] == "selector"

    # Verify SessionState was updated
    assert mcp_session._working_frame is not None


@pytest.mark.asyncio
async def test_mcp_frame_switch_by_name(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(name="payment")
    assert result["ok"] is True
    assert result["data"]["switched_by"] == "name"
    assert mcp_session._working_frame is not None


@pytest.mark.asyncio
async def test_mcp_frame_main_clears_state(mcp_session: SessionState) -> None:
    # Switch in first
    await skyvern_frame_switch(selector="#pay-frame")
    assert mcp_session._working_frame is not None

    # Switch back
    result = await skyvern_frame_main()
    assert result["ok"] is True
    assert mcp_session._working_frame is None


@pytest.mark.asyncio
async def test_mcp_frame_switch_invalid_selector(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(selector="#nonexistent")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_mcp_frame_switch_persists_across_calls(mcp_session: SessionState) -> None:
    """Frame state set by frame_switch persists across subsequent get_page() calls."""
    # Switch into iframe
    await skyvern_frame_switch(selector="#pay-frame")

    # Simulate a subsequent MCP call — get_page() reads _working_frame from SessionState
    state = get_current_session()
    assert state._working_frame is not None

    # The next get_page() call would set page._working_frame from state._working_frame
    # Verify the state is there for the propagation
    frame = state._working_frame
    heading = await frame.locator("#frame-heading").text_content()
    assert heading == "Payment"


@pytest.mark.asyncio
async def test_mcp_observe_execute_ref_in_working_frame(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")

    observe_result = await skyvern_observe()

    assert observe_result["ok"] is True
    names = {element["name"] for element in observe_result["data"]["elements"]}
    assert "Frame action" in names
    assert "Parent action" not in names
    frame = mcp_session._working_frame
    assert frame is not None
    assert observe_result["data"]["url"] == frame.url
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")

    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is True
    assert await frame.locator("#frame-status").text_content() == "clicked"


@pytest.mark.asyncio
async def test_mcp_frame_main_invalidates_iframe_observe_ref(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")
    observe_result = await skyvern_observe()
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")
    frame = mcp_session._working_frame
    assert frame is not None

    await skyvern_frame_main()
    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is False
    assert "Unknown ref" in execute_result["data"]["results"][0]["error"]
    assert await frame.locator("#frame-status").text_content() == "idle"


@pytest.mark.asyncio
async def test_iframe_navigation_invalidates_observed_ref(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")
    observe_result = await skyvern_observe()
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")
    frame = mcp_session._working_frame
    assert frame is not None

    await frame.goto("data:text/html,<button id='replacement'>Replacement action</button>")
    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is False
    assert "Unknown ref" in execute_result["data"]["results"][0]["error"]
