"""Tests for the per-action screenshot sink in ``CodeBlockActionRecording``.

The sink is awaited inside the user's own call chain (``_Recorder``'s ``finally``), so its
timeout is charged to ``CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS`` on the failure path — a hung
page must degrade fast, and must never change the block outcome.

OSS-synced: synthetic ids and example.* placeholders only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    CODE_BLOCK_FILENAME,
    RECORDED_FAILURE_RESPONSE_MAX_CHARS,
    RecordingPage,
)
from skyvern.forge.sdk.workflow.models.code_block_recording import CodeBlockActionRecording
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    ActionType,
    ClickAction,
    SelectOption,
    SelectOptionAction,
)
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext

_RECORDING_PATH = "skyvern.forge.sdk.workflow.models.code_block_recording.app"


def _has_playwright_chromium() -> bool:
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_requires_chromium = pytest.mark.skipif(
    not _has_playwright_chromium(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


def _code_block() -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="sink_output",
        description="sink test output",
        output_parameter_id="op_sink",
        workflow_id="w_sink",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="sink_block", code="value = 'ok'", output_parameter=output_parameter)


def _recording(page: SimpleNamespace) -> CodeBlockActionRecording:
    recording = CodeBlockActionRecording(
        code_block=_code_block(),
        page=page,  # type: ignore[arg-type]
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        organization_id="o_1",
        workflow_run_context=FakeWorkflowRunContext(values={}, secrets={}),
    )
    recording._recording_enabled = True
    recording._task = SimpleNamespace(task_id="tsk_1")
    recording._step = SimpleNamespace(step_id="stp_1", order=0)
    recording._workflow_run_block = SimpleNamespace(workflow_run_block_id="wrb_1")
    return recording


@pytest.mark.asyncio
async def test_capture_uses_the_short_recording_budget_not_the_browser_default() -> None:
    # 20s of a dying page is charged to the block's execution timeout and buys nothing, so this
    # best-effort capture must not inherit BROWSER_SCREENSHOT_TIMEOUT_MS.
    screenshot = AsyncMock(return_value=b"png-bytes")
    page = SimpleNamespace(url="https://example.com/", screenshot=screenshot, is_closed=lambda: False)

    with patch(f"{_RECORDING_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", AsyncMock()):
        await _recording(page)._recorded_action_sink(Action(action_type=ActionType.CLICK))

    timeout_ms = screenshot.call_args.kwargs["timeout"]
    assert timeout_ms == settings.CODE_BLOCK_RECORDING_SCREENSHOT_TIMEOUT_MS
    assert timeout_ms < settings.BROWSER_SCREENSHOT_TIMEOUT_MS


@pytest.mark.asyncio
async def test_capture_failure_still_persists_the_action_and_reports_page_state() -> None:
    # The screenshot is the only thing lost: the timeline row must still be written, and the
    # warning must say whether the page was already closed or merely hung (the two are
    # indistinguishable from the Playwright TimeoutError alone).
    page = SimpleNamespace(
        url="https://example.com/",
        screenshot=AsyncMock(side_effect=TimeoutError("Page.screenshot: Timeout exceeded")),
        is_closed=lambda: True,
    )
    upsert = AsyncMock()

    with (
        patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert),
        capture_logs() as logs,
    ):
        await _recording(page)._recorded_action_sink(Action(action_type=ActionType.CLICK))

    upsert.assert_awaited_once()
    warning = next(log for log in logs if log["event"] == "Code block screenshot capture failed")
    assert warning["page_closed"] is True


@pytest.mark.asyncio
async def test_a_secret_straddling_the_response_bound_is_masked_whole() -> None:
    # The masker matches secret values by exact substring, so the recorded failure text has to be
    # masked before it is bounded or the cut leaves an unmatchable fragment on the persisted row.
    secret = "sk-live-" + "z" * 52
    prefix = "Locator.click: Timeout exceeded. Call log: "
    message = prefix.ljust(RECORDED_FAILURE_RESPONSE_MAX_CHARS - 30, "-") + secret + " intercepts pointer events"
    assert secret not in message[:RECORDED_FAILURE_RESPONSE_MAX_CHARS]
    assert secret[:30] in message[:RECORDED_FAILURE_RESPONSE_MAX_CHARS]

    page = SimpleNamespace(
        url="https://example.com/",
        screenshot=AsyncMock(return_value=b"png"),
        is_closed=lambda: False,
        goto=AsyncMock(side_effect=RuntimeError(message)),
    )
    recording = _recording(page)
    recording._workflow_run_context.secrets = {"api_token": secret}
    unbound_mask = WorkflowRunContext.mask_secrets_in_data
    recording._workflow_run_context.mask_secrets_in_data = unbound_mask.__get__(  # type: ignore[method-assign]
        recording._workflow_run_context
    )
    recording.recording_page = RecordingPage(page, on_action=recording._recorded_action_sink)
    upsert = AsyncMock()

    with (
        patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert),
        patch(f"{_RECORDING_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", AsyncMock()),
        pytest.raises(RuntimeError),
    ):
        await recording.recording_page.goto("https://example.com/next")

    persisted = upsert.await_args.args[0]
    assert len(persisted.response) == RECORDED_FAILURE_RESPONSE_MAX_CHARS
    assert "sk-live" not in persisted.response
    assert "Locator.click: Timeout exceeded." in persisted.response


@pytest.mark.asyncio
async def test_synthetic_failure_row_is_masked_before_it_is_bounded() -> None:
    secret = "sk-live-" + "z" * 52
    action = Action(
        action_type=ActionType.NULL_ACTION,
        status=ActionStatus.failed,
        response="Failed to execute code block. Reason: ".ljust(RECORDED_FAILURE_RESPONSE_MAX_CHARS - 30, "-") + secret,
    )
    page = SimpleNamespace(
        url="https://example.com/", screenshot=AsyncMock(return_value=b"png"), is_closed=lambda: False
    )
    recording = _recording(page)
    recording._workflow_run_context.secrets = {"api_token": secret}
    unbound_mask = WorkflowRunContext.mask_secrets_in_data
    recording._workflow_run_context.mask_secrets_in_data = unbound_mask.__get__(  # type: ignore[method-assign]
        recording._workflow_run_context
    )
    upsert = AsyncMock()

    with patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert):
        await recording._persist_action(action, recording._remember_action_metadata(action))

    persisted = upsert.await_args.args[0]
    assert len(persisted.response) <= RECORDED_FAILURE_RESPONSE_MAX_CHARS
    assert "sk-live" not in persisted.response


@pytest.mark.asyncio
async def test_parameter_redaction_preserves_trusted_metadata_and_logs_safely() -> None:
    def redact(value: Any, parameters: dict[str, Any]) -> Any:
        assert parameters == {
            "zero": 0,
            "disabled": False,
            "enabled": True,
            "derived": "sk-derived-secret",
        }
        if isinstance(value, dict):
            return {key: redact(item, parameters) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item, parameters) for item in value]
        if isinstance(value, bool | int) and value in {0, 1}:
            return "[redacted]"
        if isinstance(value, str):
            return value.replace("sk-derived-secret", "[redacted]").replace("0", "[redacted]")
        return value

    page = SimpleNamespace(
        url="https://example.com/",
        screenshot=AsyncMock(side_effect=TimeoutError("Page.screenshot: Timeout exceeded")),
        is_closed=lambda: False,
    )
    recording = _recording(page)
    recording.set_redaction_parameters({"zero": 0, "disabled": False, "enabled": True, "derived": "sk-derived-secret"})
    action = SelectOptionAction(
        action_id="act_0_trusted",
        source_action_id="source_0_trusted",
        action_type=ActionType.SELECT_OPTION,
        status=ActionStatus.completed,
        action_order=0,
        element_id="select_0",
        option=SelectOption(index=0),
        started_at=datetime(2026, 8, 15, 2, 5, 40, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 15, 2, 5, 50, tzinfo=timezone.utc),
    )
    # Simulate a future model_post_init-derived value that never enters model_fields_set.
    object.__setattr__(action, "download", True)
    object.__setattr__(action, "xpath", "sk-derived-secret")
    assert action.download is True
    assert action.xpath == "sk-derived-secret"
    assert "download" not in action.model_fields_set
    assert "xpath" not in action.model_fields_set
    upsert = AsyncMock()

    with (
        patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert),
        patch(f"{_RECORDING_PATH}.AGENT_FUNCTION.redact_codeblock_parameter_values", side_effect=redact),
    ):
        await recording._recorded_action_sink(action)
        action.action_id = "mutable-parameter-0"
        action.source_action_id = "mutable-parameter-0"
        action.action_order = 99
        await recording.persist([action])

        assert upsert.await_count == 2
        persisted = upsert.await_args.args[0]
        assert isinstance(persisted, SelectOptionAction)
        assert persisted.action_id == "act_0_trusted"
        assert persisted.source_action_id == "source_0_trusted"
        assert persisted.action_order == 0
        assert persisted.started_at == datetime(2026, 8, 15, 2, 5, 40, tzinfo=timezone.utc)
        assert persisted.finished_at == datetime(2026, 8, 15, 2, 5, 50, tzinfo=timezone.utc)
        assert persisted.option.index is None
        assert persisted.download is False
        assert persisted.xpath is None
        assert persisted.element_id == "select_[redacted]"

        failure_secret = "persistence-failure-parameter"
        upsert.side_effect = RuntimeError(failure_secret)
        with capture_logs() as logs:
            await recording.persist([action])

    warning = next(log for log in logs if log["event"] == "Failed to persist recorded code block action")
    assert warning["log_level"] == "warning"
    assert warning["workflow_run_block_id"] == "wrb_1"
    assert warning["action_order"] == 0
    assert "exc_info" not in warning
    assert failure_secret not in repr(warning)


@pytest.mark.asyncio
async def test_parameter_redaction_preserves_declared_string_defaults() -> None:
    def redact(value: Any, parameters: dict[str, Any]) -> Any:
        assert parameters == {"suffix": "ft"}
        if isinstance(value, dict):
            return {key: redact(item, parameters) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item, parameters) for item in value]
        return value.replace("ft", "[redacted]") if isinstance(value, str) else value

    page = SimpleNamespace(
        url="https://example.com/",
        screenshot=AsyncMock(side_effect=TimeoutError("Page.screenshot: Timeout exceeded")),
        is_closed=lambda: False,
    )
    recording = _recording(page)
    recording.set_redaction_parameters({"suffix": "ft"})
    action = ClickAction(action_id="act_click", action_order=1, element_id="button")
    upsert = AsyncMock()

    with (
        patch(f"{_RECORDING_PATH}.DATABASE.workflow_params.upsert_recorded_action", upsert),
        patch(f"{_RECORDING_PATH}.AGENT_FUNCTION.redact_codeblock_parameter_values", side_effect=redact),
    ):
        await recording._recorded_action_sink(action)

    persisted = upsert.await_args.args[0]
    assert isinstance(persisted, ClickAction)
    assert persisted.button == "left"


async def _finalize_and_capture_final_url(page: SimpleNamespace) -> AsyncMock:
    recording = _recording(page)
    recording._workflow_run_context.mask_secrets_in_data = (  # type: ignore[method-assign]
        lambda data, mask="*****": data.replace("sk-live", mask)
    )
    update_block = AsyncMock()
    with (
        patch(f"{_RECORDING_PATH}.DATABASE.tasks.update_task", AsyncMock()),
        patch(f"{_RECORDING_PATH}.DATABASE.tasks.update_step", AsyncMock()),
        patch(f"{_RECORDING_PATH}.DATABASE.observer.update_workflow_run_block", update_block),
        patch(f"{_RECORDING_PATH}.AGENT_FUNCTION.post_code_block_execution", AsyncMock()),
    ):
        await recording.finalize(success=True)
    return update_block


@pytest.mark.asyncio
async def test_successful_block_records_the_page_it_ended_on() -> None:
    # A resumed frontier reads this column to tell where the next block starts; the copilot's API
    # side cannot read a dispatched run's session over CDP, so an unwritten URL means no anchor.
    update_block = await _finalize_and_capture_final_url(
        SimpleNamespace(url="https://example.com/dashboard", is_closed=lambda: False)
    )

    assert update_block.await_args.kwargs["final_url"] == "https://example.com/dashboard"


@pytest.mark.asyncio
async def test_a_page_whose_url_carried_a_secret_is_not_recorded_as_an_anchor() -> None:
    # The stored URL doubles as a page a later run can be resumed against, and a masked URL is not
    # one — recording it would hand back a target that only looks navigable.
    update_block = await _finalize_and_capture_final_url(
        SimpleNamespace(url="https://example.com/dashboard?token=sk-live", is_closed=lambda: False)
    )

    assert update_block.await_args is None


_PENDING_SECRET = "credential-value-never-log"

_PENDING_EVENT_BASE = {
    "block_label": "sink_block",
    "code_line": None,
    "threshold_seconds": 0.0,
    "event": "codeblock.page_call_still_pending",
    "log_level": "warning",
    "workflow_run_block_id": "wrb_1",
    "workflow_run_id": "wr_1",
}


def _stalled_page(release: asyncio.Event) -> SimpleNamespace:
    async def stall(*args: object, **kwargs: object) -> None:
        await release.wait()

    return SimpleNamespace(
        url="about:blank",
        goto=stall,
        wait_for_url=stall,
        locator=lambda selector, **kwargs: SimpleNamespace(click=stall),
    )


@pytest.mark.parametrize(
    ("invoke", "expected"),
    [
        pytest.param(
            lambda page: page.wait_for_url(f"https://example.com/private?token={_PENDING_SECRET}"),
            {"call_name": "page.wait_for_url", "action_type": None, "action_order": None},
            id="unmapped-page-call",
        ),
        pytest.param(
            lambda page: page.locator(f"#pay-{_PENDING_SECRET}").click(),
            {"call_name": "locator.click", "action_type": ActionType.CLICK.value, "action_order": 0},
            id="mapped-non-goto-call",
        ),
        pytest.param(
            lambda page: page.goto(f"https://example.com/private?token={_PENDING_SECRET}"),
            {"call_name": "page.goto", "action_type": ActionType.GOTO_URL.value, "action_order": 0},
            id="goto-call",
        ),
    ],
)
@pytest.mark.asyncio
async def test_pending_page_call_fact_is_secret_safe(
    invoke: Callable[[RecordingPage], Awaitable[object]],
    expected: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.code_block_recorder.PENDING_CALL_DELAY_SECONDS", 0.0)
    release = asyncio.Event()

    recording = _recording(_stalled_page(release))
    recording._recording_enabled = False
    recording._code_block.code = f"private_value = {_PENDING_SECRET!r}"

    with capture_logs() as logs:
        call = asyncio.create_task(invoke(recording.recording_page))
        try:
            for _ in range(200):
                pending = [log for log in logs if log["event"] == "codeblock.page_call_still_pending"]
                if pending:
                    break
                await asyncio.sleep(0.001)

            assert len(pending) == 1
            event = pending[0]
            assert event == {**_PENDING_EVENT_BASE, **expected}
            assert _PENDING_SECRET not in repr(event)
        finally:
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call


@pytest.mark.asyncio
async def test_unmapped_page_call_pending_fact_carries_code_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.code_block_recorder.PENDING_CALL_DELAY_SECONDS", 0.0)
    release = asyncio.Event()

    # Compiled under the code block filename so the recorder's frame walk resolves a real authored
    # line (the await sits on source line 4, which reports as authored line 2).
    stalled_wait_source = (
        "\nasync def authored_wait(recording_page):\n"
        "    selector = '**/never-loaded'\n"
        "    return await recording_page.wait_for_url(selector)\n"
    )
    namespace: dict[str, Any] = {}
    exec(compile(stalled_wait_source, CODE_BLOCK_FILENAME, "exec"), namespace)

    stalled_page = SimpleNamespace(url="about:blank", wait_for_url=lambda *args, **kwargs: release.wait())
    recording = _recording(stalled_page)
    recording._recording_enabled = False

    with capture_logs() as logs:
        call = asyncio.create_task(namespace["authored_wait"](recording.recording_page))
        try:
            for _ in range(200):
                pending = [log for log in logs if log["event"] == "codeblock.page_call_still_pending"]
                if pending:
                    break
                await asyncio.sleep(0.001)

            assert len(pending) == 1
            event = pending[0]
            assert event["call_name"] == "page.wait_for_url"
            assert event["code_line"] == 2
        finally:
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call


@pytest.mark.asyncio
@_requires_chromium
async def test_real_playwright_stalled_calls_emit_pending_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.code_block_recorder.PENDING_CALL_DELAY_SECONDS", 0.05)
    release_connections = asyncio.Event()

    async def accept_without_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release_connections.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept_without_response, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with asyncio.timeout(30), async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                recording = CodeBlockActionRecording(
                    code_block=_code_block(),
                    page=page,
                    workflow_run_id="wr_1",
                    workflow_run_block_id="wrb_1",
                    organization_id="o_1",
                    workflow_run_context=FakeWorkflowRunContext(values={}, secrets={}),
                )
                with capture_logs() as logs:
                    with pytest.raises(PlaywrightTimeoutError):
                        await recording.recording_page.goto(f"http://127.0.0.1:{port}/", timeout=250)
                    with pytest.raises(PlaywrightTimeoutError):
                        await recording.recording_page.wait_for_url("**/never-reached", timeout=250)
                    with pytest.raises(PlaywrightTimeoutError):
                        await recording.recording_page.locator("#never-rendered").click(timeout=250)
            finally:
                await browser.close()
    finally:
        release_connections.set()
        server.close()
        await server.wait_closed()

    pending = [log for log in logs if log["event"] == "codeblock.page_call_still_pending"]
    assert [log["call_name"] for log in pending] == ["page.goto", "page.wait_for_url", "locator.click"]
    assert {log["workflow_run_block_id"] for log in pending} == {"wrb_1"}
