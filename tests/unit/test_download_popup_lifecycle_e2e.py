"""Real Chromium coverage for synchronous and after-return download Page ownership.

The browser supplies real Page events and committed documents; persistence is controlled at the
existing durable-credit boundary. No external site or production run is involved.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from skyvern.webeye.actions.actions import ClickAction, InputTextAction, SwitchTabAction
from skyvern.webeye.actions.handler import ActionHandler, _browser_dispatch_committed
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit.helpers import make_organization, make_step, make_task


def _deps_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _deps_available(),
    reason="Requires Playwright chromium (playwright install chromium)",
)

# A viewer document for the opened popup. Rendered inline (no Content-Disposition), so Chromium keeps
# it as an open Page and fires no Playwright download event.
_DOC_HTML = b"<!doctype html><html><head><title>Receipt</title></head><body>Receipt document</body></html>"
# A synchronous window.open in the click handler -- the producer shape of the anchor incident.
_OPENER_HTML = (
    b"<!doctype html><html><head><title>documents</title></head>"
    b"<body><button id='dl' onclick=\"window.open('/doc','_blank')\">download</button></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = _DOC_HTML if self.path.startswith("/doc") else _OPENER_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest_asyncio.fixture
async def popup_site() -> Any:
    """Real Chromium context with an opener page on a local origin, plus a download-event recorder."""
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-proxy-server", "--proxy-bypass-list=*"],
            proxy={"server": "direct://"},
        )
        context = await browser.new_context(accept_downloads=True)
        downloads: list[Any] = []
        context.on("download", lambda d: downloads.append(d))
        opener = await context.new_page()
        await opener.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        try:
            yield context, opener, downloads
        finally:
            await browser.close()
            server.shutdown()


@pytest.mark.asyncio
async def test_action_owned_popup_closed_after_late_download_credit(popup_site: Any, tmp_path: Path) -> None:
    context, opener, downloads = popup_site
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-popup", workflow_run_id="wr-popup")
    step = make_step(now, task, step_id="step-popup", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    captured_popup: list[Any] = []

    async def inner(*args: object, **kwargs: object) -> list[ActionSuccess]:
        pg = kwargs["page"]
        _browser_dispatch_committed.set(True)
        # Synchronous window.open toward a committing document: the real page.on("popup") producer
        # armed by handle_action fires, and no Playwright download event is ever raised.
        async with pg.expect_popup() as popup_info:
            await pg.evaluate("window.open('/doc','_blank')")
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        captured_popup.append(popup)
        return [ActionSuccess()]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page,
            task,
            step,
            opener,
            action,
            file_download_false_click_eligible=True,
        )

    # The artifact shape produced by the real action seam: a live committed same-context popup, no
    # Playwright download event, opener still open.
    assert captured_popup, "the false-click action did not open a popup"
    popup = captured_popup[0]
    assert not popup.is_closed(), "setup: popup must be open before credit"
    assert not opener.is_closed(), "setup: opener must be open before credit"
    assert downloads == [], "setup: inline document must not fire a Playwright download event"
    assert popup.url.endswith("/doc"), "setup: popup committed to a real URL (URL is not a cleanup filter)"
    assert popup.context is context, "setup: popup belongs to the initiating BrowserContext"
    # Producer sanity: the real false-click seam recorded the action-owned popup as a task claim.
    recorded = owning_ctx.download_popup_claims.get(task.task_id, [])
    assert any(candidate is popup for candidate in recorded), "the false-click producer did not record the popup claim"

    # Durable credit arrives later (file-scan lifecycle). The scraper-oriented list omits the wedged
    # popup (as in the anchor), but the credit consumer must still close exactly the action-owned popup.
    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener])

    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert popup.is_closed(), "action-owned download popup was left open after durable credit (SKY-15371 wedge)"
    assert not opener.is_closed(), "the opener page must remain open"


@pytest.mark.asyncio
async def test_dispatched_then_failed_popup_still_closed_after_late_credit(popup_site: Any, tmp_path: Path) -> None:
    """Real Chromium: a committed dispatch that opens a same-context popup and then FAILS keeps its
    action-owned claim through the failure, so the task's durable-credit seam still closes the marker.
    Before the dispatch-boundary fix the failure released the claim and the credit seam closed nothing."""
    context, opener, downloads = popup_site
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-popup-fail", workflow_run_id="wr-popup-fail")
    step = make_step(now, task, step_id="step-popup-fail", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    captured_popup: list[Any] = []

    async def inner(*args: object, **kwargs: object) -> list[ActionFailure]:
        pg = kwargs["page"]
        _browser_dispatch_committed.set(True)
        async with pg.expect_popup() as popup_info:
            await pg.evaluate("window.open('/doc','_blank')")
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        captured_popup.append(popup)
        # A real post-dispatch failure (e.g. a challenge-solver error or action timeout after the click).
        return [ActionFailure(Exception("dispatched then failed after opening the popup"))]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    assert captured_popup, "the dispatched action did not open a popup"
    popup = captured_popup[0]
    assert not popup.is_closed(), "the failed action must not close its own action-owned popup"
    # The dispatched-then-failed epoch retained its claim across the failure.
    recorded = owning_ctx.download_popup_claims.get(task.task_id, [])
    assert any(candidate is popup for candidate in recorded), "dispatched failure released the action-owned claim"

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener])

    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert popup.is_closed(), "durable credit must still close the marker of a dispatched-then-failed action"
    assert not opener.is_closed(), "the opener page must remain open"


@pytest.mark.asyncio
async def test_after_return_child_of_failed_dispatch_reserved_before_next_action(
    popup_site: Any, tmp_path: Path
) -> None:
    """Real browser timing: a committed dispatch FAILS for an unrelated reason and the download child it
    triggered joins the BrowserContext only AFTER handle_action returns, before the next action. The
    failure path must keep the ``context.on('page')`` owner armed to the next-action boundary exactly as
    the success path does, so the after-return child is reserved and closed by durable credit. Before the
    dispatch-boundary fix the failure detached the owner at its own exit and the child was never observed."""
    context, opener, downloads = popup_site
    sibling = await context.new_page()
    await sibling.goto(opener.url, wait_until="domcontentloaded")

    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-late-fail", workflow_run_id="wr-late-fail")
    step = make_step(now, task, step_id="step-late-fail", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    async def inner(*args: object, **kwargs: object) -> list[ActionFailure]:
        _browser_dispatch_committed.set(True)
        # The child opens ~300ms AFTER this dispatch returns; the action itself fails for an unrelated reason.
        await opener.evaluate("setTimeout(() => window.open('/doc', '_blank'), 300)")
        return [ActionFailure(Exception("challenge solver failed after the click was dispatched"))]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    returned_at = time.monotonic()
    assert owning_ctx.download_popup_late_candidates.get(task.task_id) is None, "nothing is reserved yet at return"
    # The failed epoch stashed its deferred reservation release for the step's no-credit seam.
    assert task.task_id in owning_ctx.pending_download_reservation_release
    # The context owner stays armed past the failed return and records the after-return child.
    async with asyncio.timeout(5):
        while not owning_ctx.download_popup_late_candidates.get(task.task_id):
            await asyncio.sleep(0.01)
    assert time.monotonic() - returned_at >= 0.15, "the child genuinely joined after handle_action returned"
    delayed_child = owning_ctx.download_popup_late_candidates[task.task_id][0]
    await delayed_child.wait_for_load_state("domcontentloaded")
    assert not delayed_child.is_closed(), "discovery alone must not close a page before durable credit"

    # A foreign-context page must never be observed by this context's owner.
    foreign_ctx = await context.browser.new_context()
    foreign_page = await foreign_ctx.new_page()
    candidates = owning_ctx.download_popup_late_candidates.get(task.task_id, [])
    assert all(c is not opener and c is not sibling and c is not foreign_page for c in candidates)

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener, sibling])
    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert delayed_child.is_closed(), "durable credit must close the reserved after-return child of a failed dispatch"
    assert not opener.is_closed()
    assert not sibling.is_closed()
    assert not foreign_page.is_closed()
    assert owning_ctx.download_popup_context_listeners.get(task.task_id) is None
    assert owning_ctx.download_popup_late_candidates == {}
    assert owning_ctx.download_popup_claims == {}

    await foreign_ctx.close()


@pytest.mark.asyncio
async def test_dispatched_failure_popup_survives_later_batch_action_then_credit_closes(
    popup_site: Any, tmp_path: Path
) -> None:
    """Two dispatched popups survive a failed click, successful fallback, and execute_step's no-credit seam."""
    context, opener, downloads = popup_site
    unrelated = await context.new_page()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, task_id="task-midbatch", workflow_run_id="wr-midbatch")
    task.navigation_goal = "Download receipt"
    step = make_step(now, task, step_id="step-midbatch", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    captured_popup: list[Any] = []

    async def open_popup(pg: Any) -> None:
        _browser_dispatch_committed.set(True)
        async with pg.expect_popup() as popup_info:
            await pg.evaluate("window.open('/doc','_blank')")
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded")
        captured_popup.append(popup)

    async def first_inner(*args: object, **kwargs: object) -> list[ActionFailure]:
        await open_popup(kwargs["page"])
        return [ActionFailure(Exception("dispatched then failed"))]

    async def second_inner(*args: object, **kwargs: object) -> list[ActionSuccess]:
        await open_popup(kwargs["page"])
        return [ActionSuccess()]

    with (
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        with patch.object(ActionHandler, "_handle_action", side_effect=first_inner):
            await ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            )
        popup = captured_popup[0]
        assert any(c is popup for c in owning_ctx.download_popup_claims.get(task.task_id, [])), "claim must be recorded"
        with patch.object(ActionHandler, "_handle_action", side_effect=second_inner):
            await ActionHandler.handle_action(
                scraped_page,
                task,
                step,
                opener,
                ClickAction(element_id="fallback", download=False),
                file_download_false_click_eligible=True,
            )

    assert not popup.is_closed(), "the later batch action must not close action1's popup"
    assert any(c is popup for c in owning_ctx.download_popup_claims.get(task.task_id, [])), (
        "the later batch action's entry released action1's owned claim before the credit seam"
    )

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener])
    agent = ForgeAgent()
    adopted = await context.new_page()
    owning_ctx.retire_intentional_page(task.task_id, adopted)
    step.status = StepStatus.failed
    credit_browser_state.get_working_page = AsyncMock(return_value=None)
    app_mock.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
    app_mock.DATABASE.tasks.get_task = AsyncMock(return_value=task)
    app_mock.DATABASE.tasks.update_task = AsyncMock(return_value=task)
    app_mock.AGENT_FUNCTION.validate_step_execution = AsyncMock()
    app_mock.AGENT_FUNCTION.post_step_execution = AsyncMock()
    app_mock.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
    with (
        patch("skyvern.forge.agent.app", app_mock),
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=owning_ctx),
        patch("skyvern.forge.agent.get_download_retry_started_at", AsyncMock(return_value=None)),
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, credit_browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(return_value=(step, None))),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        patch.object(agent, "_wait_for_in_flight_downloads", AsyncMock()),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(return_value=[])),
        patch.object(agent, "handle_failed_step", AsyncMock(return_value=None)),
        patch.object(agent, "clean_up_task", AsyncMock()),
    ):
        await agent.execute_step(
            organization,
            task,
            step,
            close_browser_on_completion=True,
            complete_verification=True,
            task_block=MagicMock(complete_on_download=True),
            engine=RunEngine.skyvern_v1,
            download_baseline_files=[],
        )
    assert all(not p.is_closed() for p in captured_popup)
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert len(captured_popup) == 2 and all(p.is_closed() for p in captured_popup)
    assert not opener.is_closed(), "the opener page must remain open"
    assert not adopted.is_closed() and not unrelated.is_closed()


