from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import _find_verification_code_action


class TestFindVerificationCodeAction:
    def test_finds_mfa_action_at_any_position(self) -> None:
        """MFA action is 3rd of 4 actions (email, password, MFA, submit)."""
        actions = [
            {"action_type": "INPUT_TEXT", "id": "AAAX", "reasoning": "Fill email field", "text": "user@example.com"},
            {"action_type": "INPUT_TEXT", "id": "AAAZ", "reasoning": "Fill password field", "text": "pa$w0rd"},
            {
                "action_type": "INPUT_TEXT",
                "id": "AAAb",
                "reasoning": "MFA code must be entered into the MFA input",
                "text": "123456",
            },
            {
                "action_type": "CLICK",
                "id": "AAAc",
                "reasoning": "Submit the entered credentials and MFA code",
                "text": None,
            },
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "AAAb"
        assert result["text"] == "123456"

    def test_finds_totp_keyword(self) -> None:
        actions = [
            {
                "action_type": "INPUT_TEXT",
                "id": "A1",
                "reasoning": "Enter TOTP code from authenticator",
                "text": "654321",
            },
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["text"] == "654321"

    def test_finds_otp_keyword(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "B1", "reasoning": "Input OTP value", "text": "111111"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None

    def test_skips_click_actions_mentioning_mfa(self) -> None:
        """CLICK actions mentioning MFA should be ignored — avoids dupes."""
        actions = [
            {"action_type": "CLICK", "id": "AAAc", "reasoning": "Submit the MFA code form", "text": None},
        ]
        result = _find_verification_code_action(actions)
        assert result is None

    def test_skips_click_only_matches_input_text(self) -> None:
        """Only the INPUT_TEXT action should match, not the CLICK that also mentions MFA."""
        actions = [
            {"action_type": "INPUT_TEXT", "id": "AAAb", "reasoning": "Enter MFA code", "text": "123456"},
            {"action_type": "CLICK", "id": "AAAc", "reasoning": "Submit MFA form", "text": None},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "AAAb"

    def test_returns_none_for_empty_list(self) -> None:
        assert _find_verification_code_action([]) is None

    def test_returns_none_when_no_mfa_actions(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "X1", "reasoning": "Fill the name field", "text": "John"},
            {"action_type": "CLICK", "id": "X2", "reasoning": "Submit the form", "text": None},
        ]
        assert _find_verification_code_action(actions) is None

    def test_case_insensitive(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "C1", "reasoning": "Enter the MFA Code here", "text": "555555"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None

    def test_handles_missing_reasoning(self) -> None:
        actions = [
            {"action_type": "INPUT_TEXT", "id": "D1", "text": "999"},
            {"action_type": "INPUT_TEXT", "id": "D2", "reasoning": "Enter OTP", "text": "888"},
        ]
        result = _find_verification_code_action(actions)
        assert result is not None
        assert result["id"] == "D2"


@pytest.mark.asyncio
async def test_handle_verification_code_finds_mfa_not_first_action() -> None:
    """When MFA action is 3rd, method should find it, not use actions[0]."""
    from skyvern.forge.agent import ForgeAgent

    with (
        patch("skyvern.forge.agent.try_generate_totp_from_credential", return_value=None),
        patch("skyvern.forge.agent.poll_otp_value", new_callable=AsyncMock, return_value=None),
        patch("skyvern.forge.agent.app") as mock_app,
        patch("skyvern.forge.agent.clear_stale_2fa_waiting_state", new_callable=AsyncMock) as mock_clear,
        patch("skyvern.forge.agent.skyvern_context") as mock_skyvern_context,
        patch("skyvern.forge.agent.service_utils.is_cua_task", new_callable=AsyncMock, return_value=False),
        patch(
            "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
            return_value=AsyncMock(return_value={"actions": []}),
        ),
    ):
        mock_app.DATABASE.get_workflow_run = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.totp_codes = {}
        mock_skyvern_context.ensure_context.return_value = mock_context
        mock_skyvern_context.current.return_value = mock_context

        agent = ForgeAgent.__new__(ForgeAgent)
        agent._build_extract_action_prompt = AsyncMock(return_value=("prompt", False, "extract-actions"))  # type: ignore[attr-defined]

        mock_task = MagicMock()
        mock_task.organization_id = "org_123"
        mock_task.task_id = "task_456"
        mock_task.workflow_run_id = "wfr_123"
        mock_task.totp_verification_url = None
        mock_task.totp_identifier = None
        mock_task.llm_key = None

        scraped_page = MagicMock()
        scraped_page.screenshots = []

        json_response = {
            "place_to_enter_verification_code": True,
            "should_enter_verification_code": True,
            "actions": [
                {"action_type": "INPUT_TEXT", "id": "AAAX", "reasoning": "Fill email", "text": "user@example.com"},
                {"action_type": "INPUT_TEXT", "id": "AAAZ", "reasoning": "Fill password", "text": "pa$w0rd"},
                {"action_type": "INPUT_TEXT", "id": "AAAb", "reasoning": "Enter MFA code", "text": "520265"},
                {"action_type": "CLICK", "id": "AAAc", "reasoning": "Submit form with MFA", "text": None},
            ],
        }

        await agent.handle_potential_verification_code(
            task=mock_task,
            step=MagicMock(),
            scraped_page=scraped_page,
            browser_state=MagicMock(),
            json_response=json_response,
        )

        # Should have found the MFA action (AAAb) with text "520265", not actions[0] email
        mock_clear.assert_awaited_once()
        assert mock_context.totp_codes["task_456"] == "520265"
