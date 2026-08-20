"""Regression tests for Agent OTP routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.agent import ForgeAgent, PromptBuildResult, _model_is_abandoning_verification
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

    resolver.assert_awaited_once_with(task, expected_otp_type=OTPType.TOTP)
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

    resolver.assert_awaited_once_with(task, expected_otp_type=OTPType.TOTP)
    poll.assert_not_awaited()
    db_get.assert_not_awaited()
    rescrape.assert_awaited_once()
    assert result == {"actions": []}


@pytest.mark.asyncio
async def test_handle_potential_verification_code_resolves_with_should_enter_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-sink regression (calls the real sink, not a mock): with a TOTP source configured and
    place_to_enter_verification_code=True, the sink must resolve and re-plan even when
    should_enter_verification_code=False. Fails if the old inner ``(place and should_enter)`` gate is
    restored — proving the guard removal is load-bearing, not mock theater."""
    task = _make_task()
    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
    }

    resolved_code = OTPValue(value="123456", type=OTPType.TOTP)
    resolver = AsyncMock(return_value=resolved_code)
    poll = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_otp_value", resolver)
    monkeypatch.setattr("skyvern.forge.agent.poll_otp_value", poll)

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

    rescrape = AsyncMock(return_value={"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]})
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

    resolver.assert_awaited_once_with(task, expected_otp_type=OTPType.TOTP)
    rescrape.assert_awaited_once()
    assert result == {"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "give_up_actions",
    [
        pytest.param(
            [
                {
                    "action_type": "TERMINATE",
                    "reasoning": "OTP prompt remains; terminate per timeout policy.",
                    "user_detail_answer": "OTP_TIMEOUT",
                }
            ],
            id="terminate",
        ),
        pytest.param(
            [{"action_type": "WAIT", "reasoning": "Waiting for the verification code to arrive."}],
            id="lone_wait",
        ),
        pytest.param(
            [
                {"action_type": "WAIT", "reasoning": "Waiting for the verification code to arrive."},
                {
                    "action_type": "TERMINATE",
                    "reasoning": "OTP prompt remains; terminate per timeout policy.",
                    "user_detail_answer": "OTP_TIMEOUT",
                },
            ],
            id="wait_plus_terminate",
        ),
    ],
)
async def test_handle_potential_OTP_actions_resolves_before_model_give_up(
    monkeypatch: pytest.MonkeyPatch,
    give_up_actions: list,
) -> None:
    """A model give-up on the verification page (place_to_enter_verification_code=True,
    should_enter_verification_code=False, TOTP source configured) must route into the resolver
    instead of being honored — for both a TERMINATE and a lone WAIT — so the 15-minute poll budget
    is reachable by construction."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": False,
        "actions": give_up_actions,
    }

    resolved_response = {"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]}
    hpvc = AsyncMock(return_value=resolved_response)
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)
    parsed_sentinel = [object()]
    parse_mock = MagicMock(return_value=parsed_sentinel)
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", parse_mock)
    monkeypatch.setattr("skyvern.forge.agent.stamp_parsed_actions", MagicMock())

    agent = ForgeAgent.__new__(ForgeAgent)
    _returned_response, actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_awaited_once()
    assert actions == parsed_sentinel
    # The resolver's rebuilt response is what gets parsed, not the model's give-up action.
    assert parse_mock.call_args.args[4] == resolved_response["actions"]


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param([{"action_type": "CLICK", "id": "AAAA"}], id="lone_click"),
        # Canonical parse_actions keeps a mixed TERMINATE+CLICK batch, so forcing the resolver would
        # suppress the productive CLICK. Only a PURE give-up may route into the resolver.
        pytest.param(
            [{"action_type": "TERMINATE", "reasoning": "x"}, {"action_type": "CLICK", "id": "AAAA"}],
            id="terminate_plus_click",
        ),
        pytest.param(
            [{"action_type": "WAIT", "reasoning": "x"}, {"action_type": "CLICK", "id": "AAAA"}], id="wait_plus_click"
        ),
    ],
)
@pytest.mark.asyncio
async def test_handle_potential_OTP_actions_skips_resolver_for_non_pure_giveup(
    monkeypatch: pytest.MonkeyPatch,
    actions: list,
) -> None:
    """The guardrail must fire only on a PURE give-up. A productive action alone, or mixed in with a
    TERMINATE/WAIT (place_to_enter_verification_code=True, should_enter_verification_code=False), must
    not force a premature poll — it falls through to normal action parsing."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": False,
        "actions": actions,
    }

    hpvc = AsyncMock()
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)

    agent = ForgeAgent.__new__(ForgeAgent)
    _returned_response, returned_actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_not_awaited()
    assert returned_actions == []


