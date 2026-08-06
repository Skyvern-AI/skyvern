from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockCaptchaError


class FakeLocator:
    def __init__(self, *, count: int = 0, checked: bool = False, input_values: list[str] | None = None) -> None:
        self._count = count
        self._checked = checked
        self._input_values = list(input_values or [])
        self._input_value_calls = 0
        self.click = AsyncMock(side_effect=self._click)

    async def _click(self) -> None:
        self._checked = True

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return True

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


class FakeFrame:
    def __init__(
        self,
        *,
        url: str,
        anchor: FakeLocator,
        parent_frame: FakePage | None = None,
        detached: bool = False,
    ) -> None:
        self.url = url
        self.anchor = anchor
        self.parent_frame = parent_frame
        self.detached = detached

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "#recaptcha-anchor"
        return self.anchor

    def is_detached(self) -> bool:
        return self.detached


class FakePage:
    def __init__(
        self,
        *,
        checkbox: bool = False,
        recaptcha: bool = False,
        token_values: list[str] | None = None,
        frames: list[FakeFrame] | None = None,
        url: str = "https://app.example/login",
    ) -> None:
        self.checkbox = FakeLocator(count=1 if checkbox else 0)
        self.challenge = FakeLocator(count=1 if recaptcha else 0)
        self.recaptcha_token = FakeLocator(count=1 if token_values is not None else 0, input_values=token_values)
        self.continue_button = FakeLocator(count=1 if checkbox else 0)
        self.continue_button.click = AsyncMock(side_effect=self._continue)
        self.frames = list(frames or [])
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
        return self.challenge

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        await asyncio.sleep(0)

    async def evaluate(self, expression: str, *_args: object) -> None:
        self.evaluated.append(expression)


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_is_fast_noop_without_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (),
        {
            "auto_solve_captchas": AsyncMock(return_value=False),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)

    block = CodeBlock.model_construct(code="await solve_captcha(page)", label="captcha_noop")
    fn = block.generate_async_user_function(block.code, FakePage())

    await fn()

    agent_function.auto_solve_captchas.assert_not_awaited()
    agent_function.solve_recaptcha_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sandbox_solve_captcha_clicks_unique_structural_checkbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_function = type(
        "AgentFunctionStub",
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
        (),
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
    page.recaptcha_token.input_value = AsyncMock(
        side_effect=[block_module.PlaywrightError("probe failed"), "opaque-token"]
    )
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
        (),
        {
            "auto_solve_captchas": AsyncMock(return_value=True),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(block_module, "_CODE_BLOCK_RECAPTCHA_ANCHOR_ARM_TIMEOUT_SECONDS", 0.01)

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
        (),
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
        (),
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
        (),
        {
            "auto_solve_captchas": AsyncMock(side_effect=_never_returns),
            "solve_recaptcha_token": AsyncMock(return_value=False),
        },
    )()
    monkeypatch.setattr(app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(block_module, "_CODE_BLOCK_EXTENSION_ARM_TIMEOUT_SECONDS", 0.01)
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
        (),
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
        (),
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
