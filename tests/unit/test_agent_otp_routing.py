"""Regression tests for normal Agent OTP routing.

Covers:
- handle_potential_verification_code delegates to resolve_otp_value without a
  pre-resolver DB roundtrip.
- With a webhook-configured task, the resolver returns a credential-backed TOTP
  before polling is attempted (the actual SKY-9178 customer scenario).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.agent import ForgeAgent, PromptBuildResult
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import OTPValue


def _make_task(
    *,
    totp_verification_url: str | None = "https://example.com/webhook",
    totp_identifier: str | None = "user@example.com",
    navigation_payload: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="tsk_test",
        organization_id="o_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
        navigation_payload=navigation_payload,
        url="https://example.com",
        navigation_goal="log in",
        llm_key=None,
        workflow_system_prompt=None,
    )


@pytest.mark.asyncio
async def test_handle_potential_verification_code_uses_resolver_without_db_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(navigation_payload={"otp_code": "654321"})
    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
    }

    resolver = AsyncMock(return_value=None)
    db_get = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_otp_value", resolver)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run", db_get)

    agent = ForgeAgent.__new__(ForgeAgent)
    await agent.handle_potential_verification_code(task, step, scraped_page, browser_state, json_response)

    resolver.assert_awaited_once_with(task)
    db_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_potential_verification_code_skips_polling_when_credential_returns_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKY-9178 regression: when both webhook URL and credential TOTP are configured,
    the resolver yields the credential code first and webhook polling is never invoked."""
    task = _make_task()
    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
    }

    credential_code = OTPValue(value="123456", type=OTPType.TOTP)
    resolver = AsyncMock(return_value=credential_code)
    poll = AsyncMock()
    db_get = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_otp_value", resolver)
    monkeypatch.setattr("skyvern.forge.agent.poll_otp_value", poll)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run", db_get)

    rebuilt = AsyncMock(
        return_value=PromptBuildResult(
            prompt="prompt",
            use_caching=False,
            prompt_name="prompt_name",
            without_page_information=False,
        )
    )
    monkeypatch.setattr(ForgeAgent, "_build_extract_action_prompt", rebuilt)
    monkeypatch.setattr("skyvern.forge.agent.service_utils.is_cua_task", AsyncMock(return_value=False))

    rescrape = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *args, **kwargs: rescrape,
    )

    agent = ForgeAgent.__new__(ForgeAgent)
    agent.async_operation_pool = MagicMock()

    skyvern_context.set(SkyvernContext(task_id=task.task_id))
    try:
        result = await agent.handle_potential_verification_code(task, step, scraped_page, browser_state, json_response)
    finally:
        skyvern_context.reset()

    resolver.assert_awaited_once_with(task)
    poll.assert_not_awaited()
    db_get.assert_not_awaited()
    rescrape.assert_awaited_once()
    assert result == {"actions": []}
