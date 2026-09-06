"""Tests for the RecordingPage proxy that records code block playwright calls as actions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from playwright.async_api import BrowserContext, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import select

from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.copilot.code_block_steps import _METHOD_ACTION_TYPES
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import ActionModel
from skyvern.forge.sdk.db.utils import hydrate_action
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock, Credential
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    _HIGH_LEVEL_ACTION_MAP,
    _LOCATOR_ACTION_MAP,
    _PAGE_ACTION_MAP,
    CODE_BLOCK_FILENAME,
    CODE_LINE_OFFSET,
    RECORDED_FAILURE_CAPTURE_MAX_CHARS,
    RECORDED_FAILURE_RESPONSE_MAX_CHARS,
    PendingAction,
    PlaywrightInputDefaults,
    RecordingKeyboard,
    RecordingLocator,
    RecordingPage,
    _Recorder,
    json_safe_recorder_output,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.credential_release import (
    _VALUE_RELEASE_NAMES,
    ArmedSecret,
    CodeBlockCredentialReleaseError,
    CredentialReleaseGuard,
)
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockResult, BlockStatus
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus, ClickAction, GotoUrlAction, InputTextAction
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.playwright_input import (
    PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
    playwright_input_defaults_for_page,
    register_playwright_input_context,
)


class FakeFrame:
    def __init__(self, url):  # noqa: ANN001
        self.url = url


class FakeElementHandle:
    """Mirrors playwright's ElementHandle: it has owner_frame but NOT element_handle."""

    def __init__(self, url):  # noqa: ANN001
        self._url = url
        self.filled: list[str] = []
        self.typed: list[str] = []

    async def owner_frame(self):  # noqa: ANN201
        return FakeFrame(self._url)

    async def fill(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.filled.append(value)

    async def type(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.typed.append(text)


class FakeLocator(Locator):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.frame_url = "https://dash.example.com/account/login"
        self.element_handle_calls = 0
        self._page: Any = None

    @property
    def page(self) -> Any:
        return self._page

    @page.setter
    def page(self, value: Any) -> None:
        self._page = value

    def locator(self, selector):  # noqa: ANN001, ANN201
        return self

    def get_by_text(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    @property
    def first(self):  # noqa: ANN201
        return self

    async def element_handle(self, timeout=None):  # noqa: ANN001, ANN201
        self.element_handle_calls += 1
        return FakeElementHandle(self.frame_url)

    async def wait_for(self, **kwargs):  # noqa: ANN003, ANN201
        return None

    async def click(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append("click")

    async def fill(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"fill:{value}")

    async def type(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"type:{text}")

    async def press_sequentially(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"press_sequentially:{text}")

    async def select_option(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"select:{value}")

    async def press(self, key, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"press:{key}")

    def filter(self, **kwargs):  # noqa: ANN003, ANN201
        return self


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def press(self, key, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def type(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.typed.append(text)


class FakePage:
    def __init__(self) -> None:
        self.inner = FakeLocator()
        self.inner.page = self
        self.keyboard = FakeKeyboard()
        self.url = "about:blank"
        self.autocompleted: list[str] = []
        self.context = SimpleNamespace()
        self.default_timeout = PLAYWRIGHT_DEFAULT_TIMEOUT_MS

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    async def goto(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def wait_for_load_state(self, state="load", **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    def locator(self, selector):  # noqa: ANN001, ANN201
        return self.inner

    async def click(self, selector, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def fill(self, selector, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def type(self, selector, text, **kwargs):
        return None

    def get_by_role(self, role, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self.inner

    async def screenshot(self, **kwargs):  # noqa: ANN003, ANN201
        return b"img"

    async def fill_autocomplete(self, selector=None, value=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.autocompleted.append(value)

    async def evaluate(self, expression, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        return None

    async def complete(self, prompt=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def scroll(self, **kwargs):  # noqa: ANN003, ANN201
        return None


class ControlledGotoPage(FakePage):
    def __init__(self, outcome: object = None) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.outcome = outcome

    async def goto(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.started.set()
        await self.release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@asynccontextmanager
async def _recorded_action_db() -> AsyncIterator[AgentDB]:
    db = AgentDB("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as connection:
        await connection.run_sync(ActionModel.__table__.create)
    try:
        yield db
    finally:
        await db.engine.dispose()


async def _record_timed_action() -> Action:
    recorder = _Recorder()

    async def delayed_call() -> None:
        await asyncio.sleep(0.01)

    await recorder.record(ActionType.CLICK, "locator.click", "#go", delayed_call, (), {})
    action = recorder.actions[0]
    action.task_id = "tsk_timing"
    action.step_id = "stp_timing"
    action.step_order = 0
    return action


# Compiled under the code block filename and offset so the recorder's frame walk resolves a real
# authored line (the await sits on source line 4, which reports as authored line 2).
_AUTHORED_GOTO_SOURCE = (
    "\nasync def authored_goto(page):\n"
    "    url = 'https://example.com/private?token=secret'\n"
    "    return await page.goto(url)\n"
)
_authored_namespace: dict[str, Any] = {}
exec(compile(_AUTHORED_GOTO_SOURCE, CODE_BLOCK_FILENAME, "exec"), _authored_namespace)
_authored_goto: Callable[[RecordingPage], Awaitable[str]] = _authored_namespace["authored_goto"]


@pytest.mark.asyncio
async def test_pending_navigation_fact_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.code_block_recorder.PENDING_CALL_DELAY_SECONDS", 0.01)

    pending: list[PendingAction] = []
    emitted = asyncio.Event()

    def capture_pending(fact: PendingAction) -> None:
        pending.append(fact)
        emitted.set()

    stalled = ControlledGotoPage(outcome="response")
    page = RecordingPage(stalled, on_pending_action=capture_pending)
    call = asyncio.create_task(_authored_goto(page))
    await asyncio.wait_for(emitted.wait(), timeout=0.5)

    assert pending == [
        PendingAction(
            call_name="page.goto",
            threshold_seconds=0.01,
            code_line=2,
            action_type=ActionType.GOTO_URL,
            action_order=0,
        )
    ]
    assert page.recorded_actions() == []

    stalled.release.set()
    assert await call == "response"
    assert len(pending) == 1
    assert [action.status for action in page.recorded_actions()] == [ActionStatus.completed]

    fast_pending: list[PendingAction] = []
    fast_page = RecordingPage(FakePage(), on_pending_action=fast_pending.append)
    await fast_page.goto("https://example.com/fast")
    await asyncio.sleep(0.02)
    assert fast_pending == []

    failed_pending: list[PendingAction] = []
    failed_inner = ControlledGotoPage(outcome=RuntimeError("navigation failed"))
    failed_inner.release.set()
    failed_page = RecordingPage(failed_inner, on_pending_action=failed_pending.append)
    with pytest.raises(RuntimeError, match="navigation failed"):
        await failed_page.goto("https://example.com/fail")
    await asyncio.sleep(0.02)
    assert failed_pending == []

    cancelled_pending: list[PendingAction] = []
    cancelled_inner = ControlledGotoPage()
    cancelled_page = RecordingPage(cancelled_inner, on_pending_action=cancelled_pending.append)
    cancelled_call = asyncio.create_task(cancelled_page.goto("https://example.com/cancel"))
    await cancelled_inner.started.wait()
    cancelled_call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_call
    await asyncio.sleep(0.02)
    assert cancelled_pending == []

    callback_started = asyncio.Event()

    def failing_callback(fact: PendingAction) -> None:
        callback_started.set()
        raise RuntimeError("pending callback failed")

    callback_failure_inner = ControlledGotoPage(outcome="unchanged")
    callback_failure_page = RecordingPage(callback_failure_inner, on_pending_action=failing_callback)
    callback_failure_call = asyncio.create_task(callback_failure_page.goto("https://example.com/callback"))
    await asyncio.wait_for(callback_started.wait(), timeout=0.5)
    callback_failure_inner.release.set()
    assert await callback_failure_call == "unchanged"
    assert [action.status for action in callback_failure_page.recorded_actions()] == [ActionStatus.completed]


@pytest.mark.asyncio
async def test_keyboard_calls_arm_the_pending_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.code_block_recorder.PENDING_CALL_DELAY_SECONDS", 0.0)
    release = asyncio.Event()
    emitted = asyncio.Event()
    pending: list[PendingAction] = []

    def capture(fact: PendingAction) -> None:
        pending.append(fact)
        emitted.set()

    async def stall(*args: object, **kwargs: object) -> None:
        await release.wait()

    page = RecordingPage(
        SimpleNamespace(url="about:blank", keyboard=SimpleNamespace(down=stall, insert_text=stall)),
        on_pending_action=capture,
    )

    async def pending_for(invoke: Callable[[], Awaitable[object]]) -> PendingAction:
        emitted.clear()
        call = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(emitted.wait(), timeout=1)
        finally:
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        return pending[-1]

    assert (await pending_for(lambda: page.keyboard.down("Shift"))).call_name == "keyboard.down"
    assert (await pending_for(lambda: page.keyboard.insert_text("value"))).call_name == "keyboard.insert_text"

    stalled_guard = CredentialReleaseGuard()
    monkeypatch.setattr(stalled_guard, "enforce", stall)
    guarded_page = RecordingPage(
        SimpleNamespace(url="about:blank", keyboard=SimpleNamespace(type=stall)),
        on_pending_action=capture,
        credential_release_guard=stalled_guard,
    )
    assert (await pending_for(lambda: guarded_page.keyboard.type("value"))).call_name == "keyboard.type"


@pytest.mark.asyncio
async def test_recorded_action_has_wall_clock_timestamps_and_duration() -> None:
    action = await _record_timed_action()

    assert action.started_at is not None
    assert action.finished_at is not None
    assert action.started_at < action.finished_at
    assert action.started_at.tzinfo is None
    assert action.finished_at.tzinfo is None
    assert action.created_at is None
    assert action.modified_at is None
    assert isinstance(action.output, dict)
    assert "duration_ms" in action.output


@pytest.mark.asyncio
async def test_records_goto_click_fill_with_types_and_order() -> None:
    page = RecordingPage(FakePage())
    await page.goto("https://example.com")
    await page.locator("#q").fill("hello")
    await page.locator("#go").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [
        ActionType.GOTO_URL,
        ActionType.INPUT_TEXT,
        ActionType.CLICK,
    ]
    assert [a.action_order for a in recorded] == [0, 1, 2]
    assert all(a.status == ActionStatus.completed for a in recorded)
    assert recorded[0].description == "page.goto https://example.com"
    assert isinstance(recorded[0], GotoUrlAction)
    assert recorded[0].url == "https://example.com"
    assert isinstance(recorded[1], InputTextAction)
    assert recorded[1].element_id == "#q"
    assert recorded[1].text == ""
    assert isinstance(recorded[2], ClickAction)
    assert recorded[2].element_id == "#go"


@pytest.mark.asyncio
async def test_page_evaluate_records_execute_js_action() -> None:
    page = RecordingPage(FakePage())
    await page.evaluate("() => document.title")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.EXECUTE_JS]
    assert recorded[0].description == "page.evaluate () => document.title"
    assert recorded[0].status == ActionStatus.completed


@pytest.mark.asyncio
async def test_extract_does_not_resolve_on_a_raw_playwright_page() -> None:
    """Code blocks run on a raw Playwright page and must never reach the LLM extraction path,
    so page.extract neither resolves nor records a step."""
    page = RecordingPage(FakePage())
    await page.goto("https://example.com/")
    with pytest.raises(AttributeError):
        await page.extract(prompt="Extract the URLs of the top 20 posts")
    assert [a.action_type for a in page.recorded_actions()] == [ActionType.GOTO_URL]


def test_extract_is_absent_from_the_code_block_vocabulary() -> None:
    """Nothing may author or preview a page.extract call in a code block."""
    assert "extract" not in _HIGH_LEVEL_ACTION_MAP
    assert "extract" not in _METHOD_ACTION_TYPES


@pytest.mark.asyncio
async def test_other_high_level_skyvern_page_calls_are_recorded() -> None:
    """High-level SkyvernPage methods without a prompt still record their action type."""
    page = RecordingPage(FakePage())
    await page.scroll()
    await page.complete()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.SCROLL, ActionType.COMPLETE]


def test_recorder_maps_cover_every_action_wrapped_skyvern_page_method() -> None:
    """The recorder and editor-deriver maps are hand-maintained mirrors of SkyvernPage's
    @action_wrap set. A high-level method added there but absent here would execute
    unrecorded -- the exact SKY-11463 regression -- so assert every @action_wrap method
    is mapped (to the same action_type) on both surfaces, or is an explicit no-op exclusion."""
    live = {}
    for name in dir(SkyvernPage):
        action_type = getattr(getattr(SkyvernPage, name), "__skyvern_action_type__", None)
        if action_type is not None:
            live[name] = action_type
    # Guard against a vacuous pass if introspection ever stops finding the decorated surface.
    assert {"extract", "click", "complete", "scroll"} <= live.keys()

    recorder = {**_PAGE_ACTION_MAP, **_LOCATOR_ACTION_MAP, **_HIGH_LEVEL_ACTION_MAP}
    excluded = {
        "null_action",  # NULL_ACTION is a no-op probe, never a timeline step
        "extract",  # code blocks run raw Playwright; page.extract must not reach the LLM path
    }

    for name, action_type in live.items():
        if name in excluded:
            continue
        assert recorder.get(name) == action_type, (
            f"SkyvernPage.{name} is @action_wrap({action_type}) but RecordingPage maps it to "
            f"{recorder.get(name)!r}; add it to code_block_recorder or it executes unrecorded"
        )
        assert _METHOD_ACTION_TYPES.get(name) == action_type.value, (
            f"SkyvernPage.{name} ({action_type}) is missing/mismatched in "
            f"code_block_steps._METHOD_ACTION_TYPES; the editor step preview will drift from the timeline"
        )


@pytest.mark.asyncio
async def test_unmapped_calls_and_attributes_pass_through_unrecorded() -> None:
    fake = FakePage()
    page = RecordingPage(fake)
    await page.wait_for_load_state("networkidle")
    assert page.url == "about:blank"
    assert page.recorded_actions() == []


@pytest.mark.asyncio
async def test_keyboard_press_records_keypress_action() -> None:
    page = RecordingPage(FakePage())
    await page.keyboard.press("Enter")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.KEYPRESS]
    assert recorded[0].description and "Enter" in recorded[0].description


@pytest.mark.asyncio
async def test_get_by_role_click_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.get_by_role("button", name="Go").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]
    assert recorded[0].description == "locator.click get_by_role(button)"


@pytest.mark.asyncio
async def test_locator_get_by_chain_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.locator("#form").get_by_text("Submit").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]
    assert recorded[0].description == "locator.click get_by_text(Submit)"


@pytest.mark.asyncio
async def test_direct_page_actions_are_recorded_with_redaction() -> None:
    page = RecordingPage(FakePage())
    await page.click("#submit")
    await page.fill("#email", "secret@example.com")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK, ActionType.INPUT_TEXT]
    # Input values may be credentials; the fill value must never reach the description.
    assert all("secret@example.com" not in (a.description or "") for a in recorded)


@pytest.mark.asyncio
async def test_copilot_recording_page_routes_fill_and_type_through_shared_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = FakePage()
    page = RecordingPage(raw_page, strategy_aware_typing=True)

    await page.locator("#replace").fill("new", timeout=1234)
    await page.locator("#append").type(" tail", timeout=2345)
    await page.fill("#page-replace", "page new", timeout=3456)
    await page.type("#page-append", " page tail", timeout=4567)

    assert strategy_aware_input.await_args_list == [
        call(raw_page.inner, "new", clear=True, timeout=1234),
        call(raw_page.inner, " tail", clear=False, timeout=2345),
        call(raw_page.inner, "page new", clear=True, timeout=3456),
        call(raw_page.inner, " page tail", clear=False, timeout=4567),
    ]
    assert [action.action_type for action in page.recorded_actions()] == [ActionType.INPUT_TEXT] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "method", "timeout_form", "default_owner"),
    [
        pytest.param(
            surface,
            method,
            timeout_form,
            default_owner,
            id=f"{surface}-{method}-{timeout_form}-{default_owner}-custom-default",
        )
        for surface in ("locator", "page")
        for method in ("fill", "type")
        for timeout_form in ("omitted", "none")
        for default_owner in ("context", "page")
    ],
)
async def test_copilot_recording_uses_effective_playwright_timeout_when_omitted_or_none(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    method: str,
    timeout_form: str,
    default_owner: str,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = FakePage()
    context_default = 7654
    page_default = 8765 if default_owner == "page" else None
    page = RecordingPage(
        raw_page,
        strategy_aware_typing=True,
        playwright_input_defaults=PlaywrightInputDefaults(timeout_ms=page_default or context_default),
    )
    target = page.locator("#field") if surface == "locator" else page

    kwargs = {} if timeout_form == "omitted" else {"timeout": None}
    if surface == "locator":
        await getattr(target, method)("value", **kwargs)
    else:
        await getattr(target, method)("#field", "value", **kwargs)

    strategy_aware_input.assert_awaited_once_with(
        raw_page.inner,
        "value",
        clear=method == "fill",
        timeout=page_default or context_default,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "method"),
    [
        pytest.param("locator", "fill", id="locator-fill-zero-timeout"),
        pytest.param("locator", "type", id="locator-type-zero-timeout"),
        pytest.param("page", "fill", id="page-fill-zero-timeout"),
        pytest.param("page", "type", id="page-type-zero-timeout"),
    ],
)
async def test_copilot_recording_preserves_zero_timeout(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    method: str,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = FakePage()
    page = RecordingPage(raw_page, strategy_aware_typing=True)
    target = page.locator("#field") if surface == "locator" else page

    if surface == "locator":
        await getattr(target, method)("value", timeout=0)
    else:
        await getattr(target, method)("#field", "value", timeout=0)

    strategy_aware_input.assert_awaited_once_with(
        raw_page.inner,
        "value",
        clear=method == "fill",
        timeout=0,
    )


@pytest.mark.asyncio
async def test_copilot_recording_authored_playwright_timeout_catch_still_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_page = FakePage()
    page = RecordingPage(raw_page, strategy_aware_typing=True)
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field",
        AsyncMock(side_effect=TimeoutError),
    )

    caught = False
    try:
        await page.locator("#field").fill("value", timeout=1234)
    except PlaywrightTimeoutError:
        caught = True

    assert caught is True


@pytest.mark.asyncio
async def test_copilot_recording_page_preserves_page_selector_strictness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = MagicMock()
    strict_locator = MagicMock(spec=Locator)
    first_locator = MagicMock(spec=Locator)
    strict_locator.page = raw_page
    first_locator.page = raw_page
    strict_locator.first = first_locator
    raw_page.locator.return_value = strict_locator
    page = RecordingPage(
        raw_page,
        strategy_aware_typing=True,
        playwright_input_defaults=PlaywrightInputDefaults(timeout_ms=8765, strict_selectors=False),
    )

    await page.fill("#default", "default", timeout=None)
    await page.fill("#multi", "multi", strict=False, timeout=1234)
    await page.fill(selector="#strict", value="strict", strict=True, timeout=2345)

    assert strategy_aware_input.await_args_list == [
        call(first_locator, "default", clear=True, timeout=8765),
        call(first_locator, "multi", clear=True, timeout=1234),
        call(strict_locator, "strict", clear=True, timeout=2345),
    ]


def test_playwright_input_defaults_source_registered_runtime_context() -> None:
    context = MagicMock(spec=BrowserContext)
    page = MagicMock(spec=Page)
    page.context = context
    register_playwright_input_context(context, timeout_ms=6543, strict_selectors=True)

    defaults = playwright_input_defaults_for_page(page)

    assert defaults.timeout_ms == 6543
    assert defaults.strict_selectors is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fill", "type"])
@pytest.mark.parametrize("strict_form", ["omitted", "none"])
@pytest.mark.parametrize("context_strict", [False, True])
async def test_copilot_recording_page_omitted_or_none_strict_uses_context_default_through_strategy(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    strict_form: str,
    context_strict: bool,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = MagicMock()
    strict_locator = MagicMock(spec=Locator)
    first_locator = MagicMock(spec=Locator)
    strict_locator.page = raw_page
    first_locator.page = raw_page
    strict_locator.first = first_locator
    raw_page.locator.return_value = strict_locator
    page = RecordingPage(
        raw_page,
        strategy_aware_typing=True,
        playwright_input_defaults=PlaywrightInputDefaults(timeout_ms=4321, strict_selectors=context_strict),
    )

    kwargs = {} if strict_form == "omitted" else {"strict": None}
    await getattr(page, method)("#multiple", "value", **kwargs)

    strategy_aware_input.assert_awaited_once_with(
        strict_locator if context_strict else first_locator,
        "value",
        clear=method == "fill",
        timeout=4321,
    )


@pytest.mark.asyncio
async def test_copilot_recording_element_handle_fill_and_type_keep_raw_playwright_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    handle = FakeElementHandle("https://example.com/form")
    recorder = _Recorder(None, None, strategy_aware_typing=True)
    wrapped = RecordingLocator(handle, recorder, "#field")

    await wrapped.fill("replacement", timeout=1234)
    await wrapped.type(" appended", timeout=2345)

    strategy_aware_input.assert_not_awaited()
    assert handle.filled == ["replacement"]
    assert handle.typed == [" appended"]


@pytest.mark.asyncio
async def test_copilot_recording_routes_supported_playwright_input_options_through_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = MagicMock()
    raw_page.fill = AsyncMock()
    raw_page.type = AsyncMock()
    raw_locator = MagicMock(spec=Locator)
    raw_locator.page = raw_page
    raw_locator.first = raw_locator
    raw_locator.fill = AsyncMock()
    raw_locator.type = AsyncMock()
    raw_page.locator.return_value = raw_locator
    page = RecordingPage(raw_page, strategy_aware_typing=True)

    await page.locator("#forced").fill("replace", force=True, timeout=None)
    await page.locator("#delayed").type("append", delay=17, no_wait_after=True, timeout=1234)
    await page.fill("#page-forced", "replace", force=True, strict=False, timeout=None)
    await page.type("#page-delayed", "append", delay=23, no_wait_after=True, strict=True, timeout=2345)

    assert strategy_aware_input.await_args_list == [
        call(
            raw_locator,
            "replace",
            clear=True,
            timeout=PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
            force=True,
            delay=None,
            no_wait_after=None,
        ),
        call(
            raw_locator,
            "append",
            clear=False,
            timeout=1234,
            force=None,
            delay=17,
            no_wait_after=True,
        ),
        call(
            raw_locator,
            "replace",
            clear=True,
            timeout=PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
            force=True,
            delay=None,
            no_wait_after=None,
        ),
        call(
            raw_locator,
            "append",
            clear=False,
            timeout=2345,
            force=None,
            delay=23,
            no_wait_after=True,
        ),
    ]
    raw_locator.fill.assert_not_awaited()
    raw_locator.type.assert_not_awaited()
    raw_page.fill.assert_not_awaited()
    raw_page.type.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_page_tracks_runtime_default_timeout_for_strategy_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = MagicMock(spec=Page)
    raw_page.set_default_timeout = MagicMock()
    locator = MagicMock(spec=Locator)
    locator.page = raw_page
    locator.first = locator
    raw_page.locator.return_value = locator
    page = RecordingPage(raw_page, strategy_aware_typing=True)

    page.set_default_timeout(9876)
    await page.fill("#name", "Noor")

    raw_page.set_default_timeout.assert_called_once_with(9876)
    strategy_aware_input.assert_awaited_once_with(locator, "Noor", clear=True, timeout=9876)


@pytest.mark.asyncio
async def test_recording_context_tracks_runtime_default_timeout_for_strategy_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = MagicMock(spec=Page)
    raw_context = MagicMock()
    raw_context.set_default_timeout = MagicMock()
    raw_page.context = raw_context
    locator = MagicMock(spec=Locator)
    locator.page = raw_page
    locator.first = locator
    raw_page.locator.return_value = locator
    page = RecordingPage(raw_page, strategy_aware_typing=True)

    page.context.set_default_timeout(7654)
    await page.fill("#name", "Noor")

    raw_context.set_default_timeout.assert_called_once_with(7654)
    strategy_aware_input.assert_awaited_once_with(locator, "Noor", clear=True, timeout=7654)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "method", "malformed_call"),
    [
        ("locator", "fill", "duplicate"),
        ("locator", "fill", "extra_positional"),
        ("locator", "type", "duplicate"),
        ("locator", "type", "extra_positional"),
        ("page", "fill", "duplicate"),
        ("page", "fill", "extra_positional"),
        ("page", "type", "duplicate"),
        ("page", "type", "extra_positional"),
    ],
)
async def test_copilot_recording_rejects_malformed_playwright_input_before_strategy_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    method: str,
    malformed_call: str,
) -> None:
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    raw_page = FakePage()
    guard = CredentialReleaseGuard()
    enforce_credential_release = AsyncMock()
    monkeypatch.setattr(guard, "enforce", enforce_credential_release)
    page = RecordingPage(raw_page, credential_release_guard=guard, strategy_aware_typing=True)
    target = page.locator("#field") if surface == "locator" else page

    with pytest.raises(TypeError):
        if surface == "locator":
            if malformed_call == "duplicate":
                await getattr(target, method)("first", **{"value" if method == "fill" else "text": "second"})
            else:
                await getattr(target, method)("first", 1000)
        elif malformed_call == "duplicate":
            await getattr(target, method)("#field", "first", selector="#other")
        else:
            await getattr(target, method)("#field", "first", 1000)

    strategy_aware_input.assert_not_awaited()
    enforce_credential_release.assert_not_awaited()
    assert raw_page.inner.calls == []


@pytest.mark.asyncio
async def test_filter_locator_chain_click_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.get_by_role("button", name="Go").filter(has_text="Submit").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]


_ACTIONABILITY_ERROR = (
    "Locator.click: Timeout 5000ms exceeded.\n"
    "Call log:\n"
    '  - waiting for locator("#submit")\n'
    '  - <div class="privacy-notice-veil" role="dialog">…</div> intercepts pointer events'
)


@pytest.mark.asyncio
async def test_failed_call_records_the_browser_error_text_and_reraises() -> None:
    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError(_ACTIONABILITY_ERROR)

    fake = FakePage()
    fake.inner = ExplodingLocator()
    page = RecordingPage(fake)
    with pytest.raises(RuntimeError):
        await page.locator("#x").click()
    recorded = page.recorded_actions()
    assert recorded[-1].action_type == ActionType.CLICK
    assert recorded[-1].status == ActionStatus.failed
    assert recorded[-1].response != "Browser operation failed."
    assert "privacy-notice-veil" in (recorded[-1].response or "")
    assert "intercepts pointer events" in (recorded[-1].response or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_str",
    [
        pytest.param(lambda self: (_ for _ in ()).throw(RuntimeError("__str__ exploded")), id="raising"),
        pytest.param(lambda self: 42, id="non_str"),
    ],
)
async def test_a_hostile_dunder_str_cannot_swallow_the_original_failure(bad_str) -> None:  # noqa: ANN001
    """Capturing the message must not cost the fault: the original exception still propagates."""

    class Hostile(Exception):
        __str__ = bad_str

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise Hostile()

    fake = FakePage()
    fake.inner = ExplodingLocator()
    page = RecordingPage(fake)
    with pytest.raises(Hostile):
        await page.locator("#x").click()
    assert page.recorded_actions()[-1].response == "Hostile"


@pytest.mark.asyncio
async def test_a_huge_error_is_captured_above_the_mask_bound_but_still_capped() -> None:
    """Unbounded capture would exceed the parameter redactor's disclosure budget, which returns a
    replacement string instead of the payload and drops the whole row."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("x" * 70000)

    fake = FakePage()
    fake.inner = ExplodingLocator()
    page = RecordingPage(fake)
    with pytest.raises(RuntimeError):
        await page.locator("#x").click()
    captured = page.recorded_actions()[-1].response or ""
    assert len(captured) > RECORDED_FAILURE_RESPONSE_MAX_CHARS
    assert len(captured) == RECORDED_FAILURE_CAPTURE_MAX_CHARS


@pytest.mark.asyncio
async def test_on_action_sink_receives_each_action_and_errors_are_swallowed() -> None:
    seen: list[ActionType] = []

    async def sink(action) -> None:  # noqa: ANN001
        seen.append(action.action_type)
        raise RuntimeError("sink failure must not break recording")

    page = RecordingPage(FakePage(), on_action=sink)
    await page.goto("https://example.com")
    await page.locator("#go").click()
    assert seen == [ActionType.GOTO_URL, ActionType.CLICK]
    assert len(page.recorded_actions()) == 2


def test_user_code_line_from_exception_unwraps_wrapper_offset() -> None:
    code = "raise ValueError('boom')"
    full_code = f"\nasync def wrapper():\n    {code}\n    return None\n"
    namespace: dict = {}
    exec(compile(full_code, CODE_BLOCK_FILENAME, "exec"), {}, namespace)
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(namespace["wrapper"]())
    line = user_code_line_from_exception(exc_info.value)
    assert line == 3 - CODE_LINE_OFFSET  # frame line 3 -> user line 1


@pytest.mark.asyncio
async def test_generated_user_function_exception_maps_to_user_code_line() -> None:
    block = _make_code_block("x = 1")
    user_function = block.generate_async_user_function("x = 1\nraise Exception('boom')", FakePage())
    with pytest.raises(Exception, match="boom") as exc_info:
        await user_function()
    assert user_code_line_from_exception(exc_info.value) == 2


@pytest.mark.asyncio
async def test_input_values_are_elided_from_descriptions() -> None:
    page = RecordingPage(FakePage())
    await page.locator("#pw").fill("hunter2-credential")
    await page.locator("#user").type("alice-credential")
    recorded = page.recorded_actions()
    dumped = json.dumps([a.model_dump(mode="json") for a in recorded])
    assert "hunter2-credential" not in dumped
    assert "alice-credential" not in dumped
    assert recorded[0].description == "locator.fill #pw"
    assert recorded[1].description == "locator.type #user"


@pytest.mark.asyncio
async def test_copilot_authored_fill_and_type_share_strategy_aware_typing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePage()
    page = RecordingPage(fake, strategy_aware_typing=True)
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field",
        clear_field,
    )
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text",
        type_text,
    )

    await page.locator("#replace").fill("new")
    await page.locator("#append").type("more", timeout=1234)

    assert clear_field.await_args_list == [call(fake, fake.inner, char_count=0, timeout=PLAYWRIGHT_DEFAULT_TIMEOUT_MS)]
    assert type_text.await_args_list == [
        call(
            fake,
            fake.inner,
            "new",
            timeout=PLAYWRIGHT_DEFAULT_TIMEOUT_MS,
            allow_batched_playwright=True,
        ),
        call(fake, fake.inner, "more", timeout=1234, allow_batched_playwright=True),
    ]
    assert fake.inner.calls == []
    assert [action.action_type for action in page.recorded_actions()] == [
        ActionType.INPUT_TEXT,
        ActionType.INPUT_TEXT,
    ]


@pytest.mark.asyncio
async def test_copilot_authored_page_fill_keeps_one_action_and_long_value_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "a" * (TEXT_PRESS_MAX_LENGTH + 3)
    fake = FakePage()
    page = RecordingPage(fake, strategy_aware_typing=True)
    type_text = AsyncMock()
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text",
        type_text,
    )

    await page.fill("#replace", text, strict=False, timeout=2468)

    assert fake.inner.calls == [f"fill:{text[:-TEXT_PRESS_MAX_LENGTH]}"]
    type_text.assert_awaited_once_with(
        fake,
        fake.inner,
        text[-TEXT_PRESS_MAX_LENGTH:],
        timeout=2468,
        allow_batched_playwright=True,
    )
    [action] = page.recorded_actions()
    assert action.action_type == ActionType.INPUT_TEXT
    assert action.element_id == "#replace"


def _make_code_block(code: str, goal: str | None = None) -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="code_output",
        description="test output",
        output_parameter_id="op_code",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="code_1", code=code, prompt=goal, output_parameter=output_parameter)


class _FakeTask:
    def __init__(self) -> None:
        self.task_id = "tsk_code"
        self.organization_id = "o_test"


class _FakeStep:
    def __init__(self) -> None:
        self.step_id = "stp_code"
        self.order = 0


class FakeWorkflowRunContext:
    """Minimal context for CodeBlock.execute; masking delegates to the real implementation."""

    values: dict = {}
    workflow_run_outputs: list = []
    include_secrets_in_templates = False
    workflow_title = "Test Workflow"
    workflow_id = "w_test"
    workflow_permanent_id = "wpid_test"
    workflow_run_id = "wr_test"
    browser_session_id = None
    workflow = None
    mask_secrets = True

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}
        self.credential_tested_urls: dict[str, str] = {}

    def get_block_metadata(self, label):  # noqa: ANN001, ANN201
        return {}

    def build_workflow_run_summary(self) -> str:
        return ""

    def get_value(self, key):  # noqa: ANN001, ANN201
        return self.values.get(key)

    def get_original_secret_value_or_none(self, value):  # noqa: ANN001, ANN201
        return None

    def mask_secrets_in_data(self, data, mask="*****"):  # noqa: ANN001, ANN201
        return WorkflowRunContext.mask_secrets_in_data(self, data, mask)  # type: ignore[arg-type]

    async def register_output_parameter_value_post_execution(self, parameter, value):  # noqa: ANN001, ANN201
        return None


def _patch_execute_environment(
    monkeypatch: pytest.MonkeyPatch,
    page: FakePage,
    context: FakeWorkflowRunContext,
) -> dict[str, AsyncMock]:
    class FakeBrowserState:
        def __init__(self) -> None:
            self.browser_artifacts = BrowserArtifacts()

        async def get_working_page(self):  # noqa: ANN201
            return page

    async def validate_code_block(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    browser_state = FakeBrowserState()

    async def get_browser_state(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return browser_state

    async def record_output(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    mocks = {
        "get_workflow_run_block": AsyncMock(return_value=object()),
        "update_workflow_run_block": AsyncMock(return_value=None),
        "create_artifact": AsyncMock(return_value="artifact_1"),
        "create_task_and_step": AsyncMock(return_value=(_FakeTask(), _FakeStep())),
        "create_action": AsyncMock(return_value=None),
        "upsert_recorded_action": AsyncMock(return_value=None),
        "update_task": AsyncMock(return_value=None),
        "update_step": AsyncMock(return_value=None),
        "billing_hook": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block", validate_code_block
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "post_code_block_execution", mocks["billing_hook"], raising=False)
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", get_browser_state)
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *args, **kwargs: browser_state)
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda *args: context)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_block", mocks["get_workflow_run_block"])
    monkeypatch.setattr(app.DATABASE.observer, "update_workflow_run_block", mocks["update_workflow_run_block"])
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", mocks["create_artifact"])
    monkeypatch.setattr(app.agent, "create_task_and_step_from_code_block", mocks["create_task_and_step"], raising=False)
    monkeypatch.setattr(app.DATABASE.workflow_params, "create_action", mocks["create_action"])
    monkeypatch.setattr(
        app.DATABASE.workflow_params, "upsert_recorded_action", mocks["upsert_recorded_action"], raising=False
    )
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", mocks["update_task"])
    monkeypatch.setattr(app.DATABASE.tasks, "update_step", mocks["update_step"])
    return mocks


def _upsert_calls(mocks: dict[str, AsyncMock]) -> list[Action]:
    """Every recorded-action write, in order — the streamed (mid-block) writes then the end-of-block batch."""
    return [call.args[0] for call in mocks["upsert_recorded_action"].await_args_list]


def _created_actions(mocks: dict[str, AsyncMock]) -> list[Action]:
    # Final persisted row per action: the streamed write and the end-of-block batch converge on action_id,
    # so keep the last write (which carries the drained screenshot) and dedupe.
    by_id: dict[str | None, Action] = {}
    for action in _upsert_calls(mocks):
        by_id[action.action_id] = action
    return list(by_id.values())


@pytest.mark.asyncio
async def test_goal_code_block_creates_task_and_links_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code block with a goal spins up a task v1 + step and links it to the run block."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("value = 'ok'", goal="log into the portal")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_task_and_step"].await_count == 1
    linked = [
        call.kwargs.get("task_id")
        for call in mocks["update_workflow_run_block"].await_args_list
        if call.kwargs.get("task_id") is not None
    ]
    assert linked == ["tsk_code"]


@pytest.mark.asyncio
async def test_copilot_lineage_tracks_runtime_timeout_during_code_block_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    context = FakeWorkflowRunContext()
    _patch_execute_environment(monkeypatch, page, context)
    copilot_authored = AsyncMock(return_value=True)
    clear_field = AsyncMock()
    type_text = AsyncMock()
    monkeypatch.setattr(CodeBlock, "_workflow_is_copilot_authored", copilot_authored)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.clear_field", clear_field)
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.EventStrategyFactory.type_text", type_text)
    setter_block = _make_code_block("page.set_default_timeout(8765)")
    input_block = _make_code_block("await page.locator('#name').fill('Noor')")

    setter_result = await setter_block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
    )
    input_result = await input_block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test_2",
        organization_id="o_test",
    )

    assert setter_result.success is True
    assert input_result.success is True
    assert copilot_authored.await_args_list == [call(context), call(context)]
    clear_field.assert_awaited_once_with(
        page,
        page.inner,
        char_count=0,
        timeout=8765,
    )
    type_text.assert_awaited_once_with(
        page,
        page.inner,
        "Noor",
        timeout=8765,
        allow_batched_playwright=True,
    )
    assert page.default_timeout == 8765


@pytest.mark.asyncio
async def test_create_task_and_step_from_code_block_maps_goal_to_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container task carries the code block goal as its navigation goal so the agent can resume it."""
    create_task = AsyncMock(return_value=_FakeTask())
    update_task = AsyncMock(return_value=_FakeTask())
    create_step = AsyncMock(return_value=_FakeStep())
    monkeypatch.setattr(app.DATABASE.tasks, "get_last_task_for_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", create_task)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", create_step)

    block = _make_code_block("x = 1", goal="log into the portal")
    task, step = await ForgeAgent().create_task_and_step_from_code_block(
        code_block=block,
        organization_id="o_test",
        workflow_run_id="wr_test",
        task_url="https://example.com/login",
    )

    assert task.task_id == "tsk_code"
    assert step.step_id == "stp_code"
    assert create_task.await_args.kwargs["navigation_goal"] == "log into the portal"
    assert create_task.await_args.kwargs["url"] == "https://example.com/login"
    assert update_task.await_args.kwargs["status"] == TaskStatus.running
    assert create_step.await_args.kwargs["order"] == 0


@pytest.mark.asyncio
async def test_create_task_and_step_from_code_block_maps_empty_goal_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt-less code block stores navigation_goal NULL, not "", so action-plan lookups stay consistent."""
    create_task = AsyncMock(return_value=_FakeTask())
    monkeypatch.setattr(app.DATABASE.tasks, "get_last_task_for_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", create_task)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(return_value=_FakeTask()))
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", AsyncMock(return_value=_FakeStep()))

    block = _make_code_block("x = 1", goal="")
    await ForgeAgent().create_task_and_step_from_code_block(
        code_block=block,
        organization_id="o_test",
        workflow_run_id="wr_test",
        task_url="https://example.com/login",
    )

    assert create_task.await_args.kwargs["navigation_goal"] is None


@pytest.mark.asyncio
async def test_create_task_and_step_from_code_block_fails_partial_task_on_step_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If step creation fails after the container task is running, fail the task before the recorder degrades."""
    create_task = AsyncMock(return_value=_FakeTask())
    update_task = AsyncMock(return_value=_FakeTask())
    create_step = AsyncMock(side_effect=RuntimeError("step unavailable"))
    monkeypatch.setattr(app.DATABASE.tasks, "get_last_task_for_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", create_task)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", create_step)

    block = _make_code_block("x = 1", goal="log into the portal")

    with pytest.raises(RuntimeError, match="step unavailable"):
        await ForgeAgent().create_task_and_step_from_code_block(
            code_block=block,
            organization_id="o_test",
            workflow_run_id="wr_test",
            task_url="https://example.com/login",
        )

    assert [call.kwargs["status"] for call in update_task.await_args_list] == [
        TaskStatus.running,
        TaskStatus.failed,
    ]


@pytest.mark.asyncio
async def test_goal_code_block_marks_task_completed_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container task must not dangle in 'running'; success drives it to completed."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("value = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.completed in statuses
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert StepStatus.completed in step_statuses


@pytest.mark.asyncio
async def test_goal_code_block_marks_task_failed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing code block drives its container task to failed, not a stuck 'running'."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.failed in statuses
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert StepStatus.failed in step_statuses


@pytest.mark.asyncio
async def test_goal_code_block_finalizes_step_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An asyncio.CancelledError (copilot orphan-cancel) must still finalize task + step, not dangle in 'running'/'created'."""

    class CancellingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise asyncio.CancelledError()

    page = FakePage()
    page.inner = CancellingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    with pytest.raises(asyncio.CancelledError):
        await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    task_statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert TaskStatus.failed in task_statuses
    assert StepStatus.failed in step_statuses


@pytest.mark.asyncio
async def test_self_heal_success_finalizes_seat_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healed code block finalizes its SEAT task to completed — never a completed block over a failed seat."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("rotted selector")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    # The enabled-gate now lives at the chokepoint, ahead of the mocked floor.
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    # Force the floor path deterministically so this exercises execute()'s seat-finalization
    # wiring regardless of ambient cap-cache / api-key state leaked by other tests in the suite.
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.check_and_increment_self_heal_cap",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "resolve_self_heal_api_key", AsyncMock(return_value=None))
    block = _make_code_block("await page.locator('#x').click()", goal="go")
    # Stub the heal to a success result; this tests execute()'s seat-finalization wiring, not the
    # heal itself. The stub carries the full BlockResult surface the finalizer reads — heal
    # finalization rebuilds the result when download binding changes its output.
    monkeypatch.setattr(
        CodeBlock,
        "_attempt_self_heal",
        AsyncMock(
            return_value=BlockResult(
                success=True,
                output_parameter=block.output_parameter,
                output_parameter_value=None,
                failure_reason=None,
                status=BlockStatus.completed,
            )
        ),
    )

    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.completed in statuses
    assert TaskStatus.failed not in statuses


@pytest.mark.asyncio
async def test_self_heal_decline_finalizes_seat_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the heal declines (None), the block fails closed and the seat task is finalized failed."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("rotted selector")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", AsyncMock(return_value=None))

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.failed in statuses


@pytest.mark.asyncio
async def test_goalless_code_block_creates_task_and_persists_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A goalless block still gets a container task so its page activity is visible and billable."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_task_and_step"].await_count == 1
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.CLICK]
    assert all(a.task_id == "tsk_code" and a.step_id == "stp_code" for a in actions)


@pytest.mark.asyncio
async def test_goalless_code_block_takes_screenshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a container task now created for goalless blocks, screenshots anchor like goal blocks."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_artifact"].await_count == 1


@pytest.mark.asyncio
async def test_code_block_success_invokes_billing_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful code block invokes post_code_block_execution with its container task + step, after persist."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    mocks["billing_hook"].assert_awaited_once()
    task, step = mocks["billing_hook"].await_args.args
    assert task.task_id == "tsk_code"
    assert step.step_id == "stp_code"
    # Billing counts persisted action rows, so the hook must fire after persist. Streaming + the
    # end-of-block batch converge on one row per action via action_id — exactly one distinct action here.
    assert len(_created_actions(mocks)) == 1
    assert mocks["create_action"].await_count == 0  # recorder writes via the isolated upsert, not create_action


@pytest.mark.asyncio
async def test_code_block_failure_skips_billing_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed code block is not billed."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    assert mocks["billing_hook"].await_count == 0


@pytest.mark.asyncio
async def test_billing_hook_failure_does_not_fail_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Billing is best-effort at this seam: a hook error must never change the block outcome."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    mocks["billing_hook"].side_effect = RuntimeError("billing backend down")

    block = _make_code_block("value = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True


@pytest.mark.asyncio
async def test_recorded_calls_persist_as_actions_on_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each recorded playwright call becomes a real Action row tied to the task/step."""
    page = FakePage()
    context = FakeWorkflowRunContext(secrets={"pw": "secret-password"})
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block(
        "await page.goto('https://example.com')\n"
        "await page.locator('#pw').fill('secret-password')\n"
        "await page.locator('#go').click()",
        goal="go",
    )
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.GOTO_URL, ActionType.INPUT_TEXT, ActionType.CLICK]
    assert all(a.task_id == "tsk_code" and a.step_id == "stp_code" and a.step_order == 0 for a in actions)
    assert [a.action_order for a in actions] == [0, 1, 2]
    assert isinstance(actions[0], GotoUrlAction)
    assert actions[0].url == "https://example.com"
    assert isinstance(actions[1], InputTextAction)
    assert actions[1].element_id == "#pw"
    assert actions[1].text == ""
    assert isinstance(actions[2], ClickAction)
    assert actions[2].element_id == "#go"
    dumped = json.dumps([a.model_dump(mode="json") for a in actions])
    assert "secret-password" not in dumped
    hydrated = [
        hydrate_action(
            ActionModel(
                action_type=action.action_type,
                status=action.status,
                action_json=action.model_dump(mode="json"),
            )
        )
        for action in actions
    ]
    assert [type(action) for action in hydrated] == [GotoUrlAction, InputTextAction, ClickAction]


@pytest.mark.asyncio
async def test_backgrounded_screenshots_are_drained_and_linked_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshots upload off the user-await chain, but the drain must finish before
    actions persist so each persisted action carries its screenshot artifact id.
    The upload is made slow so this fails if the pre-persist drain is dropped: the
    action would serialize before the still-running upload sets its id."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    async def slow_upload(**kwargs: object) -> str:
        # Outlasts the microsecond gap between the user function ending and persist's
        # serialization, so only an explicit drain can complete it in time.
        await asyncio.sleep(0.05)
        return "artifact_1"

    mocks["create_artifact"].side_effect = slow_upload

    block = _make_code_block("await page.goto('https://example.com')\nawait page.locator('#go').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    # Both screenshot-eligible actions captured an artifact off the await chain...
    assert mocks["create_artifact"].await_count == 2
    # ...and the drain ran before persistence, so every persisted action links its screenshot.
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.GOTO_URL, ActionType.CLICK]
    assert all(a.screenshot_artifact_id == "artifact_1" for a in actions)


@pytest.mark.asyncio
async def test_page_evaluate_action_captures_and_links_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """page.evaluate (EXECUTE_JS) is a recorded, timeline-visible action and must get a screenshot
    like clicks and navigations do, so the run detail panel can render it instead of "No screenshot"."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.evaluate('() => document.title')", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.EXECUTE_JS]
    assert mocks["create_artifact"].await_count == 1
    assert actions[0].screenshot_artifact_id == "artifact_1"


@pytest.mark.asyncio
async def test_actions_stream_before_block_end_and_converge_on_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each action is written mid-block (streamed) AND in the end-of-block batch, but both writes share the
    action's stable id and upsert one row — no duplicate. This is the ticket's core idempotency guarantee."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    writes = _upsert_calls(mocks)
    # one streamed write during execution + one end-of-block batch write for the single action
    assert len(writes) == 2
    assert writes[0].action_id is not None
    assert writes[0].action_id == writes[1].action_id  # converge on one row, not a duplicate
    assert len(_created_actions(mocks)) == 1  # deduped to exactly one persisted action
    assert mocks["create_action"].await_count == 0  # shared agent write path left untouched


@pytest.mark.asyncio
async def test_streamed_write_precedes_screenshot_backfilled_by_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streamed row is written before the deferred screenshot upload finishes (screenshot_artifact_id is
    None); the end-of-block batch upserts the same id with the drained screenshot."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    writes = _upsert_calls(mocks)
    assert writes[0].screenshot_artifact_id is None  # streamed: upload still deferred
    assert writes[-1].screenshot_artifact_id == "artifact_1"  # batch: screenshot backfilled
    assert writes[0].action_id == writes[-1].action_id


@pytest.mark.asyncio
async def test_reupsert_preserves_action_end_timestamp_and_backfills_screenshot() -> None:
    async with _recorded_action_db() as db:
        action = await _record_timed_action()
        stamped_end = action.finished_at
        await db.workflow_params.upsert_recorded_action(action)

        await asyncio.sleep(0.01)
        action.screenshot_artifact_id = "artifact_timing"
        await db.workflow_params.upsert_recorded_action(action)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()

    assert row.finished_at == stamped_end
    assert row.screenshot_artifact_id == "artifact_timing"


@pytest.mark.asyncio
async def test_unstamped_reupsert_does_not_null_execution_timestamps() -> None:
    """The synthetic non-executed error row upserts with no execution timestamps; it must not
    overwrite a previously stamped row's started_at/finished_at with NULL."""
    async with _recorded_action_db() as db:
        action = await _record_timed_action()
        stamped_start, stamped_end = action.started_at, action.finished_at
        await db.workflow_params.upsert_recorded_action(action)

        unstamped = action.model_copy(update={"started_at": None, "finished_at": None})
        await db.workflow_params.upsert_recorded_action(unstamped)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()

    assert row.started_at == stamped_start
    assert row.finished_at == stamped_end


@pytest.mark.asyncio
async def test_restamped_reupsert_takes_the_new_execution_timestamps() -> None:
    """The batch refresh re-upserts with newer stamps; the incoming values must win over the
    stored ones (this is the case that discriminates the coalesce argument order)."""
    async with _recorded_action_db() as db:
        action = await _record_timed_action()
        await db.workflow_params.upsert_recorded_action(action)

        newer_start = action.started_at + timedelta(seconds=5)
        newer_end = action.finished_at + timedelta(seconds=9)
        restamped = action.model_copy(update={"started_at": newer_start, "finished_at": newer_end})
        await db.workflow_params.upsert_recorded_action(restamped)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()

    assert row.started_at == newer_start
    assert row.finished_at == newer_end


@pytest.mark.asyncio
async def test_unstamped_row_backfills_from_a_later_stamped_upsert() -> None:
    async with _recorded_action_db() as db:
        action = await _record_timed_action()
        stamped_start, stamped_end = action.started_at, action.finished_at
        unstamped = action.model_copy(update={"started_at": None, "finished_at": None})
        await db.workflow_params.upsert_recorded_action(unstamped)

        await db.workflow_params.upsert_recorded_action(action)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()

    assert row.started_at == stamped_start
    assert row.finished_at == stamped_end


@pytest.mark.asyncio
async def test_recorded_action_timestamps_round_trip_through_hydration() -> None:
    async with _recorded_action_db() as db:
        action = await _record_timed_action()
        await db.workflow_params.upsert_recorded_action(action)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()
            hydrated = hydrate_action(row)

    assert row.started_at == action.started_at
    assert row.finished_at == action.finished_at
    assert hydrated.started_at == action.started_at
    assert hydrated.finished_at == action.finished_at
    assert row.created_at >= action.finished_at
    assert row.modified_at >= action.finished_at


@pytest.mark.asyncio
async def test_unstamped_action_reupsert_keeps_bump_behavior() -> None:
    async with _recorded_action_db() as db:
        action = Action(
            action_id="act_unstamped",
            action_type=ActionType.CLICK,
            status=ActionStatus.completed,
            task_id="tsk_unstamped",
            step_id="stp_unstamped",
            step_order=0,
            action_order=0,
        )
        assert action.created_at is None
        assert action.modified_at is None
        await db.workflow_params.upsert_recorded_action(action)

        async with db.Session() as session:
            first = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()
            first_modified = first.modified_at

        await asyncio.sleep(0.01)
        action.screenshot_artifact_id = "artifact_unstamped"
        await db.workflow_params.upsert_recorded_action(action)

        async with db.Session() as session:
            row = (await session.scalars(select(ActionModel).where(ActionModel.action_id == action.action_id))).one()

    assert row.screenshot_artifact_id == "artifact_unstamped"
    assert row.modified_at > first_modified
    assert row.modified_at.tzinfo is None


@pytest.mark.asyncio
async def test_streamed_write_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets must be masked on the streamed path too, not only in the end-of-block batch."""
    secret = "s3cr3t-token"
    page = FakePage()
    context = FakeWorkflowRunContext(secrets={"pw": secret})
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block(f"await page.locator('#{secret}').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    writes = _upsert_calls(mocks)
    assert writes, "expected at least the streamed write"
    for action in writes:
        dumped = json.dumps(action.model_dump(mode="json"))
        assert secret not in dumped
        assert "*****" in dumped  # control: the secret was present and got masked, not simply absent


@pytest.mark.asyncio
async def test_persist_failure_does_not_fail_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    mocks["upsert_recorded_action"].side_effect = RuntimeError("db unavailable")

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["value"] == "ok"


@pytest.mark.asyncio
async def test_create_task_failure_does_not_fail_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording is best-effort: a DB hiccup creating the container task must not fail the block, and
    with no task the recorder degrades to in-memory only (no orphaned actions or screenshots)."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    mocks["create_task_and_step"].side_effect = RuntimeError("db unavailable")

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["value"] == "ok"
    assert mocks["upsert_recorded_action"].await_count == 0
    assert mocks["create_artifact"].await_count == 0


@pytest.mark.asyncio
async def test_link_block_failure_fails_task_and_disables_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task created but not linked to the run block must not stay running or receive orphan actions."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    async def fail_link_only(**kwargs: object) -> None:
        if kwargs.get("task_id") is not None:
            raise RuntimeError("db unavailable")

    mocks["update_workflow_run_block"].side_effect = fail_link_only

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert mocks["upsert_recorded_action"].await_count == 0
    assert mocks["create_artifact"].await_count == 0
    assert [call.kwargs.get("status") for call in mocks["update_task"].await_args_list] == [TaskStatus.failed]
    assert [call.kwargs.get("status") for call in mocks["update_step"].await_args_list] == [StepStatus.failed]


@pytest.mark.asyncio
async def test_caught_page_failure_then_unrelated_raise_persists_synthetic_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed page failure must not steal attribution from a later unrelated raise."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    code = "try:\n    await page.locator('#x').click()\nexcept Exception:\n    pass\nraise Exception('later failure')"
    block = _make_code_block(code, goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    actions = _created_actions(mocks)
    assert actions[-2].action_type == ActionType.CLICK
    assert actions[-2].status == ActionStatus.failed
    assert actions[-1].action_type == ActionType.NULL_ACTION
    assert actions[-1].status == ActionStatus.failed
    assert isinstance(actions[-1].output, dict) and actions[-1].output["code_line"] == 5
    assert actions[-1].response == "Failed to execute code block. Reason: Exception: later failure"
    # The synthetic error row is built outside the recorder; it still needs a stable id or the upsert
    # inserts a null primary key and the code-error row is lost.
    assert all(a.action_id for a in _upsert_calls(mocks))


@pytest.mark.asyncio
async def test_persisted_actions_never_contain_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cr3t-credential-value"

    class ExplodingLocator(FakeLocator):
        async def fill(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
            raise RuntimeError(f"cannot fill element with {value}")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext(secrets={"cred": secret})
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block(f"await page.locator('#pw').fill('{secret}')", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.status == BlockStatus.failed
    actions = _created_actions(mocks)
    assert actions
    dumped = json.dumps([a.model_dump(mode="json") for a in actions])
    assert secret not in dumped


def test_json_safe_recorder_output_normalizes_leaked_locator_wrappers() -> None:
    """SKY-12272: a leaked recorder proxy in a code block's output must collapse to a JSON-safe
    marker, never a raw proxy that raises TypeError at the registration boundary."""
    recorder = _Recorder(None)
    locator = RecordingLocator(FakeLocator(), recorder, "#invoice-link")
    keyboard = RecordingKeyboard(SimpleNamespace(), recorder)

    result = {
        "link": locator,
        "name": "Invoice_2026.pdf",
        "rows": [locator, {"nested": locator}],
        "kb": keyboard,
    }

    safe = json_safe_recorder_output(result)

    # The whole point: serializes with NO default= fallback. A raw wrapper raises TypeError here.
    json.dumps(safe)
    assert safe["name"] == "Invoice_2026.pdf"  # sibling field never starved
    assert safe["link"] == "<RecordingLocator>"  # leaked locator -> type marker, not its selector
    assert safe["rows"][0] == "<RecordingLocator>"  # nested inside a list
    assert safe["rows"][1]["nested"] == "<RecordingLocator>"  # nested inside a dict
    assert safe["kb"] == "<RecordingKeyboard>"  # non-locator proxy -> its own marker


def test_json_safe_recorder_output_normalizes_leaked_locator_used_as_key() -> None:
    """json.dumps rejects a non-primitive mapping key outright (default= is never consulted for
    keys), so a leaked proxy key must be normalized too, not just values."""
    locator = RecordingLocator(FakeLocator(), _Recorder(None), "#doc")
    safe = json_safe_recorder_output({locator: "delivered"})
    json.dumps(safe)  # a raw locator key raises TypeError: keys must be str/int/float/bool/None
    assert safe == {"<RecordingLocator>": "delivered"}


def test_json_safe_recorder_output_never_leaks_a_secret_bearing_selector() -> None:
    """A resolved credential can end up in a locator selector; mask_secrets_in_data scrubs dict
    values, not keys, so the marker must not carry the selector at all — as a value or a key."""
    secret = "s3cr3t-token"
    recorder = _Recorder(None)
    as_value = RecordingLocator(FakeLocator(), recorder, f"text={secret}")
    as_key = RecordingLocator(FakeLocator(), recorder, f"#{secret}")

    safe = json_safe_recorder_output({"field": as_value, as_key: "delivered"})

    assert secret not in json.dumps(safe)


def test_json_safe_recorder_output_passes_through_plain_data() -> None:
    payload = {"a": 1, "b": ["x", {"c": True, "d": None}], "e": 3.5}
    assert json_safe_recorder_output(payload) == payload


@pytest.mark.asyncio
async def test_code_block_output_registers_leaked_locator_as_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-12272 end-to-end: a code block that leaves a locator in a local variable registers a
    JSON-safe output (selector string), and sibling fields survive rather than dropping the payload."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("link = page.locator('#invoice-link')\nname = 'Invoice_2026.pdf'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.output_parameter_value is not None
    json.dumps(result.output_parameter_value)  # registration payload is JSON-safe
    assert result.output_parameter_value["name"] == "Invoice_2026.pdf"  # sibling preserved
    assert result.output_parameter_value["link"] == "<RecordingLocator>"  # locator normalized, not a raw proxy


def _release_guard(allowed_url: str = "https://dash.example.com/account/login") -> CredentialReleaseGuard:
    guard = CredentialReleaseGuard(workflow_run_id="wr_guard_test", block_label="login")
    guard.arm("Sup3rSecretPW!", allowed_url, "login_credentials")
    return guard


@pytest.mark.asyncio
async def test_off_site_credential_fill_is_refused_before_release() -> None:
    fake = FakePage()
    fake.inner.frame_url = "https://accounts.example.org/challenge/pwd"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError) as exc_info:
        await page.locator('input[type="password"]').fill("Sup3rSecretPW!")
    assert "login_credentials" in str(exc_info.value)
    assert re.search(r"https://example\.org", str(exc_info.value))
    assert not any(call.startswith("fill:") for call in fake.inner.calls)
    [action] = page.recorded_actions()
    # The recorded row is redacted by the transport hardening (SKY-13764); the refusal text reaches
    # the run record through the raised exception's failure_reason, not through this field.
    assert action.status == ActionStatus.failed


@pytest.mark.asyncio
async def test_same_site_credential_fill_releases() -> None:
    fake = FakePage()
    fake.inner.frame_url = "https://login.example.com/session"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    await page.locator('input[type="password"]').fill("Sup3rSecretPW!")
    assert "fill:Sup3rSecretPW!" in fake.inner.calls


@pytest.mark.asyncio
async def test_same_origin_without_registrable_domain_releases() -> None:
    fake = FakePage()
    fake.inner.frame_url = "http://localhost:8907/portal"
    page = RecordingPage(fake, credential_release_guard=_release_guard("http://localhost:8907/login"))
    await page.locator("#password").fill("Sup3rSecretPW!")
    assert "fill:Sup3rSecretPW!" in fake.inner.calls


@pytest.mark.asyncio
async def test_non_secret_values_skip_element_resolution() -> None:
    fake = FakePage()
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    await page.locator("#search").fill("hello world")
    assert fake.inner.element_handle_calls == 0
    assert "fill:hello world" in fake.inner.calls


@pytest.mark.asyncio
async def test_page_level_fill_is_guarded() -> None:
    fake = FakePage()
    fake.inner.frame_url = "https://accounts.example.org/challenge"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError):
        await page.fill("#password", "Sup3rSecretPW!")


@pytest.mark.asyncio
async def test_keyboard_type_is_guarded_by_page_url() -> None:
    fake = FakePage()
    fake.url = "https://accounts.example.org/challenge"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError):
        await page.keyboard.type("Sup3rSecretPW!")
    assert fake.keyboard.typed == []


@pytest.mark.asyncio
async def test_unguarded_page_records_and_releases_as_before() -> None:
    fake = FakePage()
    fake.inner.frame_url = "https://accounts.example.org/challenge"
    page = RecordingPage(fake)
    await page.locator("#password").fill("Sup3rSecretPW!")
    assert "fill:Sup3rSecretPW!" in fake.inner.calls
    assert fake.inner.element_handle_calls == 0


_SSO_MISROUTE_CODE = """
await page.goto("https://dash.example.com/logs")
sso_button = page.get_by_role("button", name="Sign in with IdP")
await sso_button.wait_for(state="visible", timeout=1000)
await sso_button.click()
identifier = page.locator("#identifierId")
await identifier.fill(login_credentials.username)
password_field = page.locator('input[type="password"]')
await password_field.fill(login_credentials.password)
"""


@pytest.mark.asyncio
async def test_sso_misroute_code_is_refused_at_the_first_off_site_release() -> None:
    """A block that clicks into a third-party sign-in and fills the saved credential there
    must fail with the site mismatch before any credential value reaches the page."""
    fake = FakePage()
    fake.inner.frame_url = "https://accounts.example.org/signin/identifier"
    guard = CredentialReleaseGuard(workflow_run_id="wr_guard_test", block_label="start_sso_sign_in")
    guard.arm("user@example.com", "https://dash.example.com/account/login", "login_credentials")
    guard.arm("Sup3rSecretPW!", "https://dash.example.com/account/login", "login_credentials")
    page = RecordingPage(fake, credential_release_guard=guard)
    block = _make_code_block(_SSO_MISROUTE_CODE)
    user_function = block.generate_async_user_function(
        _SSO_MISROUTE_CODE,
        page,
        {"login_credentials": Credential(username="user@example.com", password="Sup3rSecretPW!")},
    )
    with pytest.raises(CodeBlockCredentialReleaseError) as exc_info:
        await user_function()
    message = str(exc_info.value)
    assert "belongs to https://example.com" in message
    assert re.search(r"https://example\.org", message)
    assert not any(call.startswith(("fill:", "type:")) for call in fake.inner.calls)


@pytest.mark.asyncio
async def test_element_handle_fill_releases_on_site() -> None:
    """`page.wait_for_selector` yields an ElementHandle, which the recorder wraps in a
    RecordingLocator just like a Locator — but an ElementHandle has no element_handle() of its own.
    The guard must read its owner frame directly rather than dying on the credential's own site."""
    handle = FakeElementHandle("https://login.example.com/session")
    recorder = _Recorder(None, _release_guard())
    wrapped = RecordingLocator(handle, recorder, "#password")
    await wrapped.fill("Sup3rSecretPW!")
    assert handle.filled == ["Sup3rSecretPW!"]


@pytest.mark.asyncio
async def test_element_handle_fill_is_refused_off_site() -> None:
    handle = FakeElementHandle("https://accounts.example.org/challenge")
    recorder = _Recorder(None, _release_guard())
    wrapped = RecordingLocator(handle, recorder, "#password")
    with pytest.raises(CodeBlockCredentialReleaseError):
        await wrapped.fill("Sup3rSecretPW!")
    assert handle.filled == []


@pytest.mark.asyncio
async def test_press_sequentially_is_recorded_and_guarded() -> None:
    fake = FakePage()
    fake.inner.frame_url = "https://accounts.example.org/challenge"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError):
        await page.locator("#password").press_sequentially("Sup3rSecretPW!")
    assert fake.inner.calls == []


def test_value_release_names_are_recorded_operations() -> None:
    """The guard only runs inside the recorder's wrapper, which exists only for mapped names. A
    release name absent from the maps is silently unguarded — the drift that left
    press_sequentially unrecorded in the first place."""
    mapped = {
        *(f"locator.{name}" for name in _LOCATOR_ACTION_MAP),
        *(f"page.{name}" for name in _LOCATOR_ACTION_MAP),
        *(f"page.{name}" for name in _PAGE_ACTION_MAP),
        *(f"page.{name}" for name in _HIGH_LEVEL_ACTION_MAP),
    }
    unmapped = {name for name in _VALUE_RELEASE_NAMES if not name.startswith("keyboard.")} - mapped
    assert not unmapped, (
        f"{sorted(unmapped)} are in _VALUE_RELEASE_NAMES but not in any recording map, so the "
        "recorder never wraps them and the credential release guard never runs for them"
    )


@pytest.mark.asyncio
async def test_readable_off_site_frames_do_not_refuse_an_on_site_keystroke() -> None:
    """A login page embeds third-party frames (captcha, analytics) as a matter of course. Only a
    frame we could not read can be hiding focus, so readable off-site frames must not refuse."""
    fake = FakePage()
    fake.url = "https://login.example.com/session"

    class Quiet:
        def __init__(self, url):  # noqa: ANN001
            self.url = url

        async def evaluate(self, _expression):  # noqa: ANN001, ANN202
            return False

    fake.frames = [Quiet("https://login.example.com/session"), Quiet("https://captcha.example.org/widget")]
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    await page.keyboard.type("Sup3rSecretPW!")
    assert fake.keyboard.typed == ["Sup3rSecretPW!"]


def test_a_credential_with_an_unusable_tested_url_is_not_armed() -> None:
    """An unparseable saved login site yields no scope to compare against. Arming it anyway would
    refuse every fill of that credential; leaving it unarmed matches the absent-tested_url case."""
    guard = CredentialReleaseGuard(workflow_run_id="wr_x", block_label="b")
    # A basic-auth tested_url is refused by origin canonicalisation, so it yields no scope.
    assert guard.arm("Sup3rSecretPW!", "https://user:pass@example.com/login", "login_credentials") is False
    assert guard.is_armed is False


def test_a_value_shared_by_two_credentials_releases_on_either_site() -> None:
    """The same username is commonly saved against two sites; refusing on the second because the
    first was armed first would be an order-dependent false refusal."""
    guard = CredentialReleaseGuard(workflow_run_id="wr_x", block_label="b")
    guard.arm("user@example.com", "https://one.example.com/login", "first_credentials")
    guard.arm("user@example.com", "https://two.example.net/login", "second_credentials")
    candidates = guard.matches("user@example.com")
    assert len(candidates) == 2
    guard.check_release(
        candidates[0], "https://two.example.net/login", operation="locator.fill", alternatives=candidates[1:]
    )
    with pytest.raises(CodeBlockCredentialReleaseError):
        guard.check_release(
            candidates[0], "https://accounts.example.org/x", operation="locator.fill", alternatives=candidates[1:]
        )


def test_a_shorter_secret_cannot_authorize_releasing_a_longer_one() -> None:
    """Two credentials sharing one value may each authorize it, but a shorter secret that merely
    appears inside the typed value is a different secret: site A's password must not ride out on
    site B's shorter one."""
    guard = CredentialReleaseGuard(workflow_run_id="wr_x", block_label="b")
    guard.arm("hunter2!", "https://one.example.com/login", "first_credentials")
    guard.arm("hunter2", "https://two.example.net/login", "second_credentials")
    candidates = guard.matches("hunter2!")
    assert len(candidates) == 2
    with pytest.raises(CodeBlockCredentialReleaseError):
        guard.check_release(
            candidates[0], "https://two.example.net/login", operation="locator.fill", alternatives=candidates[1:]
        )


def test_unreadable_allowed_url_is_not_echoed_into_the_refusal() -> None:
    """tested_url can carry basic-auth or a token in its query; the refusal text and the log line
    both reach persisted records, so an unparseable scope must degrade to a placeholder."""
    guard = CredentialReleaseGuard(workflow_run_id="wr_x", block_label="b")
    guard._armed.append(ArmedSecret("Sup3rSecretPW!", "https://user:tok3n@example.com/login", "login_credentials"))
    entry = guard.match("Sup3rSecretPW!")
    assert entry is not None
    with pytest.raises(CodeBlockCredentialReleaseError) as exc_info:
        guard.check_release(entry, "https://accounts.example.org/x", operation="locator.fill")
    assert "tok3n" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_credential_release_refusal_reaches_the_run_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal names both sites, and that text is the observation the next authoring iteration
    repairs from — so it must survive into failure_reason rather than the generic reason every
    other exception collapses to."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("raise RuntimeError('placeholder')")

    def _raise_refusal(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise CodeBlockCredentialReleaseError(
            "Refused to type the saved credential `login_credentials` here: the credential belongs "
            "to https://example.com, but this field is on https://example.org."
        )

    monkeypatch.setattr(CodeBlock, "generate_async_user_function", _raise_refusal)
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    assert "belongs to https://example.com" in (result.failure_reason or "")
    assert re.search(r"https://example\.org", result.failure_reason or "")


@pytest.mark.asyncio
async def test_prompt_only_fill_is_judged_by_the_page_not_refused() -> None:
    """A prompt-only fill names no selector to resolve; judging it unresolvable would refuse a
    credential fill on the credential's own site."""
    fake = FakePage()
    fake.url = "https://login.example.com/session"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    await page.fill_autocomplete(value="Sup3rSecretPW!", prompt="the password field")


@pytest.mark.asyncio
async def test_prompt_only_fill_is_still_refused_off_site() -> None:
    fake = FakePage()
    fake.url = "https://accounts.example.org/challenge"
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError):
        await page.fill_autocomplete(value="Sup3rSecretPW!", prompt="the password field")


@pytest.mark.asyncio
async def test_keyboard_release_refuses_when_an_unreadable_frame_is_off_site() -> None:
    """Nothing claiming focus is ordinary in a headless window, so it cannot refuse by itself —
    but an unreadable off-site frame might be the one holding focus."""
    fake = FakePage()
    fake.url = "https://login.example.com/session"

    class Unreadable:
        url = "https://accounts.example.org/challenge"

        async def evaluate(self, _expression):  # noqa: ANN001, ANN202
            raise RuntimeError("frame detached")

    class Quiet:
        url = "https://login.example.com/session"

        async def evaluate(self, _expression):  # noqa: ANN001, ANN202
            return False

    fake.frames = [Quiet(), Unreadable()]
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    with pytest.raises(CodeBlockCredentialReleaseError):
        await page.keyboard.type("Sup3rSecretPW!")


@pytest.mark.asyncio
async def test_keyboard_release_allows_when_all_frames_are_on_site() -> None:
    fake = FakePage()
    fake.url = "https://login.example.com/session"

    class Quiet:
        url = "https://login.example.com/session"

        async def evaluate(self, _expression):  # noqa: ANN001, ANN202
            return False

    fake.frames = [Quiet()]
    page = RecordingPage(fake, credential_release_guard=_release_guard())
    await page.keyboard.type("Sup3rSecretPW!")
    assert fake.keyboard.typed == ["Sup3rSecretPW!"]


@pytest.mark.asyncio
async def test_execute_arms_the_guard_from_a_credential_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring, not the guard: a real credential parameter with a tested login site must arm the
    release check for the block's own run. Hand-armed guards in the other tests cannot see this
    path, so disabling it here is invisible to them."""
    page = FakePage()
    page.inner.frame_url = "https://accounts.example.org/challenge"
    context = FakeWorkflowRunContext()
    context.credential_tested_urls = {"login_credentials": "https://login.example.com/account"}
    _patch_execute_environment(monkeypatch, page, context)

    credential_parameter = CredentialParameter(
        key="login_credentials",
        credential_id="cred_test",
        description="test credential",
        credential_parameter_id="cpid_test",
        workflow_id="w_test",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )
    context.values = {"login_credentials": {"context": "placeholders", "password": "Sup3rSecretPW!"}}
    monkeypatch.setattr(
        FakeWorkflowRunContext, "get_original_secret_value_or_none", lambda self, value: value, raising=False
    )

    block = _make_code_block('await page.locator("#password").fill(login_credentials.password)')
    block.parameters = [credential_parameter]
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    assert "belongs to https://example.com" in (result.failure_reason or "")
    assert not any(call.startswith("fill:") for call in page.inner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["click", "wait_for"])
async def test_failure_locator_tracks_exact_exception_without_reparsing_selector(operation: str) -> None:
    page = FakePage()
    failure = PlaywrightTimeoutError("locator timed out")
    original = getattr(page.inner, operation)
    setattr(page.inner, operation, AsyncMock(side_effect=failure))
    recording = RecordingPage(page)
    with pytest.raises(PlaywrightTimeoutError) as caught:
        await getattr(recording.get_by_role("button", name="Continue"), operation)()
    assert recording.failure_locator(caught.value) is page.inner
    assert recording.failure_locator(PlaywrightTimeoutError("locator timed out")) is None
    setattr(page.inner, operation, original)
    await recording.get_by_role("button", name="Continue").click()
    assert recording.failure_locator(caught.value) is None


@pytest.mark.asyncio
async def test_inline_timeout_snapshot_masks_before_bounding_and_uses_exact_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline diagnostic reads the recorded locator, never a selector parsed from the error."""
    from skyvern.forge.sdk.workflow.models import block as block_module

    secret = "credential-secret"
    exception = PlaywrightTimeoutError("locator timed out")

    class TimedOutLocator:
        async def evaluate(self, script: str) -> str:
            assert "elementFromPoint" in script
            return f'<div id="cover">{secret}</div>'

    class SnapshotPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.url = f"https://fixture.test/checkout?token={secret}"

        async def title(self) -> str:
            return f"\x1b\u202e\nCheckout {secret}" + "x" * 1200

    recording = RecordingPage(SnapshotPage())
    locator = TimedOutLocator()
    monkeypatch.setattr(RecordingPage, "failure_locator", lambda _self, exc: locator if exc is exception else None)
    monkeypatch.setattr(
        block_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(redact_codeblock_parameter_values=lambda value, _parameters: value),
    )
    context = SimpleNamespace(mask_secrets_in_data=lambda value: value.replace(secret, "*****"))
    block = _make_code_block("pass")

    state = await block._capture_inline_failure_page_state(
        page=recording,
        failure_locator=recording.failure_locator(exception),
        workflow_run_context=context,
        redaction_parameters={},
    )

    assert state["final_url"] == "https://fixture.test/checkout?token=*****"
    assert state["page_title"].startswith("Checkout *****")
    assert len(state["page_title"]) == 1000
    assert state["covering_element"] == '<div id="cover">*****</div>'
    assert secret not in str(state)
    assert await block._capture_inline_failure_page_state(
        page=recording,
        failure_locator=recording.failure_locator(PlaywrightTimeoutError("same message")),
        workflow_run_context=context,
        redaction_parameters={},
    ) == {"final_url": "https://fixture.test/checkout?token=*****", "page_title": state["page_title"]}


@pytest.mark.asyncio
async def test_later_page_operation_invalidates_an_inflight_failure_locator():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = FakePage()
    started = asyncio.Event()
    release = asyncio.Event()
    failure = PlaywrightTimeoutError("covered target")

    async def delayed_click(**kwargs):
        started.set()
        await release.wait()
        raise failure

    page.inner.click = delayed_click
    recording = RecordingPage(page)
    pending = asyncio.create_task(recording.locator("#target").click())
    await started.wait()
    await recording.wait_for_load_state()
    release.set()
    with pytest.raises(PlaywrightTimeoutError):
        await pending
    assert recording.failure_locator(failure) is None
