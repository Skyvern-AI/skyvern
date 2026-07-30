"""PASTE_TEXT action: fill a spreadsheet grid with one tab/newline block.

Canvas-rendered grid editors expose no per-cell DOM, so cell-by-cell typing truncates and
misplaces. PASTE_TEXT sets the clipboard to the block and pastes it, which distributes the
tab/newline-separated values across cells atomically.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

from skyvern.core.script_generations.generate_script import _build_block_fn
from skyvern.forge.sdk.models import StepStatus
from skyvern.utils.action_redaction import redact_action_for_log
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import PasteTextAction
from skyvern.webeye.actions.handler import ActionHandler, check_for_invalid_web_action, handle_paste_text_action
from skyvern.webeye.actions.parse_actions import parse_action
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill the spreadsheet headers")
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

_TSV = "Column A\tColumn B\nValue A\tValue B"


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


def _scraped_page() -> MagicMock:
    sp = MagicMock()
    sp.id_to_element_hash = {"e1": "hash1"}
    sp.id_to_element_dict = {"e1": {"tagName": "div"}}
    sp.url = "https://example.com/sheet/"
    return sp


def _mock_isolated_clipboard(
    page: MagicMock,
    *,
    outcomes: list[dict | BaseException | None] | None = None,
) -> tuple[MagicMock, list[str]]:
    writes: list[str] = []
    pending_outcomes = list(outcomes or [])

    async def cdp_send(method: str, params: dict | None = None) -> dict:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main-frame"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 17}
        if method == "Runtime.callFunctionOn":
            assert params is not None
            writes.append(params["arguments"][0]["value"])
            outcome = pending_outcomes.pop(0) if pending_outcomes else None
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or {"result": {"type": "undefined"}}
        raise AssertionError(f"Unexpected CDP method: {method}")

    cdp_session = MagicMock()
    cdp_session.send = AsyncMock(side_effect=cdp_send)
    cdp_session.detach = AsyncMock()
    page.context.new_cdp_session = AsyncMock(return_value=cdp_session)
    return cdp_session, writes


# --------------------------------------------------------------------------- #
# parsing: LLM output -> PasteTextAction
# --------------------------------------------------------------------------- #
def test_parse_action_builds_paste_text() -> None:
    action = parse_action(
        {"action_type": "paste_text", "id": "e1", "text": _TSV, "reasoning": "fill the grid in one shot"},
        _scraped_page(),
    )
    assert isinstance(action, PasteTextAction)
    assert action.action_type == ActionType.PASTE_TEXT
    assert action.element_id == "e1"
    assert action.text == _TSV


def test_paste_text_repr_redacts_text() -> None:
    action = PasteTextAction(element_id="e1", text=_TSV, reasoning="fill the grid")

    rendered = repr(action)

    assert _TSV not in rendered
    assert "<redacted input value>" in rendered


def test_paste_text_payload_redacts_text() -> None:
    action = PasteTextAction(element_id="e1", text=_TSV, reasoning="fill the grid")

    assert redact_action_for_log(action)["text"] == "<redacted input value>"


def test_paste_text_prevents_cached_script_generation() -> None:
    with pytest.raises(ValueError, match="PASTE_TEXT"):
        _build_block_fn(
            {"label": "grid", "parameters": []},
            [{"action_type": ActionType.PASTE_TEXT, "text": _TSV, "xpath": "//*[@id='grid']"}],
        )


def test_paste_text_without_element_passes_action_preflight() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    assert check_for_invalid_web_action(action, MagicMock(), _scraped_page(), _TASK, _STEP) == []


# --------------------------------------------------------------------------- #
# handler: focus the anchor cell, grant clipboard, write the block, Ctrl+V
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_paste_text_grants_writes_and_pastes() -> None:
    action = PasteTextAction(element_id="e1", text=_TSV, reasoning="paste headers")
    resolved_text = "Resolved A\tResolved B"

    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.click = AsyncMock()
    skyvern_el = MagicMock()
    skyvern_el.get_locator.return_value = locator
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    page = MagicMock()
    page.url = "https://grid.example.com/sheet/abc?x=1"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    _, clipboard_writes = _mock_isolated_clipboard(page)

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value=resolved_text,
        ),
    ):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    # focused the anchor cell so the paste lands at the intended top-left position
    locator.click.assert_awaited_once()
    # granted clipboard, wrote the block, and pasted with a platform-neutral modifier
    page.context.grant_permissions.assert_awaited_once()
    # write-only, and scoped to this page's origin so later navigations don't inherit the grant
    assert page.context.grant_permissions.call_args.args[0] == ["clipboard-write"]
    assert page.context.grant_permissions.call_args.kwargs["origin"] == "https://grid.example.com"
    assert clipboard_writes == [resolved_text, ""]
    page.evaluate.assert_not_awaited()
    # exit any inline cell editor, anchor the grid at A1 (Ctrl+Home), then paste
    pressed = [c.args[0] for c in page.keyboard.press.call_args_list]
    assert pressed == ["Escape", "Control+Home", "ControlOrMeta+v"]


@pytest.mark.asyncio
@_skip_no_browser
async def test_handle_paste_text_real_page_clipboard_shadow_cannot_observe_secret() -> None:
    resolved_secret = "synthetic-resolved-secret"
    response_body = b"<html><body><input autofocus></body></html>"

    async def serve_page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            + f"Content-Length: {len(response_body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + response_body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve_page, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{port}/")
            await page.evaluate(
                """() => {
                    window.observedClipboardWrites = [];
                    navigator.clipboard.writeText = (text) => {
                        window.observedClipboardWrites.push(text);
                        return Promise.resolve();
                    };
                }"""
            )

            with patch(
                "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
                return_value=resolved_secret,
            ):
                results = await handle_paste_text_action(
                    action=PasteTextAction(element_id="", text="placeholder_AAAA_password", reasoning="paste"),
                    page=page,
                    scraped_page=_scraped_page(),
                    task=_TASK,
                    step=_STEP,
                )

            observed_values = await page.evaluate("window.observedClipboardWrites")
            await browser.close()
    finally:
        server.close()
        await server.wait_closed()

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert observed_values == []


@pytest.mark.asyncio
async def test_handle_paste_text_skips_grant_without_origin() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "about:blank"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    _mock_isolated_clipboard(page)

    results = await handle_paste_text_action(
        action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    page.context.grant_permissions.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_rejects_unresolved_totp_placeholder() -> None:
    action = PasteTextAction(element_id="", text="placeholder_grid_totp", reasoning="paste code")
    page = MagicMock()
    page.evaluate = AsyncMock()

    with patch(
        "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
        return_value="placeholder_grid_totp",
    ):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,token_value,expected",
    [
        ("placeholder_AAAA_password", "resolved value", "label\tresolved value"),
        ("placeholder_AAAA_totp", "BW_TOTP", "label\t123456"),
    ],
)
async def test_handle_paste_text_resolves_embedded_placeholders(
    token: str,
    token_value: str,
    expected: str,
) -> None:
    action = PasteTextAction(element_id="e1", text=f"label\t{token}", reasoning="paste values")
    task = _TASK.model_copy(update={"workflow_run_id": "wr_1"})
    workflow_context = MagicMock()
    workflow_context.find_embedded_placeholder_tokens.return_value = [token]
    workflow_context.get_original_secret_value_or_none.return_value = token_value
    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.click = AsyncMock()
    skyvern_element = MagicMock()
    skyvern_element.get_locator.return_value = locator
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_element)
    page = MagicMock(url="https://grid.example.com/sheet")
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard.press = AsyncMock()
    _, clipboard_writes = _mock_isolated_clipboard(page)

    def resolve(_task: object, parameter: str) -> str:
        return token_value if parameter == token else parameter

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom),
        patch("skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task", side_effect=resolve),
        patch(
            "skyvern.webeye.actions.handler.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
            new=MagicMock(return_value=workflow_context),
        ),
        patch("skyvern.webeye.actions.handler.generate_totp_value_with_task", return_value="123456"),
    ):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=task, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert clipboard_writes == [expected, ""]
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_rejects_resolved_secret_without_target_before_clipboard() -> None:
    action = PasteTextAction(element_id="", text="placeholder_fake_password", reasoning="paste secret")
    task = _TASK.model_copy(update={"workflow_run_id": "wr_1"})
    workflow_context = MagicMock()
    workflow_context.get_original_secret_value_or_none.return_value = "obvious-fake-secret"
    workflow_context.find_credential_parameter_key_for_secret.return_value = None
    workflow_context.find_embedded_placeholder_tokens.return_value = []
    page = MagicMock(url="https://grid.example.com/sheet")
    page.context.grant_permissions = AsyncMock()
    page.context.new_cdp_session = AsyncMock()
    page.keyboard.press = AsyncMock()

    with patch(
        "skyvern.webeye.actions.handler.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        new=MagicMock(return_value=workflow_context),
    ):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=task, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    page.context.grant_permissions.assert_not_awaited()
    page.context.new_cdp_session.assert_not_awaited()
    page.keyboard.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_without_element_skips_focus() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "https://grid.example.com/sheet/abc"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    _, clipboard_writes = _mock_isolated_clipboard(page)

    with patch("skyvern.webeye.actions.handler.DomUtil") as dom_cls:
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    dom_cls.assert_not_called()  # no element -> no DOM lookup, still pastes
    assert clipboard_writes == ["a\tb", ""]
    page.evaluate.assert_not_awaited()
    assert page.keyboard.press.call_args.args[0] == "ControlOrMeta+v"


@pytest.mark.asyncio
async def test_handle_paste_text_retries_clipboard_clear_after_navigation() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "https://grid.example.com/sheet/abc"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    _, clipboard_writes = _mock_isolated_clipboard(
        page,
        outcomes=[None, RuntimeError("execution context destroyed"), None],
    )

    results = await handle_paste_text_action(
        action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
    )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert clipboard_writes == ["a\tb", "", ""]
    page.wait_for_load_state.assert_awaited_once()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_logs_error_when_clear_retry_fails() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")
    page = MagicMock(url="https://grid.example.com/sheet/abc")
    page.context.grant_permissions = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    _, clipboard_writes = _mock_isolated_clipboard(
        page,
        outcomes=[
            None,
            RuntimeError("execution context destroyed"),
            RuntimeError("page closed"),
        ],
    )

    with patch("skyvern.webeye.actions.handler.LOG.error") as log_error:
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
    assert clipboard_writes == ["a\tb", "", ""]
    assert "may remain on the clipboard" in log_error.call_args.args[0]


@pytest.mark.asyncio
async def test_sensitive_paste_surfaces_clipboard_clear_failure_without_retrying() -> None:
    action = PasteTextAction(element_id="e1", text="placeholder_fake_password", reasoning="paste secret")
    task = _TASK.model_copy(update={"workflow_run_id": "wr_1"})
    workflow_context = MagicMock()
    workflow_context.get_original_secret_value_or_none.return_value = "obvious-fake-secret"
    workflow_context.find_embedded_placeholder_tokens.return_value = []
    locator = MagicMock()
    locator.scroll_into_view_if_needed = AsyncMock()
    locator.click = AsyncMock()
    skyvern_element = MagicMock()
    skyvern_element.get_locator.return_value = locator
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_element)
    page = MagicMock(url="https://grid.example.com/sheet")
    page.context.grant_permissions = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    _mock_isolated_clipboard(
        page,
        outcomes=[
            None,
            RuntimeError("execution context destroyed"),
            RuntimeError("page closed"),
        ],
    )

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom),
        patch(
            "skyvern.webeye.actions.handler.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
            new=MagicMock(return_value=workflow_context),
        ),
    ):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=task, step=_STEP
        )

    assert len(results) == 1
    assert type(results[0]) is ActionResult
    assert results[0].success is True
    assert results[0].needs_followup is True
    assert results[0].skip_remaining_actions is True
    pressed_keys = [call.args[0] for call in page.keyboard.press.await_args_list]
    assert pressed_keys == ["Escape", "Control+Home", "ControlOrMeta+v"]


@pytest.mark.asyncio
async def test_concurrent_paste_text_actions_serialize_clipboard_sequence() -> None:
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    events: list[str] = []

    first_page = MagicMock(url="https://grid.example.com/first")
    first_page.context.grant_permissions = AsyncMock()
    second_page = MagicMock(url="https://grid.example.com/second")
    second_page.context.grant_permissions = AsyncMock()

    async def write_clipboard(page: object, _text: str) -> None:
        label = "first" if page is first_page else "second"
        events.append(f"write:{label}")
        if page is first_page:
            first_write_started.set()
            await release_first_write.wait()

    async def paste(page: object, _keys: list[str]) -> None:
        label = "first" if page is first_page else "second"
        events.append(f"paste:{label}")

    async def clear_clipboard(page: object) -> bool:
        label = "first" if page is first_page else "second"
        events.append(f"clear:{label}")
        return True

    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")
    with (
        patch("skyvern.webeye.actions.handler._write_clipboard_text_in_isolated_world", side_effect=write_clipboard),
        patch("skyvern.webeye.actions.handler.handler_utils.keypress", side_effect=paste),
        patch("skyvern.webeye.actions.handler._clear_clipboard_after_paste", side_effect=clear_clipboard),
    ):
        first = asyncio.create_task(
            handle_paste_text_action(
                action=action, page=first_page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
            )
        )
        await first_write_started.wait()
        second = asyncio.create_task(
            handle_paste_text_action(
                action=action, page=second_page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
            )
        )
        await asyncio.sleep(0)
        release_first_write.set()
        await asyncio.gather(first, second)

    assert events == [
        "write:first",
        "paste:first",
        "clear:first",
        "write:second",
        "paste:second",
        "clear:second",
    ]


@pytest.mark.asyncio
async def test_paste_text_execution_is_blocked_when_umbrella_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="planner emitted paste")
    page = MagicMock(url="https://grid.example.com/sheet/abc")
    page.context.new_cdp_session = AsyncMock()
    page.keyboard.press = AsyncMock()
    provider = MagicMock()
    provider.is_feature_enabled_cached = AsyncMock(return_value=False)

    monkeypatch.setattr("skyvern.webeye.actions.handler.settings.PLANNER_MINI_GOAL_IMPROVEMENTS", False)
    monkeypatch.setattr("skyvern.webeye.actions.handler.app.EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler.app.AGENT_FUNCTION.wait_for_challenge_solver",
        AsyncMock(),
    )

    results = await ActionHandler._handle_action(_scraped_page(), _TASK, _STEP, page, action)

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    provider.is_feature_enabled_cached.assert_awaited_once_with(
        "PLANNER_MINI_GOAL_IMPROVEMENTS",
        _TASK.organization_id,
        properties={"organization_id": _TASK.organization_id},
    )
    page.context.new_cdp_session.assert_not_awaited()
    page.keyboard.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_clears_clipboard_when_paste_fails() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "https://grid.example.com/sheet/abc"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock(side_effect=RuntimeError("paste failed"))
    _, clipboard_writes = _mock_isolated_clipboard(page)

    with pytest.raises(RuntimeError, match="paste failed"):
        await handle_paste_text_action(action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP)

    assert clipboard_writes == ["a\tb", ""]
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_surfaces_rejected_clipboard_promise_reason() -> None:
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "https://grid.example.com/sheet"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    _mock_isolated_clipboard(
        page,
        outcomes=[
            {
                "exceptionDetails": {
                    "text": "Uncaught (in promise)",
                    "exception": {
                        "type": "object",
                        "description": "NotAllowedError: Write permission denied",
                    },
                },
                "result": {"type": "object", "value": {}},
            }
        ],
    )

    with patch("skyvern.webeye.actions.handler.DomUtil"):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert "NotAllowedError: Write permission denied" in results[0].exception_message
    assert "Uncaught (in promise)" not in results[0].exception_message
    page.keyboard.press.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_paste_text_fails_when_clipboard_unavailable() -> None:
    """navigator.clipboard is undefined outside a secure context, so the write throws there."""
    action = PasteTextAction(element_id="", text="a\tb", reasoning="paste at current selection")

    page = MagicMock()
    page.url = "http://insecure.example.com/sheet"
    page.context = MagicMock()
    page.context.grant_permissions = AsyncMock()
    page.evaluate = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    _mock_isolated_clipboard(
        page,
        outcomes=[
            {
                "exceptionDetails": {"text": "Uncaught"},
                "result": {"description": "Error: navigator.clipboard is undefined"},
            }
        ],
    )

    with patch("skyvern.webeye.actions.handler.DomUtil"):
        results = await handle_paste_text_action(
            action=action, page=page, scraped_page=_scraped_page(), task=_TASK, step=_STEP
        )

    # Surfaces as a failed action the planner can re-plan around, not an exception mid-handler.
    assert len(results) == 1 and isinstance(results[0], ActionFailure)
    assert "navigator.clipboard is undefined" in results[0].exception_message
    assert "Uncaught" not in results[0].exception_message
    page.keyboard.press.assert_not_awaited()
    page.evaluate.assert_not_awaited()


# --------------------------------------------------------------------------- #
# prompt: PASTE_TEXT offered to the agent only under the umbrella
# --------------------------------------------------------------------------- #
def _render_static(planner_mini_goal_improvements: bool) -> str:
    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "skyvern", "forge", "prompts", "skyvern")
    env = Environment(loader=FileSystemLoader(prompts_dir))
    ctx = dict(
        enable_new_planner_actions=False,
        data_extraction_goal=None,
        complete_criterion="",
        show_new_tab_action=False,
        show_switch_tab_action=False,
        show_close_page_action=False,
        navigation_goal="g",
        elements="e",
        action_history="",
        local_datetime="now",
        utc_datetime="now",
        error_code_mapping_str=None,
        data_extraction_schema=None,
    )
    return env.get_template("extract-action-static.j2").render(
        planner_mini_goal_improvements=planner_mini_goal_improvements, **ctx
    )


def test_paste_text_gated_on_planner_mini_goal_improvements() -> None:
    assert "PASTE_TEXT" not in _render_static(planner_mini_goal_improvements=False)
    assert "PASTE_TEXT" in _render_static(planner_mini_goal_improvements=True)