@pytest.mark.parametrize(
    "actions, expected",
    [
        pytest.param([{"action_type": "TERMINATE", "reasoning": "x"}], True, id="pure_terminate"),
        pytest.param(
            [{"action_type": "terminate", "reasoning": "x"}, {"action_type": "TERMINATE", "reasoning": "y"}],
            True,
            id="multi_terminate",
        ),
        pytest.param([{"action_type": "WAIT", "reasoning": "x"}], True, id="lone_wait"),
        pytest.param([{"action_type": "WAIT"}, {"action_type": "WAIT"}], True, id="multi_wait"),
        pytest.param(
            [{"action_type": "TERMINATE"}, {"action_type": "CLICK", "id": "A"}], False, id="terminate_plus_click"
        ),
        pytest.param([{"action_type": "WAIT"}, {"action_type": "CLICK", "id": "A"}], False, id="wait_plus_click"),
        # _execute_step_actions drops WAIT from a mixed batch, so WAIT+TERMINATE really executes as a
        # lone TERMINATE and must classify as abandonment (order-independent).
        pytest.param(
            [{"action_type": "WAIT"}, {"action_type": "TERMINATE", "reasoning": "x"}], True, id="wait_plus_terminate"
        ),
        pytest.param(
            [{"action_type": "TERMINATE", "reasoning": "x"}, {"action_type": "WAIT"}], True, id="terminate_plus_wait"
        ),
        pytest.param([{"action_type": "CLICK", "id": "A"}], False, id="lone_click"),
        pytest.param([], False, id="empty"),
    ],
)
def test_model_is_abandoning_verification_pure_batch_only(actions: list, expected: bool) -> None:
    """Abandonment is judged on the actions that will actually execute. A pure TERMINATE or pure WAIT
    batch is abandonment; because _execute_step_actions drops WAIT from a mixed batch, WAIT+TERMINATE
    (either order) also executes as a lone TERMINATE and is abandonment. A TERMINATE or WAIT mixed
    with a productive action (which survives execution) is not abandonment."""
    assert _model_is_abandoning_verification({"actions": actions}) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "give_up_actions",
    [
        pytest.param([{"action_type": "TERMINATE", "reasoning": "give up"}], id="terminate"),
        pytest.param([{"action_type": "WAIT", "reasoning": "waiting"}], id="lone_wait"),
    ],
)
async def test_handle_potential_OTP_actions_magic_link_wins_over_give_up_fallback(
    monkeypatch: pytest.MonkeyPatch,
    give_up_actions: list,
) -> None:
    """When magic-link verification is requested (should_verify_by_magic_link=True) but
    should_enter_verification_code=False, a model give-up must NOT be hijacked by the
    verification-code resolver — the established magic-link path runs instead."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": True,
        "actions": give_up_actions,
    }

    # Give the (wrong) verification-code branch harmless stubs so, if it is taken, the test fails on
    # the explicit assertion below rather than on an incidental downstream error.
    hpvc = AsyncMock(return_value={"actions": []})
    magic = AsyncMock(return_value=["magic_link_action"])
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)
    monkeypatch.setattr(ForgeAgent, "handle_potential_magic_link", magic)
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", MagicMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.stamp_parsed_actions", MagicMock())

    agent = ForgeAgent.__new__(ForgeAgent)
    returned_response, actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_not_awaited()
    magic.assert_awaited_once()
    assert actions == ["magic_link_action"]
    assert returned_response == json_response
