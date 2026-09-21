from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route, async_playwright
from playwright.sync_api import sync_playwright

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockCaptchaError
from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import ActionStatus
from skyvern.webeye.utils import captcha_solver as captcha_solver_module
from skyvern.webeye.utils.captcha_solver import CaptchaChallengeUnsolvedError, solve_challenge_ladder
from tests.unit.conftest import ScopeRecordingAgentFunction

CHALLENGE_URL = (
    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/if/ov2/av0/fake?sitekey=0xTESTKEY"
)
CHALLENGE_SUBDOMAIN_URL = "https://edge.challenges.cloudflare.com/cdn-cgi/challenge-platform/fake"


class FakeLocator:
    def __init__(
        self,
        *,
        count: int = 0,
        checked: bool = False,
        input_values: list[str] | None = None,
        visible: bool = True,
        box: dict[str, float] | None = None,
        raises: bool = False,
    ) -> None:
        self._count = count
        self._checked = checked
        self._visible = visible
        self._raises = raises
        self._box = box if box is not None else {"x": 10.0, "y": 10.0, "width": 300.0, "height": 80.0}
        self._input_values = list(input_values or [])
        self._input_value_calls = 0
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self) -> None:
        self._checked = True

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        if self._raises:
            raise PlaywrightError("Element is not attached to the DOM")
        return self._visible

    async def bounding_box(self) -> dict[str, float]:
        return self._box

    async def is_enabled(self) -> bool:
        return True

    async def is_checked(self) -> bool:
        return self._checked

    async def get_attribute(self, name: str) -> str | None:
        assert name == "aria-checked"
        return "true" if self._checked else "false"

    async def element_handle(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    async def input_value(self) -> str:
        if not self._input_values:
            return ""
        index = min(self._input_value_calls, len(self._input_values) - 1)
        self._input_value_calls += 1
        return self._input_values[index]

    @property
    def first(self) -> FakeLocator:
        return self


class FakeFrameElement:
    def __init__(self, *, visible: bool, src: str | None = None, box: dict[str, float] | None = None) -> None:
        self._visible = visible
        self._src = src
        self._box = box if box is not None else {"x": 0.0, "y": 100.0, "width": 300.0, "height": 65.0}
        self.get_attribute_calls: list[str] = []

    async def is_visible(self) -> bool:
        return self._visible

    async def bounding_box(self) -> dict[str, float]:
        return self._box

    async def get_attribute(self, name: str) -> str | None:
        self.get_attribute_calls.append(name)
        return self._src if name == "src" else None


class FakeFrame:
    def __init__(
        self,
        *,
        url: str,
        anchor: FakeLocator | None = None,
        parent_frame: FakePage | None = None,
        detached: bool = False,
        nested_marker: FakeLocator | None = None,
        is_hcaptcha_marker: bool = False,
        visible: bool = True,
        src: str | None = None,
        box: dict[str, float] | None = None,
    ) -> None:
        self.url = url
        self.anchor = anchor or FakeLocator(count=0)
        self.parent_frame = parent_frame
        self.detached = detached
        self.nested_marker = nested_marker or FakeLocator(count=0)
        self.is_hcaptcha_marker = is_hcaptcha_marker
        self.element = FakeFrameElement(visible=visible, src=src, box=box)

    def locator(self, selector: str) -> FakeLocator:
        if selector == "#recaptcha-anchor":
            return self.anchor
        if selector == captcha_solver_module._HCAPTCHA_MARKER_SELECTOR:
            return self.nested_marker if self.is_hcaptcha_marker else FakeLocator(count=0)
        if selector == captcha_solver_module._CAPTCHA_CHECKBOX_SELECTOR:
            return FakeLocator(count=0)
        return self.nested_marker

    def is_detached(self) -> bool:
        return self.detached

    async def frame_element(self) -> FakeFrameElement:
        return self.element


class FakePage:
    def __init__(
        self,
        *,
        checkbox: bool = False,
        recaptcha: bool = False,
        hcaptcha: bool = False,
        turnstile: bool = False,
        token_values: list[str] | None = None,
        frames: list[FakeFrame] | None = None,
        url: str = "https://app.example/login",
    ) -> None:
        self.checkbox = FakeLocator(count=1 if checkbox else 0)
        self.challenge = FakeLocator(count=1 if recaptcha else 0)
        self.recaptcha_token = FakeLocator(count=1 if token_values is not None else 0, input_values=token_values)
        self.continue_button = FakeLocator(count=1 if checkbox else 0)
        self.continue_button.click = AsyncMock(side_effect=self._continue)
        self.hcaptcha_marker = FakeLocator(count=1 if hcaptcha else 0)
        self.marker = FakeLocator(count=1 if (recaptcha or hcaptcha or turnstile) else 0)
        self.frames = list(frames or [])
        self.main_frame = object()
        self.viewport_size = {"width": 1280, "height": 720}
        self.url = url
        self.evaluated: list[str] = []

    async def _continue(self) -> None:
        self.checkbox._count = 0
        self.challenge._count = 0

    def locator(self, selector: str) -> FakeLocator:
        if "g-recaptcha-response" in selector:
            return self.recaptcha_token
        if "checkbox" in selector:
            return self.checkbox
        if "button" in selector:
            return self.continue_button
        if selector == captcha_solver_module._HCAPTCHA_MARKER_SELECTOR:
            return self.hcaptcha_marker
        if selector == captcha_solver_module._CAPTCHA_MARKER_SELECTOR:
            return self.marker
        return self.challenge

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        await asyncio.sleep(0)

    async def evaluate(self, expression: str, *_args: object) -> None:
        self.evaluated.append(expression)


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_is_fast_noop_without_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_noop")
    fn = block.generate_async_user_function(block.code, FakePage())

    await fn()

    assert await solve_challenge_ladder(FakePage()) is False
    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_clicks_unique_structural_checkbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(checkbox=True)
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_checkbox")

    await block.generate_async_user_function(block.code, page)()

    page.checkbox.click.assert_awaited_once_with()
    page.continue_button.click.assert_awaited_once_with()
    assert await page.checkbox.is_checked() is True
    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.parametrize(
    "anchor_url",
    (
        "https://www.google.com/recaptcha/api2/anchor",
        "https://www.google.com/recaptcha/enterprise/anchor",
    ),
)
@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_clicks_recaptcha_anchor_in_frame(
    monkeypatch: pytest.MonkeyPatch,
    anchor_url: str,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    parent_frame = FakePage(token_values=["", "opaque-token"])
    page = FakePage(
        recaptcha=True,
        frames=[FakeFrame(url=anchor_url, anchor=anchor, parent_frame=parent_frame)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_awaited_once_with()
    assert await anchor.is_checked() is True
    assert parent_frame.recaptcha_token._input_value_calls == 2
    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.parametrize(
    "anchor_url",
    (
        "https://evil.example/recaptcha/api2/anchor",
        "https://attacker.storage.googleapis.com/recaptcha/api2/anchor",
    ),
)
@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_does_not_click_untrusted_anchor_host(
    monkeypatch: pytest.MonkeyPatch,
    anchor_url: str,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    page = FakePage(
        recaptcha=True,
        frames=[FakeFrame(url=anchor_url, anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_untrusted_anchor")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_not_awaited()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_does_not_click_after_anchor_frame_navigates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    frame = FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)

    async def navigate_before_handle_returns() -> FakeLocator:
        frame.url = "https://evil.example/recaptcha/api2/anchor"
        return anchor

    anchor.element_handle = AsyncMock(side_effect=navigate_before_handle_returns)
    page = FakePage(recaptcha=True, frames=[frame])
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_navigated_anchor")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_not_awaited()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination_url", "navigation_solved"),
    (
        ("https://app.example/account", True),
        ("https://app.example/login#done", False),
    ),
)
async def test_real_sandbox_solve_captcha_returns_after_callback_navigation(
    monkeypatch: pytest.MonkeyPatch,
    destination_url: str,
    navigation_solved: bool,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=not navigation_solved),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    parent_frame = FakePage(token_values=[""])
    frame = FakeFrame(
        url="https://www.google.com/recaptcha/api2/anchor",
        anchor=anchor,
        parent_frame=parent_frame,
    )
    page = FakePage(recaptcha=True, frames=[frame])

    async def navigate_after_click(_milliseconds: int) -> None:
        frame.detached = True
        page.url = destination_url

    page.wait_for_timeout = AsyncMock(side_effect=navigate_after_click)
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_callback_navigation")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_awaited_once_with()
    if navigation_solved:
        agent_function.auto_solve_captchas.assert_not_awaited()
    else:
        agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_rejects_prechecked_anchor_without_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1, checked=True)
    page = FakePage(
        recaptcha=True,
        token_values=["opaque-token"],
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_prechecked")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_not_awaited()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_falls_through_when_anchor_stays_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    anchor.click = AsyncMock()
    page = FakePage(
        recaptcha=True,
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_unchecked")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_awaited_once_with()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_falls_through_when_token_lags_checked_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    page = FakePage(
        recaptcha=True,
        token_values=["", ""],
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_late_token")

    await block.generate_async_user_function(block.code, page)()

    assert await anchor.is_checked() is True
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_rejects_preexisting_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    page = FakePage(
        recaptcha=True,
        token_values=["stale-token"],
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_stale_token")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_awaited_once_with()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_rejects_inconclusive_token_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    page = FakePage(
        recaptcha=True,
        token_values=["opaque-token"],
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    page.recaptcha_token.input_value = AsyncMock(side_effect=[PlaywrightError("probe failed"), "opaque-token"])
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_unknown_baseline")

    await block.generate_async_user_function(block.code, page)()

    anchor.click.assert_awaited_once_with()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_bounds_unresponsive_anchor_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(captcha_solver_module, "_RECAPTCHA_ANCHOR_ARM_TIMEOUT_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.sleep(60)

    anchor = FakeLocator(count=1)
    anchor.click = AsyncMock(side_effect=hang)
    page = FakePage(
        recaptcha=True,
        frames=[FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor)],
    )
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_anchor_timeout")

    await asyncio.wait_for(block.generate_async_user_function(block.code, page)(), timeout=0.25)

    anchor.click.assert_awaited_once_with()
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_raises_constant_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_unsolved")
    page = FakePage(recaptcha=True)

    with pytest.raises(CodeBlockCaptchaError) as exc_info:
        await block.generate_async_user_function(
            block.code,
            page,
            organization_id="org-1",
            workflow_run_id="wr-1",
        )()

    assert str(exc_info.value) == "CAPTCHA could not be solved."
    assert "recaptcha" not in str(exc_info.value).lower()
    agent_function.auto_solve_captchas.assert_awaited_once()
    agent_function.solve_recaptcha_token.assert_awaited_once_with(
        page,
        organization_id="org-1",
        workflow_run_id="wr-1",
        browser_session_id=None,
    )


@pytest.mark.asyncio
async def test_builtin_reports_whether_an_arm_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page with no challenge markers must be distinguishable from a solve, so callers that
    re-perceive after a solve do not re-perceive a page nothing touched."""
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    assert await block_module._code_block_solve_captcha_builtin(FakePage()) is False
    assert (
        await block_module._code_block_solve_captcha_builtin(
            FakePage(recaptcha=True), organization_id="org-1", browser_session_id="bs-1"
        )
        is True
    )
    assert agent_function.solve_recaptcha_token.await_args.kwargs["browser_session_id"] == "bs-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [True, False])
async def test_builtin_records_one_solver_action_with_nested_probe(
    monkeypatch: pytest.MonkeyPatch,
    result: bool,
) -> None:
    async def ladder(page: RecordingPage, **_kwargs: object) -> bool:
        await page.locator("#challenge").click()
        return result

    monkeypatch.setattr(block_module, "solve_challenge_ladder", ladder)
    page = RecordingPage(FakePage())

    assert await block_module._code_block_solve_captcha_builtin(page, workflow_run_id="wr_test") is result

    actions = page.recorded_actions()
    assert [action.action_type for action in actions] == [ActionType.SOLVE_CAPTCHA, ActionType.CLICK]
    assert [action.action_order for action in actions] == [0, 1]
    assert actions[0].status == ActionStatus.completed
    assert actions[0].response == str(result).lower()
    assert actions[0].workflow_run_id == "wr_test"


@pytest.mark.asyncio
async def test_builtin_records_sanitized_unsolved_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive = "https://example.com/account?token=solver-secret#challenge"

    async def ladder(_page: RecordingPage, **_kwargs: object) -> bool:
        raise CaptchaChallengeUnsolvedError(sensitive)

    monkeypatch.setattr(block_module, "solve_challenge_ladder", ladder)
    page = RecordingPage(FakePage())

    with pytest.raises(CodeBlockCaptchaError, match="CAPTCHA could not be solved"):
        await block_module._code_block_solve_captcha_builtin(page)

    [action] = page.recorded_actions()
    assert action.action_type == ActionType.SOLVE_CAPTCHA
    assert action.status == ActionStatus.failed
    assert action.response == "CodeBlockCaptchaError"
    assert sensitive not in action.model_dump_json()


@pytest.mark.asyncio
async def test_builtin_records_only_the_unexpected_failure_type(monkeypatch: pytest.MonkeyPatch) -> None:
    sensitive = "https://example.com/account?token=solver-secret#challenge"
    error = RuntimeError(sensitive)

    async def ladder(_page: RecordingPage, **_kwargs: object) -> bool:
        raise error

    monkeypatch.setattr(block_module, "solve_challenge_ladder", ladder)
    page = RecordingPage(FakePage())

    with pytest.raises(RuntimeError) as exc_info:
        await block_module._code_block_solve_captcha_builtin(page, workflow_run_id="wr_test")

    assert exc_info.value is error
    [action] = page.recorded_actions()
    assert action.response == "RuntimeError"
    assert action.workflow_run_id == "wr_test"
    assert sensitive not in action.model_dump_json()


@pytest.mark.asyncio
async def test_authored_code_cannot_override_solver_workflow_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ladder_calls = 0

    async def ladder(_page: RecordingPage, **_kwargs: object) -> bool:
        nonlocal ladder_calls
        ladder_calls += 1
        return False

    monkeypatch.setattr(block_module, "solve_challenge_ladder", ladder)
    page = RecordingPage(FakePage())
    block = CodeBlock.model_construct(
        code='await solve_captcha(page, workflow_run_id="wr_forged")',
        label="captcha_run_binding",
    )

    with pytest.raises(TypeError, match="workflow_run_id"):
        await block.generate_async_user_function(
            block.code,
            page,
            workflow_run_id="wr_coordinator",
            organization_id="o_coordinator",
        )()

    assert ladder_calls == 0
    assert page.recorded_actions() == []

    valid_page = RecordingPage(FakePage())
    valid_block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_run_binding")
    await valid_block.generate_async_user_function(
        valid_block.code,
        valid_page,
        workflow_run_id="wr_coordinator",
        organization_id="o_coordinator",
    )()

    [action] = valid_page.recorded_actions()
    assert ladder_calls == 1
    assert action.workflow_run_id == "wr_coordinator"


def test_solve_captcha_is_reserved_in_sandbox_namespace() -> None:
    assert "solve_captcha" in CodeBlock.build_safe_vars()


@pytest.mark.asyncio
async def test_extension_arm_is_bounded_and_falls_through_on_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copilot scout calls this builtin with no enclosing timeout, so a solver extension that
    never answers must not stall the turn — the arm is bounded and the ladder continues."""

    async def _never_returns(*_args: object, **_kwargs: object) -> bool:
        await asyncio.Event().wait()
        raise AssertionError("should be cancelled before this point")

    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(side_effect=_never_returns),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(captcha_solver_module, "_EXTENSION_ARM_TIMEOUT_SECONDS", 0.01)
    page = FakePage(recaptcha=True)
    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_ext_hang")

    # wait_for makes an ablated (unbounded) arm fail this test cleanly instead of
    # hanging it until the CI job timeout.
    with pytest.raises(CodeBlockCaptchaError):
        await asyncio.wait_for(block.generate_async_user_function(block.code, page)(), timeout=5)

    agent_function.auto_solve_captchas.assert_awaited_once()
    agent_function.solve_recaptcha_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unsolved_anchor_click_closes_the_challenge_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clicking the anchor escalates to an image challenge whose overlay covers the page and
    outlives the arm, so a click that won no token has to put the widget back."""
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    parent_frame = FakePage(token_values=["", ""])
    page = FakePage(
        recaptcha=True,
        frames=[
            FakeFrame(
                url="https://www.google.com/recaptcha/api2/anchor",
                anchor=FakeLocator(count=1),
                parent_frame=parent_frame,
            )
        ],
    )

    with pytest.raises(CodeBlockCaptchaError):
        await block_module._code_block_solve_captcha_builtin(page, organization_id="org-1")

    assert any("grecaptcha" in expression for expression in page.evaluated)


@pytest.mark.asyncio
async def test_an_anchor_click_that_left_a_token_is_not_reset_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset discards whatever response it finds, so it must not run over a token already there."""
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    parent_frame = FakePage(token_values=["tok", "tok"])
    page = FakePage(
        recaptcha=True,
        frames=[
            FakeFrame(
                url="https://www.google.com/recaptcha/api2/anchor",
                anchor=FakeLocator(count=1),
                parent_frame=parent_frame,
            )
        ],
    )

    with pytest.raises(CodeBlockCaptchaError):
        await block_module._code_block_solve_captcha_builtin(page, organization_id="org-1")

    assert not any("grecaptcha" in expression for expression in page.evaluated)


@pytest.mark.asyncio
async def test_hcaptcha_marker_reaches_extension_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page whose only marker is an hCaptcha iframe must still reach the extension arm.

    Before hCaptcha markers were recognized, the initial probe saw no known marker and the ladder
    returned False without ever awaiting the solver.
    """
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(hcaptcha=True)

    assert await solve_challenge_ladder(page) is True
    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_nested_visible_frame_marker_reaches_extension_arm_with_hcaptcha_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An hCaptcha marker that lives inside a visible child frame (not the main document) must still be
    detected by the presence precheck and must select the hCaptcha extension-arm budget, not the shorter
    generic one."""
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    resolve_calls: list[float] = []
    monkeypatch.setattr(
        agent_function,
        "resolve_captcha_solver_extension_timeout",
        lambda _page, default_timeout: resolve_calls.append(default_timeout) or default_timeout,
    )
    child_frame = FakeFrame(
        url="https://app.example/challenge-frame",
        nested_marker=FakeLocator(count=1),
        is_hcaptcha_marker=True,
        visible=True,
    )
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)
    assert resolve_calls == [captcha_solver_module._HCAPTCHA_ARM_TIMEOUT_SECONDS]


@pytest.mark.asyncio
async def test_nested_invisible_frame_marker_is_not_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker inside a child frame whose own frame element is not visible (e.g. a hidden badge frame)
    must not count as a present challenge: no solver arm should be invoked."""
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(
        url="https://app.example/hidden-frame",
        nested_marker=FakeLocator(count=1),
        is_hcaptcha_marker=True,
        visible=False,
    )
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is False

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_invisible_marker_in_visible_frame_is_not_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    # An embedded form frame commonly carries a hidden challenge widget (an invisible reCAPTCHA badge);
    # only a match the user can actually see may send the page down the paid solver arms.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(
        url="https://app.example/embedded-form",
        nested_marker=FakeLocator(count=1, visible=False),
        visible=True,
    )
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is False

    agent_function.auto_solve_captchas.assert_not_awaited()


class _MatchList:
    """Several matches in one frame, each with its own visibility and box."""

    def __init__(self, matches: list[FakeLocator]) -> None:
        self._matches = matches

    async def count(self) -> int:
        return len(self._matches)

    def nth(self, index: int) -> FakeLocator:
        return self._matches[index]


@pytest.mark.asyncio
async def test_nested_visible_marker_after_many_hidden_ones_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single-page app can keep stale hidden widgets ahead of the live one in document order.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(url="https://app.example/challenge-frame", visible=True)
    # The live widget sits past the old five-match prefix and is not the last match, so neither a
    # first-N nor a last-only walk finds it.
    matches = [FakeLocator(count=1, visible=False) for _ in range(8)]
    matches[5] = FakeLocator(count=1)
    child_frame.nested_marker = _MatchList(matches)  # type: ignore[assignment]
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True, (
        "a visible match after hidden ones must count"
    )

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_page_marker_that_raises_does_not_hide_a_presented_one(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage()
    page.marker = _MatchList([FakeLocator(count=1, raises=True), FakeLocator(count=1)])  # type: ignore[assignment]

    assert await solve_challenge_ladder(page) is True, "an unreadable marker must not read as no challenge"

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_page_checkbox_that_raises_does_not_hide_a_presented_one(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage()
    page.checkbox = _MatchList([FakeLocator(count=1, raises=True), FakeLocator(count=1)])  # type: ignore[assignment]

    assert await solve_challenge_ladder(page) is True, "an unreadable checkbox must not read as no challenge"

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_nested_marker_that_raises_does_not_hide_a_presented_one(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(url="https://app.example/challenge-frame", visible=True)
    child_frame.nested_marker = _MatchList([FakeLocator(count=1, raises=True), FakeLocator(count=1)])  # type: ignore[assignment]
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True, (
        "an unreadable nested marker must not read as no challenge"
    )

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_hidden_checkbox_only_page_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(checkbox=True)
    page.checkbox = FakeLocator(count=1, visible=False)

    assert await solve_challenge_ladder(page) is False

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_offscreen_marker_is_not_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    # An embedded widget parked outside the viewport still reports is_visible(); it is not a gate the user sees.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(
        url="https://app.example/offscreen-frame",
        nested_marker=FakeLocator(count=1, box={"x": -5000.0, "y": 0.0, "width": 300.0, "height": 80.0}),
        is_hcaptcha_marker=True,
        visible=True,
    )
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is False, "an off-screen match must not count"

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_onscreen_match_after_an_offscreen_one_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pairs with the off-screen test: an off-screen match ahead of a real one must not end the walk.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(url="https://app.example/challenge-frame", visible=True)
    offscreen = FakeLocator(count=1, box={"x": -5000.0, "y": 0.0, "width": 300.0, "height": 80.0})
    child_frame.nested_marker = _MatchList([offscreen, FakeLocator(count=1)])  # type: ignore[assignment]
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True, "an on-screen match must count"


class _WedgedFrame:
    url = "https://app.example/wedged"

    async def frame_element(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_wedged_child_frames_cannot_outlast_the_scan_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each frame is bounded on its own, so without a whole-scan budget five wedged frames would cost five
    # full per-frame bounds before the ladder could even decide nothing is there.
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(captcha_solver_module, "_CHILD_FRAME_SCAN_BUDGET_SECONDS", 0.3)
    page = FakePage(frames=[_WedgedFrame() for _ in range(5)])

    started = time.monotonic()
    assert await solve_challenge_ladder(page, probe_child_frames=True) is False
    assert time.monotonic() - started < 1.0


class _DetachedFrame:
    url = "https://app.example/detached"

    async def frame_element(self) -> None:
        raise PlaywrightError("Frame was detached")


@pytest.mark.asyncio
async def test_a_frame_that_raises_does_not_abort_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    # Frames detach mid-probe on interstitial pages; one probe failing must not cost the frame that holds the gate.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    challenge = FakeFrame(url="https://app.example/challenge-frame", nested_marker=FakeLocator(count=1), visible=True)
    page = FakePage(frames=[_DetachedFrame(), challenge])  # type: ignore[list-item]

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True, (
        "a raising frame must not abort the scan"
    )


@pytest.mark.asyncio
async def test_a_challenge_frame_behind_wedged_frames_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # Frame order must not decide the verdict: wedged frames ahead of the challenge cannot spend the budget.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(captcha_solver_module, "_CHILD_FRAME_SCAN_BUDGET_SECONDS", 0.5)
    challenge = FakeFrame(url="https://app.example/challenge-frame", nested_marker=FakeLocator(count=1), visible=True)
    page = FakePage(frames=[*(_WedgedFrame() for _ in range(4)), challenge])  # type: ignore[list-item]

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True, (
        "a challenge behind wedged frames must be found"
    )


@pytest.mark.asyncio
async def test_child_frames_are_not_probed_unless_the_caller_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default path only reads child frames' URLs; probing their documents for markers stays opt-in
    # (the Task V3 solve_captcha tool), so a marker nested in a non-challenge-host frame reads absent.
    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    child_frame = FakeFrame(
        url="https://app.example/challenge-frame",
        nested_marker=FakeLocator(count=1),
        is_hcaptcha_marker=True,
        visible=True,
    )
    page = FakePage(frames=[child_frame])

    assert await solve_challenge_ladder(page) is False, "default path must not probe child frames"

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
async def test_hcaptcha_arm_bound_cuts_extension_before_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extension arm's timeout must switch on the hCaptcha marker, not stay at the Turnstile-sized
    default: a slow hCaptcha solve inside the hCaptcha bound must survive, but the same slowness on a
    non-hCaptcha page must be cut by the shorter default."""
    monkeypatch.setattr(captcha_solver_module, "_HCAPTCHA_ARM_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(captcha_solver_module, "_EXTENSION_ARM_TIMEOUT_SECONDS", 5)

    async def slow_solve(_page: FakePage) -> bool:
        await asyncio.sleep(1.0)
        return True

    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(side_effect=slow_solve),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    hcaptcha_page = FakePage(hcaptcha=True)
    with pytest.raises(CaptchaChallengeUnsolvedError):
        await solve_challenge_ladder(hcaptcha_page)

    turnstile_page = FakePage(turnstile=True)
    assert await solve_challenge_ladder(turnstile_page) is True


@pytest.mark.asyncio
async def test_extension_arm_bound_falls_back_to_default_when_no_hcaptcha_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the above with the timeouts swapped: the hCaptcha marker must still be what selects
    the longer bound, not incidental ordering."""
    monkeypatch.setattr(captcha_solver_module, "_HCAPTCHA_ARM_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(captcha_solver_module, "_EXTENSION_ARM_TIMEOUT_SECONDS", 0.05)

    async def slow_solve(_page: FakePage) -> bool:
        await asyncio.sleep(1.0)
        return True

    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(side_effect=slow_solve),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    hcaptcha_page = FakePage(hcaptcha=True)
    assert await solve_challenge_ladder(hcaptcha_page) is True

    turnstile_page = FakePage(turnstile=True)
    with pytest.raises(CaptchaChallengeUnsolvedError):
        await solve_challenge_ladder(turnstile_page)


@pytest.mark.asyncio
async def test_token_arm_skipped_when_ladder_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow hCaptcha extension arm on a mixed-vendor page must not leave enough of the ladder's fixed
    budget for the token arm to still run at its full timeout."""
    monkeypatch.setattr(captcha_solver_module, "_LADDER_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(captcha_solver_module, "_HCAPTCHA_ARM_TIMEOUT_SECONDS", 0.2)

    async def slow_solve(_page: FakePage) -> bool:
        await asyncio.sleep(0.25)
        return False

    agent_function = type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(side_effect=slow_solve),
            "solve_recaptcha_token": AsyncMock(return_value=True),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(hcaptcha=True, recaptcha=True)

    with pytest.raises(CaptchaChallengeUnsolvedError):
        await solve_challenge_ladder(page)

    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_ladder_wraps_solve_in_neutral_lifecycle_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    # The direct callers (Task V3, code blocks) reach the vendor solver only through this scope, so it
    # must open once, resolve the extension window INSIDE the open scope, arm the solver, then close —
    # the ordering that lets a deployment widen the window for a solver it armed on scope entry.
    agent_function = ScopeRecordingAgentFunction(auto_solve=True)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    assert await solve_challenge_ladder(FakePage(hcaptcha=True)) is True
    assert agent_function.events == ["enter", "resolve", "solve", "exit"]


@pytest.mark.asyncio
async def test_ladder_lifecycle_scope_closes_when_challenge_unsolved(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even when every arm fails and the ladder raises, the scope must still close on the way out so a
    # vendor solver is never left armed past the solve.
    agent_function = ScopeRecordingAgentFunction(auto_solve=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    with pytest.raises(CaptchaChallengeUnsolvedError):
        await solve_challenge_ladder(FakePage(hcaptcha=True, recaptcha=True))

    assert agent_function.events[0] == "enter"
    assert agent_function.events[-1] == "exit"
    assert "solve" in agent_function.events


@pytest.mark.parametrize("default_timeout", [12.0, 90.0])
def test_base_resolver_returns_default_extension_timeout_unchanged(default_timeout: float) -> None:
    # OSS has no background solver: the resolver hands the ladder's own default (generic 12 / hCaptcha 90) back.
    assert AgentFunction().resolve_captcha_solver_extension_timeout(object(), default_timeout) == default_timeout


class _SlowExtensionSolverAgent(AgentFunction):
    """A solver whose extension arm outlasts the generic bound; the resolver decides whether it gets room to
    finish. ``resolved=None`` returns the default unchanged (an unarmed/OSS deployment)."""

    def __init__(self, *, resolved: float | None) -> None:
        self._resolved = resolved

    def resolve_captcha_solver_extension_timeout(self, page: object, default_timeout: float) -> float:
        return default_timeout if self._resolved is None else self._resolved

    async def auto_solve_captchas(self, page: object) -> bool:
        await asyncio.sleep(0.05)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(("resolved", "solved"), [(0.5, True), (None, False)])
async def test_ladder_honors_resolved_extension_timeout(
    monkeypatch: pytest.MonkeyPatch, resolved: float | None, solved: bool
) -> None:
    # Generic bound shrunk below the solve time: only the deployment-resolved (widened) window lets the slow
    # solver finish; the default cuts it off and the ladder raises. A ladder ignoring the resolver fails here.
    monkeypatch.setattr(captcha_solver_module, "_EXTENSION_ARM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app, "AGENT_FUNCTION", _SlowExtensionSolverAgent(resolved=resolved))
    if solved:
        assert await solve_challenge_ladder(FakePage(turnstile=True)) is True
    else:
        with pytest.raises(CaptchaChallengeUnsolvedError):
            await solve_challenge_ladder(FakePage(turnstile=True))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirm", "expected_events"),
    [
        (True, ["enter", "confirm", "exit"]),
        (False, ["enter", "confirm", "resolve", "solve", "exit"]),
    ],
)
async def test_ladder_anchor_completion_gate_runs_inside_scope(
    monkeypatch: pytest.MonkeyPatch, confirm: bool, expected_events: list[str]
) -> None:
    # The anchor arm's checked+fresh-token exit is gated by is_captcha_solver_completion_confirmed, probed
    # INSIDE the open scope: confirmed exits at the anchor; unconfirmed falls through to the extension solver.
    agent_function = ScopeRecordingAgentFunction(auto_solve=True, confirm=confirm)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    anchor = FakeLocator(count=1)
    parent_frame = FakePage(token_values=["", "opaque-token"])
    page = FakePage(
        recaptcha=True,
        frames=[
            FakeFrame(url="https://www.google.com/recaptcha/api2/anchor", anchor=anchor, parent_frame=parent_frame)
        ],
    )

    assert await solve_challenge_ladder(page) is True
    assert agent_function.events == expected_events


def _has_playwright_browser() -> bool:
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

_AUTO_RENDER_TURNSTILE_HTML = f"""<!DOCTYPE html>
<html><body>
  <form><input id="email" type="email" /><button type="submit">Submit</button></form>
  <div class="cf-turnstile" data-sitekey="0xTESTKEY">
    <iframe src="{CHALLENGE_URL}" style="width:300px;height:65px"></iframe>
  </div>
</body></html>
"""

_CLOSED_SHADOW_TURNSTILE_HTML = f"""<!DOCTYPE html>
<html><body>
  <form><input id="email" type="email" /><button type="submit">Submit</button></form>
  <div id="widget-host"></div>
  <script>
    const root = document.getElementById("widget-host").attachShadow({{mode: "closed"}});
    const f = document.createElement("iframe");
    f.src = "{CHALLENGE_URL}";
    f.style.width = "300px";
    f.style.height = "65px";
    root.appendChild(f);
  </script>
</body></html>
"""


async def _fulfill_challenge(route: Route) -> None:
    await route.fulfill(status=200, content_type="text/html", body="<html><body>Verify you are human</body></html>")


@asynccontextmanager
async def _challenge_browser_page(
    html: str | None = None, *, url: str | None = None, expect_challenge_frame: bool = True
) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        # Production runs headful chromium, where a cross-origin widget frame is an out-of-process iframe;
        # without site isolation a headless probe would never exercise that path.
        browser = await playwright.chromium.launch(headless=True, args=["--site-per-process"])
        try:
            context = await browser.new_context()
            await context.route("https://challenges.cloudflare.com/**", _fulfill_challenge)
            page = await context.new_page()
            if url is not None:
                await page.goto(url, wait_until="load")
            else:
                assert html is not None
                await page.set_content(html, wait_until="load")
            if expect_challenge_frame:
                assert any(urlparse(frame.url).hostname == "challenges.cloudflare.com" for frame in page.frames), (
                    "challenge frame did not commit"
                )
            yield page
        finally:
            await browser.close()


def _stub_solver_agent(*, solves: bool = True) -> AgentFunction:
    return type(
        "AgentFunctionStub",
        (AgentFunction,),
        {
            "auto_solve_captchas": AsyncMock(return_value=solves),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_auto_render_turnstile_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_AUTO_RENDER_TURNSTILE_HTML) as page:
        assert await solve_challenge_ladder(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_closed_shadow_root_turnstile_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Turnstile mounted under a closed shadow root is unreachable by CSS locators; only page.frames sees it.
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_CLOSED_SHADOW_TURNSTILE_HTML) as page:
        assert await page.locator(captcha_solver_module._CAPTCHA_MARKER_SELECTOR).count() == 0
        assert await page.locator(captcha_solver_module._CAPTCHA_CHECKBOX_SELECTOR).count() == 0

        assert await solve_challenge_ladder(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_child_frames", [False, True])
@pytest.mark.parametrize("frame_url", [CHALLENGE_URL, CHALLENGE_SUBDOMAIN_URL])
async def test_visible_cloudflare_challenge_frame_reaches_extension_arm(
    monkeypatch: pytest.MonkeyPatch, probe_child_frames: bool, frame_url: str
) -> None:
    """A Turnstile widget can mount where no CSS locator reaches it, so a visible frame on the challenge
    host is itself the presence signal, for both callers."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[FakeFrame(url=frame_url, visible=True)])

    assert await solve_challenge_ladder(page, probe_child_frames=probe_child_frames) is True

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_builtin_reaches_extension_arm_for_a_visible_cloudflare_challenge_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[FakeFrame(url=CHALLENGE_URL, visible=True)])

    assert await block_module._code_block_solve_captcha_builtin(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame_url", "visible", "box"),
    [
        ("https://evil.example.com/?next=challenges.cloudflare.com", True, None),
        ("https://challenges.cloudflare.com.evil.example/", True, None),
        (CHALLENGE_URL, False, None),
        (CHALLENGE_URL, True, {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}),
        (CHALLENGE_URL, True, {"x": -9999.0, "y": 0.0, "width": 300.0, "height": 65.0}),
    ],
)
async def test_decoy_and_hidden_cloudflare_frames_stay_absent(
    monkeypatch: pytest.MonkeyPatch, frame_url: str, visible: bool, box: dict[str, float] | None
) -> None:
    """The host must be matched on the parsed hostname, and Turnstile's hidden helper frames, its
    invisible-mode 1x1 frame, and a frame parked off-screen sit on the real host, so those must keep
    reading absent."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[FakeFrame(url=frame_url, visible=visible, box=box)])

    assert await solve_challenge_ladder(page) is False

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("frame_x", "present"), [(100.0, True), (5000.0, False)])
async def test_challenge_frame_is_judged_on_screen_when_the_page_reports_no_viewport(
    monkeypatch: pytest.MonkeyPatch, frame_x: float, present: bool
) -> None:
    """A display-fitted browser context reports no viewport size, so the configured browser size stands in."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(
        frames=[
            FakeFrame(url=CHALLENGE_URL, visible=True, box={"x": frame_x, "y": 0.0, "width": 300.0, "height": 65.0})
        ]
    )
    page.viewport_size = None

    assert await solve_challenge_ladder(page) is present


@pytest.mark.asyncio
async def test_an_uncommitted_challenge_frame_matches_on_its_iframe_src(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-origin challenge frame reports an empty URL until its navigation commits, while the iframe
    element's src already names the challenge host."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[FakeFrame(url="", src=CHALLENGE_URL, visible=True)])

    assert await solve_challenge_ladder(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once_with(page)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example.com/?next=challenges.cloudflare.com",
        "https://challenges.cloudflare.com.evil.example/",
    ],
)
async def test_an_uncommitted_frame_with_a_decoy_src_stays_absent(monkeypatch: pytest.MonkeyPatch, src: str) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[FakeFrame(url="about:blank", src=src, visible=True)])

    assert await solve_challenge_ladder(page) is False

    agent_function.auto_solve_captchas.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_uncommitted_frame_with_a_foreign_src_is_still_selector_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The src probe must not short-circuit an uncommitted frame out of the marker scan it would otherwise
    get."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    frame = FakeFrame(
        url="about:blank", src="https://cdn.example/widget", nested_marker=FakeLocator(count=1), visible=True
    )
    page = FakePage(frames=[frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True


@pytest.mark.asyncio
async def test_a_committed_non_challenge_frame_is_never_probed_for_its_src(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the src attribute is a round trip per frame; only frames with no committed URL may pay it."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    frame = FakeFrame(url="https://app.example/embed", nested_marker=FakeLocator(count=1), visible=True)
    page = FakePage(frames=[frame])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True
    assert frame.element.get_attribute_calls == []


class _DetachedChallengeFrame(_DetachedFrame):
    url = CHALLENGE_URL


@pytest.mark.asyncio
async def test_a_raising_challenge_frame_does_not_hide_a_visible_one(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    page = FakePage(frames=[_DetachedChallengeFrame(), FakeFrame(url=CHALLENGE_URL, visible=True)])  # type: ignore[list-item]

    assert await solve_challenge_ladder(page) is True


class _WedgedChallengeFrame(_WedgedFrame):
    url = CHALLENGE_URL


@pytest.mark.asyncio
async def test_wedged_challenge_frames_are_bounded_on_the_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(captcha_solver_module, "_CHILD_FRAME_SCAN_BUDGET_SECONDS", 0.3)
    page = FakePage(frames=[_WedgedChallengeFrame() for _ in range(5)])

    started = time.monotonic()
    assert await solve_challenge_ladder(page) is False
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_the_opted_in_absent_path_runs_one_frame_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two composed scans (challenge-host frames, then markers) would each spend the whole budget.
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(captcha_solver_module, "_CHILD_FRAME_SCAN_BUDGET_SECONDS", 0.3)
    page = FakePage(frames=[_WedgedChallengeFrame()])

    started = time.monotonic()
    assert await solve_challenge_ladder(page, probe_child_frames=True) is False
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_visible_challenge_frame_keeps_the_turnstile_extension_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Turnstile page must not inherit the hCaptcha image-challenge bound: it would hold a failing
    extension arm open for a minute and a half."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    resolve_calls: list[float] = []
    monkeypatch.setattr(
        agent_function,
        "resolve_captcha_solver_extension_timeout",
        lambda _page, default_timeout: resolve_calls.append(default_timeout) or default_timeout,
    )
    page = FakePage(frames=[FakeFrame(url=CHALLENGE_URL, visible=True)])

    assert await solve_challenge_ladder(page, probe_child_frames=True) is True
    assert resolve_calls == [captcha_solver_module._EXTENSION_ARM_TIMEOUT_SECONDS]


_FAKE_CAPTCHA_SITE = Path(__file__).resolve().parents[2] / "dev_scripts" / "fake_captcha_site"

_UNRENDERED_RECAPTCHA_HTML = """<!DOCTYPE html>
<html><body>
  <form>
    <input id="email" type="email" />
    <div class="g-recaptcha" data-sitekey="6LcFixtureKey"></div>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""

_HIDDEN_V3_BADGE_HTML = """<!DOCTYPE html>
<html><body>
  <form>
    <input id="email" type="email" />
    <button type="submit">Submit</button>
  </form>
  <div class="grecaptcha-badge" style="visibility:hidden;position:fixed;right:0;bottom:14px">
    <iframe title="reCAPTCHA" width="256" height="60"></iframe>
  </div>
</body></html>
"""

_BELOW_THE_FOLD_RECAPTCHA_HTML = """<!DOCTYPE html>
<html><body>
  <form>
    <input id="email" type="email" />
    <div style="height:3000px"></div>
    <div class="g-recaptcha" data-sitekey="6LcFixtureKey" style="width:304px;height:78px;background:#eee"></div>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""

_HIDDEN_THEN_RENDERED_RECAPTCHA_HTML = """<!DOCTYPE html>
<html><body>
  <form>
    <div class="g-recaptcha" data-sitekey="6LcStaleKey" style="display:none"></div>
    <div class="g-recaptcha" data-sitekey="6LcLiveKey" style="width:304px;height:78px;background:#eee"></div>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""

_RENDERED_TURNSTILE_WITH_HIDDEN_HCAPTCHA_HTML = """<!DOCTYPE html>
<html><body>
  <form>
    <div class="cf-turnstile" data-sitekey="0xTESTKEY" style="width:300px;height:65px;background:#eee"></div>
    <div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001" style="display:none"></div>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_invisible_hcaptcha_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An hCaptcha the site loaded in invisible mode and has not executed is not a gate the run must clear."""
    agent_function = _stub_solver_agent(solves=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(
        url=(_FAKE_CAPTCHA_SITE / "invisible_hcaptcha.html").as_uri(), expect_challenge_frame=False
    ) as page:
        assert await page.locator(captcha_solver_module._CAPTCHA_MARKER_SELECTOR).count() > 0

        assert await solve_challenge_ladder(page) is False

    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_invisible_hcaptcha_page_submits_after_the_absent_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the absent verdict: the run goes on to Submit, and the site runs its own invisible
    challenge from there."""
    monkeypatch.setattr(app, "AGENT_FUNCTION", _stub_solver_agent(solves=False))

    async with _challenge_browser_page(
        url=(_FAKE_CAPTCHA_SITE / "invisible_hcaptcha.html").as_uri(), expect_challenge_frame=False
    ) as page:
        await page.fill("#full-name", "Sample Applicant")
        assert await solve_challenge_ladder(page) is False

        await page.click("#apply-submit")
        await page.wait_for_url("**/invisible_hcaptcha_results.html*")

        assert "h-captcha-response=fixture-invisible-token" in page.url
        assert "Application received" in await page.locator("#confirmation").inner_text()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_unrendered_recaptcha_container_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent(solves=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_UNRENDERED_RECAPTCHA_HTML, expect_challenge_frame=False) as page:
        assert await page.locator(captcha_solver_module._CAPTCHA_MARKER_SELECTOR).count() > 0

        assert await solve_challenge_ladder(page) is False

    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_hidden_recaptcha_badge_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hidden v3 badge is a marker the site never shows, so it reads absent and the solver arms stay
    unused; the run goes on to Submit and the site scores it itself."""
    agent_function = _stub_solver_agent(solves=False)
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_HIDDEN_V3_BADGE_HTML, expect_challenge_frame=False) as page:
        assert await page.locator(captcha_solver_module._CAPTCHA_MARKER_SELECTOR).count() > 0

        assert await solve_challenge_ladder(page, probe_child_frames=True) is False

    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_marker_below_the_fold_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A widget a long form scrolls to is a real gate, so page-level presence must not require the viewport."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_BELOW_THE_FOLD_RECAPTCHA_HTML, expect_challenge_frame=False) as page:
        assert await solve_challenge_ladder(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_rendered_marker_after_a_hidden_one_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    async with _challenge_browser_page(_HIDDEN_THEN_RENDERED_RECAPTCHA_HTML, expect_challenge_frame=False) as page:
        assert await solve_challenge_ladder(page) is True

    agent_function.auto_solve_captchas.assert_awaited_once()


@_skip_no_browser
@pytest.mark.asyncio
async def test_browser_hidden_hcaptcha_beside_a_rendered_turnstile_keeps_the_hcaptcha_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arm selection reads raw marker counts on purpose: once presence is settled, a page carrying any
    hCaptcha still needs the longer image-challenge bound."""
    agent_function = _stub_solver_agent()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    resolve_calls: list[float] = []
    monkeypatch.setattr(
        agent_function,
        "resolve_captcha_solver_extension_timeout",
        lambda _page, default_timeout: resolve_calls.append(default_timeout) or default_timeout,
    )

    async with _challenge_browser_page(
        _RENDERED_TURNSTILE_WITH_HIDDEN_HCAPTCHA_HTML, expect_challenge_frame=False
    ) as page:
        assert await solve_challenge_ladder(page) is True

    assert resolve_calls == [captcha_solver_module._HCAPTCHA_ARM_TIMEOUT_SECONDS]