@pytest.mark.asyncio
async def test_after_return_child_reserved_before_later_action_popup(popup_site: Any, tmp_path: Path) -> None:
    """Real browser timing across the action-return, next-action, and durable-credit boundaries."""
    context, opener, downloads = popup_site
    sibling = await context.new_page()
    await sibling.goto(opener.url, wait_until="domcontentloaded")

    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-late", workflow_run_id="wr-late")
    step = make_step(now, task, step_id="step-late", status=StepStatus.created, order=0, output=None)
    action = ClickAction(element_id="download-link", download=False)
    owning_ctx = SkyvernContext(task_id=task.task_id)

    scraped_page = MagicMock()
    seam_browser_state = MagicMock()
    seam_browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    seam_browser_state.release_driver_on_close = False

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = seam_browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)

    async def inner(*args: object, **kwargs: object) -> list[ActionSuccess]:
        _browser_dispatch_committed.set(True)
        await opener.evaluate("setTimeout(() => window.open('/doc', '_blank'), 300)")
        return [ActionSuccess()]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=inner),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.get_download_dir", MagicMock(return_value=str(tmp_path))),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    returned_at = time.monotonic()
    assert owning_ctx.download_popup_claims == {}
    assert owning_ctx.download_popup_late_candidates == {}
    async with asyncio.timeout(5):
        while not owning_ctx.download_popup_late_candidates.get(task.task_id):
            await asyncio.sleep(0.01)
    assert time.monotonic() - returned_at >= 0.15
    delayed_child = owning_ctx.download_popup_late_candidates[task.task_id][0]
    await delayed_child.wait_for_load_state("domcontentloaded")
    blank_marker = await context.new_page()

    async def open_later_popup(**kwargs: Any) -> list[ActionSuccess]:
        _browser_dispatch_committed.set(True)
        async with opener.expect_popup() as popup:
            await opener.evaluate("window.open('/doc?receipt', '_blank')")
        await (await popup.value).wait_for_load_state("domcontentloaded")
        return [ActionSuccess()]

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=open_later_popup),
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
    ):
        await ActionHandler.handle_action(
            scraped_page,
            task,
            step,
            opener,
            InputTextAction(element_id="verification", text="test"),
            file_download_false_click_eligible=True,
        )
    committed_popup = context.pages[-1]
    # A foreign-context page must never be observed by this context's owner.
    foreign_ctx = await context.browser.new_context()
    foreign_page = await foreign_ctx.new_page()

    assert blank_marker.url == "about:blank", "setup: the child reports about:blank before painting"
    assert committed_popup.url.endswith("/doc?receipt"), "setup: the site popup must be committed"
    assert downloads == [], "no Playwright download event fires in this shape"
    candidates = owning_ctx.download_popup_late_candidates.get(task.task_id, [])
    assert any(c is blank_marker for c in candidates)
    assert any(c is delayed_child for c in candidates)
    assert all(c is not committed_popup for c in candidates)
    assert all(c is not opener and c is not sibling and c is not foreign_page for c in candidates)
    assert not blank_marker.is_closed() and not committed_popup.is_closed(), "no close before durable credit"
    assert await committed_popup.locator("body").inner_text() == "Receipt document"

    # Durable credit arrives later.
    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    credit_browser_state.list_valid_pages = AsyncMock(return_value=[opener, sibling, committed_popup])

    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert blank_marker.is_closed(), "durable credit must close the owned about:blank page"
    assert delayed_child.is_closed(), "durable credit must close the reserved after-return child"
    assert not committed_popup.is_closed(), "a later action popup must survive unrelated credit"
    assert not opener.is_closed(), "the opener must remain open"
    assert not sibling.is_closed(), "the pre-existing sibling must remain open"
    assert not foreign_page.is_closed(), "a foreign-context page must never be touched"
    assert owning_ctx.download_popup_context_listeners.get(task.task_id) is None
    assert owning_ctx.download_popup_late_candidates == {}
    assert owning_ctx.download_popup_claims == {}
    after_credit = await context.new_page()
    assert owning_ctx.download_popup_late_candidates == {}, "credit must detach the listener"
    assert not after_credit.is_closed()

    await foreign_ctx.close()


