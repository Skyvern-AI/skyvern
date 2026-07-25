from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockCaptchaError


class FakeLocator:
    def __init__(self, *, count: int = 0, checked: bool = False) -> None:
        self._count = count
        self._checked = checked
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

    @property
    def first(self) -> FakeLocator:
        return self


class FakePage:
    def __init__(self, *, checkbox: bool = False, recaptcha: bool = False) -> None:
        self.checkbox = FakeLocator(count=1 if checkbox else 0)
        self.challenge = FakeLocator(count=1 if recaptcha else 0)
        self.continue_button = FakeLocator(count=1 if checkbox else 0)
        self.continue_button.click = AsyncMock(side_effect=self._continue)

    async def _continue(self) -> None:
        self.checkbox._count = 0
        self.challenge._count = 0

    def locator(self, selector: str) -> FakeLocator:
        if "checkbox" in selector:
            return self.checkbox
        if "button" in selector:
            return self.continue_button
        return self.challenge

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


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
    )


def test_solve_captcha_is_reserved_in_sandbox_namespace() -> None:
    assert "solve_captcha" in CodeBlock.build_safe_vars()
