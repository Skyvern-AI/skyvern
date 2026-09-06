"""E2E test for iframe MCP tools with a real browser.

Exercises the MCP tool chain (frame_list, frame_switch, frame_main) through
real Playwright + SessionState wiring, without requiring Skyvern's local
browser launcher infrastructure.

Skipped in CI when Playwright browsers are not installed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import structlog
from mcp.types import Tool as MCPTool
from playwright.async_api import BrowserContext as PlaywrightBrowserContext
from playwright.async_api import Frame, Page, Route, async_playwright

from skyvern.cli.core.browser_ops import get_observe_document_id
from skyvern.cli.core.guards import STALE_FRAME_HINT
from skyvern.cli.core.result import BrowserContext, ErrorCode
from skyvern.cli.core.session_manager import SessionState, get_current_session, get_page, set_current_session
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.browser import (
    skyvern_click,
    skyvern_drag,
    skyvern_evaluate,
    skyvern_evaluate_and_screenshot,
    skyvern_execute,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_observe,
    skyvern_press_key,
    skyvern_scroll,
    skyvern_select_option,
    skyvern_type,
)
from skyvern.exceptions import StaleFrameSelectionError
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot.browser_ablation import CopilotToolSurface
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage
from skyvern.library.skyvern_browser_page_ai import SdkSkyvernPageAi
from tests.unit.copilot_test_helpers import make_copilot_ctx

LOG = structlog.get_logger()


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
  <select id="ship"><option value="">Pick</option><option value="ground">Ground</option></select>
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


POPUP_URL = "https://popup.example.com/"

POPUP_HTML = """\
<!DOCTYPE html>
<html>
<body>
  <div id="main-only-sentinel">popup-page</div>
  <input id="card" type="text" value="" />