@pytest.mark.asyncio
async def test_switch_tab_adopts_late_candidate_before_delayed_credit(popup_site: Any, tmp_path: Path) -> None:
    context, opener, downloads = popup_site
    unrelated = await context.new_page()
    await unrelated.goto(f"{opener.url}doc", wait_until="domcontentloaded")
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-switch", workflow_run_id="wr-switch")
    step = make_step(now, task, step_id="step-switch", status=StepStatus.created, order=0, output=None)
    owning_ctx = SkyvernContext(task_id=task.task_id)
    browser_state = RealBrowserState(pw=MagicMock(), browser_context=context, page=opener)
    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock()
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()

    with (
        patch("skyvern.webeye.actions.handler.app", app_mock),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=owning_ctx),
        patch("skyvern.webeye.actions.handler.get_download_dir", return_value=str(tmp_path)),
        patch("skyvern.webeye.actions.handler.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0),
    ):

        async def _dispatched_success(*args: object, **kwargs: object) -> list[ActionSuccess]:
            _browser_dispatch_committed.set(True)
            return [ActionSuccess()]

        with patch.object(ActionHandler, "_handle_action", side_effect=_dispatched_success):
            await ActionHandler.handle_action(
                MagicMock(),
                task,
                step,
                opener,
                ClickAction(element_id="download-link", download=False),
                file_download_false_click_eligible=True,
            )
        # Real page events after the click returns reserve both pages, even after they commit.
        target = await context.new_page()
        sibling = await context.new_page()
        await target.goto(unrelated.url, wait_until="domcontentloaded")
        await sibling.goto(unrelated.url, wait_until="domcontentloaded")
        assert owning_ctx.download_popup_late_candidates[task.task_id] == [target, sibling]
        assert downloads == []
        pages = await browser_state.list_valid_pages()
        result = await ActionHandler.handle_action(
            MagicMock(), task, step, opener, SwitchTabAction(tab_index=pages.index(target))
        )
        assert isinstance(result[-1], ActionSuccess)
        assert result[-1].skip_remaining_actions
        assert await browser_state.get_working_page() is target
        assert not target.is_closed() and not sibling.is_closed()

        await ForgeAgent()._close_credited_download_popups(task, browser_state)

        assert not target.is_closed(), "SWITCH_TAB must protect the adopted tab from delayed credit"
        assert sibling.is_closed(), "a genuine sibling reservation must still close"
        assert not opener.is_closed()
        assert not unrelated.is_closed(), "an unreserved page with the same URL must survive"
        assert await browser_state.get_working_page() is target
        assert await target.locator("body").inner_text() == "Receipt document"
        assert owning_ctx.download_popup_late_candidates == {}
        assert owning_ctx.download_popup_claims == {}


