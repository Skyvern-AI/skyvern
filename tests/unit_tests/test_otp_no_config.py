"""Tests for manual 2FA input without pre-configured TOTP credentials (SKY-6)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import SPECIAL_FIELD_VERIFICATION_CODE
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.notification.local import LocalNotificationRegistry
from skyvern.forge.sdk.routes.credentials import send_totp_code
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCodeCreate
from skyvern.schemas.runs import RunEngine
from skyvern.services.otp_service import (
    OTPResultParsedByLLM,
    OTPValue,
    _get_otp_value_by_run,
    extract_totp_from_navigation_inputs,
    poll_otp_value,
)


@pytest.mark.asyncio
async def test_get_otp_codes_by_run_exists():
    """get_otp_codes_by_run should exist on AgentDB."""
    assert hasattr(AgentDB, "get_otp_codes_by_run"), "AgentDB missing get_otp_codes_by_run method"


@pytest.mark.asyncio
async def test_get_otp_codes_by_run_returns_empty_without_identifiers():
    """get_otp_codes_by_run should return [] when neither task_id nor workflow_run_id is given."""
    db = AgentDB.__new__(AgentDB)
    result = await db.get_otp_codes_by_run(
        organization_id="org_1",
    )
    assert result == []


# === Fix: OTPResultParsedByLLM empty otp_type coercion ===


def test_otp_result_parsed_by_llm_empty_string_coerced_to_none():
    """OTPResultParsedByLLM should coerce empty otp_type to None instead of raising ValidationError."""
    result = OTPResultParsedByLLM(
        reasoning="No OTP found in content",
        otp_type="",
        otp_value_found=False,
        otp_value=None,
    )
    assert result.otp_type is None


def test_otp_result_parsed_by_llm_whitespace_string_coerced_to_none():
    """OTPResultParsedByLLM should coerce whitespace-only otp_type to None."""
    result = OTPResultParsedByLLM(
        reasoning="No OTP found",
        otp_type="   ",
        otp_value_found=False,
        otp_value=None,
    )
    assert result.otp_type is None


def test_otp_result_parsed_by_llm_valid_otp_type_unchanged():
    """OTPResultParsedByLLM should accept valid otp_type values without modification."""
    result = OTPResultParsedByLLM(
        reasoning="Found TOTP code",
        otp_type="totp",
        otp_value_found=True,
        otp_value="123456",
    )
    assert result.otp_type == OTPType.TOTP


# === Task 2: _get_otp_value_by_run OTP service function ===


@pytest.mark.asyncio
async def test_get_otp_value_by_run_returns_code():
    """_get_otp_value_by_run should find OTP codes by task_id."""
    mock_code = MagicMock()
    mock_code.code = "123456"
    mock_code.otp_type = "totp"

    mock_db = AsyncMock()
    mock_db.get_otp_codes_by_run.return_value = [mock_code]

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with patch("skyvern.services.otp_service.app", new=mock_app):
        result = await _get_otp_value_by_run(
            organization_id="org_1",
            task_id="tsk_1",
        )
    assert result is not None
    assert result.value == "123456"


@pytest.mark.asyncio
async def test_get_otp_value_by_run_returns_none_when_no_codes():
    """_get_otp_value_by_run should return None when no codes found."""
    mock_db = AsyncMock()
    mock_db.get_otp_codes_by_run.return_value = []

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with patch("skyvern.services.otp_service.app", new=mock_app):
        result = await _get_otp_value_by_run(
            organization_id="org_1",
            task_id="tsk_1",
        )
    assert result is None


# === Task: extract_totp_from_navigation_inputs (LLM-based) ===


@pytest.mark.asyncio
async def test_extract_totp_returns_none_for_none_payload():
    """None payload should short-circuit without calling LLM."""
    result = await extract_totp_from_navigation_inputs(None, "org_1")
    assert result is None


@pytest.mark.asyncio
async def test_extract_totp_returns_none_for_empty_string_payload():
    """Empty string payload should short-circuit without calling LLM."""
    result = await extract_totp_from_navigation_inputs("", "org_1")
    assert result is None


@pytest.mark.asyncio
async def test_extract_totp_returns_none_without_organization_id():
    """Missing organization_id should short-circuit without calling LLM."""
    result = await extract_totp_from_navigation_inputs({"otp": "123456"}, None)
    assert result is None


@pytest.mark.asyncio
async def test_extract_totp_serializes_dict_to_json_for_llm():
    """Dict payload should be JSON-serialized and passed to parse_otp_login."""
    payload = {"mfaChoice": "520265", "other": "data"}
    expected_otp = OTPValue(value="520265", type=OTPType.TOTP)

    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=expected_otp,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs(payload, "org_1")

    assert result == expected_otp
    mock_parse.assert_called_once()
    call_args = mock_parse.call_args
    assert call_args[0][0] == '{"mfaChoice": "520265", "other": "data"}'
    assert call_args[0][1] == "org_1"


@pytest.mark.asyncio
async def test_extract_totp_serializes_list_to_json_for_llm():
    """List payload should be JSON-serialized and passed to parse_otp_login."""
    payload = [{"otp": "654321"}]
    expected_otp = OTPValue(value="654321", type=OTPType.TOTP)

    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=expected_otp,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs(payload, "org_1")

    assert result == expected_otp
    mock_parse.assert_called_once()
    call_args = mock_parse.call_args
    assert call_args[0][0] == '[{"otp": "654321"}]'


@pytest.mark.asyncio
async def test_extract_totp_passes_string_payload_directly():
    """String payload should be passed as-is to parse_otp_login."""
    payload = "Your verification code is 123456"
    expected_otp = OTPValue(value="123456", type=OTPType.TOTP)

    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=expected_otp,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs(payload, "org_1")

    assert result == expected_otp
    mock_parse.assert_called_once_with(payload, "org_1")


@pytest.mark.asyncio
async def test_extract_totp_returns_none_when_llm_finds_nothing():
    """Should return None when parse_otp_login returns None."""
    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await extract_totp_from_navigation_inputs({"data": "no code here"}, "org_1")

    assert result is None


@pytest.mark.asyncio
async def test_extract_totp_returns_none_on_llm_exception():
    """Should catch LLM exceptions and return None gracefully."""
    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        side_effect=Exception("LLM unavailable"),
    ):
        result = await extract_totp_from_navigation_inputs({"otp": "123456"}, "org_1")

    assert result is None


# === navigation_goal support ===


@pytest.mark.asyncio
async def test_extract_totp_from_navigation_goal_text():
    """Should find OTP code embedded in navigation_goal text when payload is empty."""
    expected_otp = OTPValue(value="522225", type=OTPType.TOTP)

    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=expected_otp,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs(
            {}, "org_1", navigation_goal="GOAL: Submit MFA code\n- Input 522225."
        )

    assert result == expected_otp
    mock_parse.assert_called_once()
    content = mock_parse.call_args[0][0]
    assert "522225" in content
    assert "GOAL: Submit MFA code" in content


@pytest.mark.asyncio
async def test_extract_totp_goal_only_no_payload():
    """Should extract OTP from navigation_goal when payload is None."""
    expected_otp = OTPValue(value="123456", type=OTPType.TOTP)

    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=expected_otp,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs(None, "org_1", navigation_goal="Enter code 123456")

    assert result == expected_otp
    mock_parse.assert_called_once()


@pytest.mark.asyncio
async def test_extract_totp_returns_none_when_both_empty():
    """Should return None without calling LLM when payload is None and goal is None."""
    result = await extract_totp_from_navigation_inputs(None, "org_1", navigation_goal=None)
    assert result is None


@pytest.mark.asyncio
async def test_extract_totp_returns_none_when_payload_empty_dict_and_no_goal():
    """Should still call LLM for empty dict payload (non-empty JSON string '{}')."""
    with patch(
        "skyvern.services.otp_service.parse_otp_login",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_parse:
        result = await extract_totp_from_navigation_inputs({}, "org_1", navigation_goal=None)

    assert result is None
    mock_parse.assert_called_once()


# === Task 3: poll_otp_value without identifier ===


@pytest.mark.asyncio
async def test_poll_otp_value_without_identifier_uses_run_lookup():
    """poll_otp_value should use _get_otp_value_by_run when no identifier/URL provided."""
    mock_code = MagicMock()
    mock_code.code = "123456"
    mock_code.otp_type = "totp"

    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = MagicMock(token="tok")
    mock_db.get_otp_codes_by_run.return_value = [mock_code]
    mock_db.update_task_2fa_state = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await poll_otp_value(
            organization_id="org_1",
            task_id="tsk_1",
        )
    assert result is not None
    assert result.value == "123456"


# === Task 6: Integration test — handle_potential_OTP_actions without TOTP config ===


@pytest.mark.asyncio
async def test_handle_potential_OTP_actions_without_totp_config():
    """When LLM detects 2FA but no TOTP config exists, should still enter verification flow."""
    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.organization_id = "org_1"
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_1"
    task.workflow_run_id = None

    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()

    json_response = {
        "should_enter_verification_code": True,
        "place_to_enter_verification_code": "input#otp-code",
        "actions": [],
    }

    with patch.object(agent, "handle_potential_verification_code", new_callable=AsyncMock) as mock_handler:
        mock_handler.return_value = {"actions": []}
        with patch("skyvern.forge.agent.parse_actions", return_value=[]):
            result_json, result_actions = await agent.handle_potential_OTP_actions(
                task, step, scraped_page, browser_state, json_response
            )
        mock_handler.assert_called_once()


@pytest.mark.asyncio
async def test_handle_potential_verification_code_uses_navigation_payload_and_skips_poll():
    """When payload includes MFA code, should consume it and skip poll_otp_value."""
    from skyvern.forge import agent as forge_agent_module

    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.organization_id = "org_1"
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_payload_code"
    task.workflow_run_id = None
    task.navigation_payload = {"mfaChoice": "520265"}
    task.llm_key = None

    step = MagicMock()
    step.step_id = "step_1"
    step.order = 0

    scraped_page = MagicMock()
    scraped_page.screenshots = []
    browser_state = MagicMock()
    json_response = {
        "should_enter_verification_code": True,
        "place_to_enter_verification_code": "input#otp-code",
    }

    original_app_inst = object.__getattribute__(forge_agent_module.app, "_inst")
    object.__setattr__(forge_agent_module.app, "_inst", MagicMock(LLM_API_HANDLER=AsyncMock()))
    try:
        with (
            patch(
                "skyvern.forge.agent.extract_totp_from_navigation_inputs",
                new_callable=AsyncMock,
                return_value=OTPValue(value="520265", type=OTPType.TOTP),
            ) as mock_extract,
            patch("skyvern.forge.agent.poll_otp_value", new_callable=AsyncMock) as mock_poll,
            patch("skyvern.forge.agent.skyvern_context") as mock_skyvern_context,
            patch("skyvern.forge.agent.service_utils.is_cua_task", new_callable=AsyncMock, return_value=False),
            patch(
                "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
                return_value=AsyncMock(return_value={"actions": []}),
            ),
            patch.object(
                agent,
                "_build_extract_action_prompt",
                new_callable=AsyncMock,
                return_value=("prompt", False, "extract-actions"),
            ),
        ):
            mock_context = MagicMock()
            mock_context.totp_codes = {}
            mock_skyvern_context.ensure_context.return_value = mock_context
            mock_skyvern_context.current.return_value = None

            await agent.handle_potential_verification_code(
                task=task,
                step=step,
                scraped_page=scraped_page,
                browser_state=browser_state,
                json_response=json_response,
            )
    finally:
        object.__setattr__(forge_agent_module.app, "_inst", original_app_inst)

    mock_extract.assert_called_once_with({"mfaChoice": "520265"}, "org_1", navigation_goal=task.navigation_goal)
    mock_poll.assert_not_called()
    assert mock_context.totp_codes["tsk_payload_code"] == "520265"


@pytest.mark.asyncio
async def test_handle_potential_OTP_actions_skips_magic_link_without_totp_config():
    """Magic links should still require TOTP config."""
    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.organization_id = "org_1"
    task.totp_verification_url = None
    task.totp_identifier = None

    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()

    json_response = {
        "should_verify_by_magic_link": True,
    }

    with patch.object(agent, "handle_potential_magic_link", new_callable=AsyncMock) as mock_handler:
        result_json, result_actions = await agent.handle_potential_OTP_actions(
            task, step, scraped_page, browser_state, json_response
        )
        mock_handler.assert_not_called()
    assert result_actions == []


# === Task 7: verification_code_check always True in LLM prompt ===


@pytest.mark.asyncio
async def test_verification_code_check_always_true_without_totp_config():
    """_build_extract_action_prompt should receive verification_code_check=True even when task has no TOTP config."""
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.async_operation_pool = MagicMock()

    task = MagicMock()
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_1"
    task.workflow_run_id = None
    task.organization_id = "org_1"
    task.url = "https://example.com"

    step = MagicMock()
    step.step_id = "step_1"
    step.order = 0
    step.retry_index = 0

    scraped_page = MagicMock()
    scraped_page.elements = []

    browser_state = MagicMock()

    with (
        patch("skyvern.forge.agent.skyvern_context") as mock_ctx,
        patch.object(agent, "_scrape_with_type", new_callable=AsyncMock, return_value=scraped_page),
        patch.object(
            agent,
            "_build_extract_action_prompt",
            new_callable=AsyncMock,
            return_value=("prompt", False, "extract_action"),
        ) as mock_build,
    ):
        mock_ctx.current.return_value = None
        await agent.build_and_record_step_prompt(
            task, step, browser_state, RunEngine.skyvern_v1, persist_artifacts=False
        )
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs["verification_code_check"] is True


@pytest.mark.asyncio
async def test_verification_code_check_always_true_with_totp_config():
    """_build_extract_action_prompt should receive verification_code_check=True when task HAS TOTP config (unchanged)."""
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.async_operation_pool = MagicMock()

    task = MagicMock()
    task.totp_verification_url = "https://otp.example.com"
    task.totp_identifier = "user@example.com"
    task.task_id = "tsk_2"
    task.workflow_run_id = None
    task.organization_id = "org_1"
    task.url = "https://example.com"

    step = MagicMock()
    step.step_id = "step_1"
    step.order = 0
    step.retry_index = 0

    scraped_page = MagicMock()
    scraped_page.elements = []

    browser_state = MagicMock()

    with (
        patch("skyvern.forge.agent.skyvern_context") as mock_ctx,
        patch.object(agent, "_scrape_with_type", new_callable=AsyncMock, return_value=scraped_page),
        patch.object(
            agent,
            "_build_extract_action_prompt",
            new_callable=AsyncMock,
            return_value=("prompt", False, "extract_action"),
        ) as mock_build,
    ):
        mock_ctx.current.return_value = None
        await agent.build_and_record_step_prompt(
            task, step, browser_state, RunEngine.skyvern_v1, persist_artifacts=False
        )
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs["verification_code_check"] is True


# === Fix: poll_otp_value should pass workflow_id, not workflow_permanent_id ===


@pytest.mark.asyncio
async def test_poll_otp_value_passes_workflow_id_not_permanent_id():
    """poll_otp_value should pass workflow_id (w_* format) to _get_otp_value_from_db, not workflow_permanent_id."""
    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = MagicMock(token="tok")
    mock_db.update_workflow_run = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "skyvern.services.otp_service._get_otp_value_from_db",
            new_callable=AsyncMock,
            return_value=OTPValue(value="654321", type="totp"),
        ) as mock_get_from_db,
    ):
        result = await poll_otp_value(
            organization_id="org_1",
            workflow_id="w_123",
            workflow_run_id="wr_789",
            workflow_permanent_id="wpid_456",
            totp_identifier="user@example.com",
        )
    assert result is not None
    assert result.value == "654321"
    mock_get_from_db.assert_called_once_with(
        "org_1",
        "user@example.com",
        task_id=None,
        workflow_id="w_123",
        workflow_run_id="wr_789",
    )


# === Fix: send_totp_code should resolve wpid_* to w_* before storage ===


@pytest.mark.asyncio
async def test_send_totp_code_resolves_wpid_to_workflow_id():
    """send_totp_code should resolve wpid_* to w_* before storing in DB."""
    mock_workflow = MagicMock()
    mock_workflow.workflow_id = "w_abc123"

    mock_totp_code = MagicMock()

    mock_db = AsyncMock()
    mock_db.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
    mock_db.create_otp_code = AsyncMock(return_value=mock_totp_code)

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    data = TOTPCodeCreate(
        totp_identifier="user@example.com",
        content="123456",
        workflow_id="wpid_xyz789",
    )
    curr_org = MagicMock()
    curr_org.organization_id = "org_1"

    with patch("skyvern.forge.sdk.routes.credentials.app", new=mock_app):
        await send_totp_code(data=data, curr_org=curr_org)

    mock_db.create_otp_code.assert_called_once()
    call_kwargs = mock_db.create_otp_code.call_args[1]
    assert call_kwargs["workflow_id"] == "w_abc123", f"Expected w_abc123 but got {call_kwargs['workflow_id']}"


@pytest.mark.asyncio
async def test_send_totp_code_w_format_passes_through():
    """send_totp_code should resolve and store w_* format workflow_id correctly."""
    mock_workflow = MagicMock()
    mock_workflow.workflow_id = "w_abc123"

    mock_totp_code = MagicMock()

    mock_db = AsyncMock()
    mock_db.get_workflow = AsyncMock(return_value=mock_workflow)
    mock_db.create_otp_code = AsyncMock(return_value=mock_totp_code)

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    data = TOTPCodeCreate(
        totp_identifier="user@example.com",
        content="123456",
        workflow_id="w_abc123",
    )
    curr_org = MagicMock()
    curr_org.organization_id = "org_1"

    with patch("skyvern.forge.sdk.routes.credentials.app", new=mock_app):
        await send_totp_code(data=data, curr_org=curr_org)

    call_kwargs = mock_db.create_otp_code.call_args[1]
    assert call_kwargs["workflow_id"] == "w_abc123"


@pytest.mark.asyncio
async def test_send_totp_code_none_workflow_id():
    """send_totp_code should pass None workflow_id when not provided."""
    mock_totp_code = MagicMock()

    mock_db = AsyncMock()
    mock_db.create_otp_code = AsyncMock(return_value=mock_totp_code)

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    data = TOTPCodeCreate(
        totp_identifier="user@example.com",
        content="123456",
    )
    curr_org = MagicMock()
    curr_org.organization_id = "org_1"

    with patch("skyvern.forge.sdk.routes.credentials.app", new=mock_app):
        await send_totp_code(data=data, curr_org=curr_org)

    call_kwargs = mock_db.create_otp_code.call_args[1]
    assert call_kwargs["workflow_id"] is None


# === Fix: _build_navigation_payload should inject code without TOTP config ===


def test_build_navigation_payload_injects_code_without_totp_config():
    """_build_navigation_payload should inject SPECIAL_FIELD_VERIFICATION_CODE even when
    task has no totp_verification_url or totp_identifier (manual 2FA flow)."""
    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_manual_2fa"
    task.workflow_run_id = "wr_123"
    task.navigation_payload = {"username": "user@example.com"}

    mock_context = MagicMock()
    mock_context.totp_codes = {"tsk_manual_2fa": "123456"}
    mock_context.has_magic_link_page.return_value = False

    with patch("skyvern.forge.agent.skyvern_context") as mock_skyvern_ctx:
        mock_skyvern_ctx.ensure_context.return_value = mock_context
        result = agent._build_navigation_payload(task)

    assert isinstance(result, dict)
    assert SPECIAL_FIELD_VERIFICATION_CODE in result
    assert result[SPECIAL_FIELD_VERIFICATION_CODE] == "123456"
    # Original payload preserved
    assert result["username"] == "user@example.com"


def test_build_navigation_payload_injects_code_when_payload_is_none():
    """_build_navigation_payload should create a dict with the code when payload is None."""
    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_manual_2fa"
    task.workflow_run_id = "wr_123"
    task.navigation_payload = None

    mock_context = MagicMock()
    mock_context.totp_codes = {"tsk_manual_2fa": "999999"}
    mock_context.has_magic_link_page.return_value = False

    with patch("skyvern.forge.agent.skyvern_context") as mock_skyvern_ctx:
        mock_skyvern_ctx.ensure_context.return_value = mock_context
        result = agent._build_navigation_payload(task)

    assert isinstance(result, dict)
    assert result[SPECIAL_FIELD_VERIFICATION_CODE] == "999999"


def test_build_navigation_payload_no_code_no_injection():
    """_build_navigation_payload should NOT inject anything when no code in context."""
    agent = ForgeAgent.__new__(ForgeAgent)

    task = MagicMock()
    task.totp_verification_url = None
    task.totp_identifier = None
    task.task_id = "tsk_no_code"
    task.workflow_run_id = "wr_456"
    task.navigation_payload = {"field": "value"}

    mock_context = MagicMock()
    mock_context.totp_codes = {}  # No code in context
    mock_context.has_magic_link_page.return_value = False

    with patch("skyvern.forge.agent.skyvern_context") as mock_skyvern_ctx:
        mock_skyvern_ctx.ensure_context.return_value = mock_context
        result = agent._build_navigation_payload(task)

    assert isinstance(result, dict)
    assert SPECIAL_FIELD_VERIFICATION_CODE not in result
    assert result["field"] == "value"


# === Task: poll_otp_value publishes 2FA events to notification registry ===


@pytest.mark.asyncio
async def test_poll_otp_value_publishes_required_event_for_task():
    """poll_otp_value should publish verification_code_required when task waiting state is set."""
    mock_code = MagicMock()
    mock_code.code = "123456"
    mock_code.otp_type = "totp"

    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = MagicMock(token="tok")
    mock_db.get_otp_codes_by_run.return_value = [mock_code]
    mock_db.update_task_2fa_state = AsyncMock()

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "skyvern.forge.sdk.notification.factory.NotificationRegistryFactory._NotificationRegistryFactory__registry",
            new=registry,
        ),
    ):
        await poll_otp_value(organization_id="org_1", task_id="tsk_1")

    # Should have received required + resolved messages
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())

    types = [m["type"] for m in messages]
    assert "verification_code_required" in types
    assert "verification_code_resolved" in types

    required = next(m for m in messages if m["type"] == "verification_code_required")
    assert required["task_id"] == "tsk_1"

    resolved = next(m for m in messages if m["type"] == "verification_code_resolved")
    assert resolved["task_id"] == "tsk_1"


@pytest.mark.asyncio
async def test_poll_otp_value_publishes_required_event_for_workflow_run():
    """poll_otp_value should publish verification_code_required when workflow run waiting state is set."""
    mock_code = MagicMock()
    mock_code.code = "654321"
    mock_code.otp_type = "totp"

    mock_db = AsyncMock()
    mock_db.get_valid_org_auth_token.return_value = MagicMock(token="tok")
    mock_db.update_workflow_run = AsyncMock()
    mock_db.get_otp_codes_by_run.return_value = [mock_code]

    mock_app = MagicMock()
    mock_app.DATABASE = mock_db

    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    with (
        patch("skyvern.services.otp_service.app", new=mock_app),
        patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "skyvern.forge.sdk.notification.factory.NotificationRegistryFactory._NotificationRegistryFactory__registry",
            new=registry,
        ),
    ):
        await poll_otp_value(organization_id="org_1", workflow_run_id="wr_1")

    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())

    types = [m["type"] for m in messages]
    assert "verification_code_required" in types
    assert "verification_code_resolved" in types

    required = next(m for m in messages if m["type"] == "verification_code_required")
    assert required["workflow_run_id"] == "wr_1"