</body>
</html>
"""


class _FakeBrowserContext:
    """Minimal browser context to satisfy get_page() hooks from tab management."""

    def __init__(self, context: PlaywrightBrowserContext) -> None:
        self._context = context

    @property
    def pages(self) -> list[Page]:
        return list(self._context.pages)

    def on(self, event: str, handler: Any) -> None:
        pass  # No-op for tests


class _FakeBrowser:
    """Minimal SkyvernBrowser substitute over a real Playwright context, selecting pages[-1] like
    SkyvernBrowser.get_working_page. real_browser_state.py:362-380 pins an explicitly selected page
    only while every open page is known and otherwise returns the newest, which is the same page
    here because no tab is ever selected."""

    def __init__(self, context: PlaywrightBrowserContext) -> None:
        self._context = context
        self._browser_context = _FakeBrowserContext(context)

    async def get_working_page(self) -> SkyvernBrowserPage:
        return SkyvernBrowserPage(MagicMock(), self._context.pages[-1])


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

        fake_browser = _FakeBrowser(context)
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


async def _select_frame_then_steal_focus(state: SessionState) -> tuple[Frame, Page]:
    """Select the payment iframe, then let the page open a popup that takes focus on its own."""
    switched = await skyvern_frame_switch(selector="#pay-frame")
    assert switched["ok"] is True
    leftover_frame = state._working_frame
    assert leftover_frame is not None

    context = leftover_frame.page.context
    origin = context.pages[0]

    async def _serve(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=POPUP_HTML)

    await context.route("**/*", _serve)
    async with context.expect_page() as popup_info:
        await origin.evaluate(f"window.open({POPUP_URL!r}, '_blank')")
    popup = await popup_info.value
    await popup.wait_for_load_state()

    assert len(context.pages) == 2
    assert context.pages[-1] is popup
    assert leftover_frame.is_detached() is False
    LOG.info(
        "POPUP_GATE",
        open_pages=len(context.pages),
        newest_is_popup=context.pages[-1] is popup,
        leftover_is_detached=leftover_frame.is_detached(),
    )
    return leftover_frame, popup


@pytest.mark.asyncio
async def test_popup_focus_refuses_stale_frame_read(mcp_session: SessionState) -> None:
    leftover_frame, popup = await _select_frame_then_steal_focus(mcp_session)

    result = await skyvern_evaluate(expression="document.querySelector('#frame-only-sentinel')?.textContent ?? null")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
    assert result["error"]["hint"] == STALE_FRAME_HINT
    assert "Stale frame selection" in result["error"]["message"]
    assert "payment-frame" not in json.dumps(result)
    assert await leftover_frame.evaluate("document.querySelector('#frame-only-sentinel').textContent") == (
        "payment-frame"
    )
    assert await popup.evaluate("document.querySelector('#main-only-sentinel').textContent") == "popup-page"


@pytest.mark.asyncio
async def test_popup_focus_refuses_stale_frame_write(mcp_session: SessionState) -> None:
    leftover_frame, popup = await _select_frame_then_steal_focus(mcp_session)

    result = await skyvern_type(selector="#card", text="4242", selector_mode="direct")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
    assert result["error"]["hint"] == STALE_FRAME_HINT
    assert "Stale frame selection" in result["error"]["message"]
    assert await leftover_frame.locator("#card").input_value() == ""
    assert await popup.locator("#card").input_value() == ""


@pytest.mark.parametrize(
    "page_space_action",
    [
        lambda x, y: skyvern_click(x=x, y=y),
        lambda x, y: skyvern_type(x=x, y=y, text="4242"),
        lambda x, y: skyvern_press_key(key="4"),
    ],
    ids=["click_at", "type_at", "press_key"],
)
@pytest.mark.asyncio
async def test_popup_focus_refuses_stale_frame_page_space_action(
    mcp_session: SessionState,
    page_space_action: Callable[[float, float], Awaitable[dict[str, Any]]],
) -> None:
    leftover_frame, popup = await _select_frame_then_steal_focus(mcp_session)
    box = await popup.locator("#card").bounding_box()
    assert box is not None

    result = await page_space_action(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
    assert result["error"]["hint"] == STALE_FRAME_HINT
    assert "Stale frame selection" in result["error"]["message"]
    assert await popup.locator("#card").input_value() == ""
    assert await leftover_frame.locator("#card").input_value() == ""


@pytest.mark.parametrize(
    "page_space_action",
    [
        lambda x, y: skyvern_click(x=x, y=y),
        lambda x, y: skyvern_type(x=x, y=y, text="4242"),
        lambda x, y: skyvern_press_key(key="4"),
    ],
    ids=["click_at", "type_at", "press_key"],
)
@pytest.mark.asyncio
async def test_owned_frame_keeps_page_space_actions_working(
    mcp_session: SessionState,
    page_space_action: Callable[[float, float], Awaitable[dict[str, Any]]],
) -> None:
    switched = await skyvern_frame_switch(selector="#pay-frame")
    assert switched["ok"] is True
    frame = mcp_session._working_frame
    assert frame is not None
    box = await frame.locator("#card").bounding_box()
    assert box is not None

    result = await page_space_action(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    assert result["ok"] is True, result.get("error")


_SELECTOR_PAGE_SPACE_ACTIONS = [
    lambda: skyvern_drag(source_selector="#main-input", target_selector="#parent-action"),
    lambda: skyvern_scroll(direction="down"),
    lambda: skyvern_select_option(selector="#ship", value="Ground", by_label=True, selector_mode="direct"),
    lambda: skyvern_click(selector="#ship option[value='ground']", selector_mode="direct"),
]
_SELECTOR_PAGE_SPACE_IDS = ["drag", "scroll", "select_by_label", "native_option"]


@pytest.mark.parametrize("action", _SELECTOR_PAGE_SPACE_ACTIONS, ids=_SELECTOR_PAGE_SPACE_IDS)
@pytest.mark.asyncio
async def test_popup_focus_refuses_stale_frame_selector_page_space_action(
    mcp_session: SessionState,
    action: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    await _select_frame_then_steal_focus(mcp_session)

    result = await action()

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
    assert result["error"]["hint"] == STALE_FRAME_HINT


@pytest.mark.parametrize("action", _SELECTOR_PAGE_SPACE_ACTIONS, ids=_SELECTOR_PAGE_SPACE_IDS)
@pytest.mark.asyncio
async def test_owned_frame_keeps_selector_page_space_actions_in_the_top_document(
    mcp_session: SessionState,
    action: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    switched = await skyvern_frame_switch(selector="#pay-frame")
    assert switched["ok"] is True
    frame = mcp_session._working_frame
    assert frame is not None
    assert await frame.locator("#main-input").count() == 0
    assert await frame.locator("#ship").count() == 0

    result = await action()

    assert result["ok"] is True, result.get("error")


@pytest.mark.parametrize(
    "retarget",
    [
        lambda: skyvern_type(selector="#card", text="4242", intent="the card number field"),
        lambda: skyvern_press_key(key="4", selector="#card", intent="the card number field"),
    ],
    ids=["fill_ai_fallback", "ai_locator"],
)
@pytest.mark.asyncio
async def test_popup_focus_refusal_is_not_retargeted_to_ai(
    mcp_session: SessionState,
    monkeypatch: pytest.MonkeyPatch,
    retarget: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    leftover_frame, popup = await _select_frame_then_steal_focus(mcp_session)
    ai_calls: list[str] = []

    async def _record_ai_input_text(self: SdkSkyvernPageAi, *args: Any, **kwargs: Any) -> str:
        ai_calls.append("ai_input_text")
        return "4242"

    async def _record_ai_locate_element(self: SdkSkyvernPageAi, *args: Any, **kwargs: Any) -> str:
        ai_calls.append("ai_locate_element")
        return "//input[@id='card']"

    monkeypatch.setattr(SdkSkyvernPageAi, "ai_input_text", _record_ai_input_text)
    monkeypatch.setattr(SdkSkyvernPageAi, "ai_locate_element", _record_ai_locate_element)

    result = await retarget()

    assert ai_calls == []
    assert result["ok"] is False
    assert "Stale frame selection" in result["error"]["message"]
    assert await leftover_frame.locator("#card").input_value() == ""
    assert await popup.locator("#card").input_value() == ""


@pytest.mark.asyncio
async def test_popup_focus_refuses_stale_observe_document_id(mcp_session: SessionState) -> None:
    switched = await skyvern_frame_switch(selector="#pay-frame")
    assert switched["ok"] is True
    owned_page, _ = await get_page()
    selected_document_id = await get_observe_document_id(owned_page)
    assert selected_document_id is not None

    await _select_frame_then_steal_focus(mcp_session)

    stale_page, _ = await get_page()
    with pytest.raises(StaleFrameSelectionError):
        await get_observe_document_id(stale_page)

    await skyvern_frame_main()
    live_page, _ = await get_page()
    assert await get_observe_document_id(live_page) != selected_document_id


@pytest.mark.asyncio
async def test_owner_closed_frame_names_the_stale_selection(mcp_session: SessionState) -> None:
    leftover_frame, popup = await _select_frame_then_steal_focus(mcp_session)
    context = popup.context
    owner = context.pages[0]
    await owner.close()
    assert owner.is_closed() is True
    assert len(context.pages) == 1

    result = await skyvern_evaluate(expression="1 + 1")

    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
    assert result["error"]["hint"] == STALE_FRAME_HINT
    assert "Stale frame selection" in result["error"]["message"]
    assert "TargetClosedError" not in json.dumps(result)
    assert leftover_frame is mcp_session._working_frame