@pytest.mark.asyncio
async def test_claimed_real_dead_blank_no_credit_releases_and_recovers(popup_site: Any, tmp_path: Path) -> None:
    """Real Chromium: a claimed real ``about:blank`` popup that never credits is given its bounded grace
    (observed against a real empty download dir), then the EXACT page is released from both registries
    and internal recovery is returned -- exercising real ``page.context``/``page_is_dead_blank`` identity
    without orphaning the opener/survivor."""
    context, opener, _downloads = popup_site
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="tsk-recover", workflow_run_id="wr_recover")
    step = make_step(now, task, step_id="step-recover", status=StepStatus.created, order=0, output=None)
    dead = await context.new_page()  # a real about:blank dead-blank page
    owning_ctx = SkyvernContext(task_id=task.task_id)
    owning_ctx.record_download_popup_claim(task.task_id, dead)

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=dead)
    browser_state.list_valid_pages = AsyncMock(return_value=[dead, opener])

    agent = ForgeAgent()
    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=owning_ctx),
        patch("skyvern.forge.agent.resolve_run_download_id", return_value="wr_recover"),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=tmp_path),
    ):
        result = await agent._empty_page_recovery_plan(
            task,
            step,
            browser_state,
            # A genuinely tiny positive configured grace: production resolves ``download_timeout or
            # BROWSER_DOWNLOAD_TIMEOUT``, so 0 would fall back to the 600s default and hang the test.
            task_block=MagicMock(complete_on_download=True, download_timeout=0.01),
            attempt_started_at=None,
            list_files_before=[],
        )

    assert result is not None and len(result) == 1 and result[0].is_internal_recovery is True
    assert owning_ctx.has_download_popup_claim(task.task_id, dead) is False, "exact real page must be released"
    assert owning_ctx.download_popup_recovery_grace_started_at == {}, "the released page's anchor must be dropped"
    assert dead.context is context, "release/recovery must not perturb the real BrowserContext identity"
    assert not opener.is_closed(), "the http survivor must remain open"
