"""Tests for credential TOTP priority over webhook (totp_url) and totp_identifier.

Verifies that try_generate_totp_from_credential() correctly generates TOTP codes
from credential secrets stored in workflow run context, and that callers check
credential TOTP before falling back to poll_otp_value.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest

from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import OTPValue, try_generate_totp_from_credential

# A valid base32 TOTP secret for testing
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _make_workflow_run_context(
    values: dict | None = None,
    secrets: dict | None = None,
) -> MagicMock:
    """Create a mock WorkflowRunContext with the given values and secrets."""
    ctx = MagicMock()
    ctx.values = values or {}
    ctx.secrets = secrets or {}

    def totp_secret_value_key(totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"

    ctx.totp_secret_value_key = totp_secret_value_key

    def get_original_secret_value_or_none(secret_key: str) -> str | None:
        return ctx.secrets.get(secret_key)

    ctx.get_original_secret_value_or_none = get_original_secret_value_or_none
    return ctx


class TestTryGenerateTotpFromCredential:
    """Tests for the try_generate_totp_from_credential helper."""

    def test_returns_none_when_workflow_run_id_is_none(self) -> None:
        result = try_generate_totp_from_credential(None)
        assert result is None

    def test_returns_none_when_no_workflow_run_context(self) -> None:
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = None
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_no_credential_values(self) -> None:
        ctx = _make_workflow_run_context(values={"some_param": "plain_string"})
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_dict_value_has_no_totp_key(self) -> None:
        ctx = _make_workflow_run_context(
            values={"cred_param": {"username": "user", "password": "pass"}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_totp_secret_id_is_empty(self) -> None:
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": ""}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_returns_none_when_totp_secret_not_in_secrets(self) -> None:
        """When the secret ID doesn't resolve to an actual secret value."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": "secret_id_123"}},
            secrets={},  # no secret stored
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_generates_totp_from_credential_secret(self) -> None:
        """Happy path: credential with valid TOTP secret generates a code."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"username": "user", "password": "pass", "totp": "totp_ref_1"}},
            secrets={"totp_ref_1_value": TEST_TOTP_SECRET},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert isinstance(result, OTPValue)
            assert result.type == OTPType.TOTP
            # Verify the code matches what pyotp would generate
            expected_code = pyotp.TOTP(TEST_TOTP_SECRET).now()
            assert result.value == expected_code

    def test_returns_first_matching_credential(self) -> None:
        """When multiple credentials have TOTP, returns the first one found."""
        ctx = _make_workflow_run_context(
            values={
                "cred_a": {"totp": "ref_a"},
                "cred_b": {"totp": "ref_b"},
            },
            secrets={
                "ref_a_value": TEST_TOTP_SECRET,
                "ref_b_value": "ORSXG5DJNZTQ====",
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert result.value == pyotp.TOTP(TEST_TOTP_SECRET).now()

    def test_skips_invalid_secret_and_continues(self) -> None:
        """If one credential has an invalid TOTP secret, skip it and try the next."""
        ctx = _make_workflow_run_context(
            values={
                "cred_bad": {"totp": "ref_bad"},
                "cred_good": {"totp": "ref_good"},
            },
            secrets={
                "ref_bad_value": "NOT_A_VALID_BASE32!!!",
                "ref_good_value": TEST_TOTP_SECRET,
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")

            assert result is not None
            assert result.value == pyotp.TOTP(TEST_TOTP_SECRET).now()

    def test_skips_non_string_totp_id(self) -> None:
        """If the totp value is not a string (e.g., int or None), skip it."""
        ctx = _make_workflow_run_context(
            values={"cred_param": {"totp": 12345}},
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None

    def test_skips_non_dict_values(self) -> None:
        """Non-dict values in the context should be ignored."""
        ctx = _make_workflow_run_context(
            values={
                "string_param": "hello",
                "int_param": 42,
                "list_param": [1, 2, 3],
                "none_param": None,
            },
        )
        with patch("skyvern.services.otp_service.app") as mock_app:
            mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
            result = try_generate_totp_from_credential("wfr_123")
            assert result is None


class TestPollOtpCalledWithoutTotpConfig:
    """Tests that poll_otp_value is called even when totp_verification_url and
    totp_identifier are both None — i.e. the manual 2FA code submission path."""

    @pytest.mark.asyncio
    async def test_poll_otp_called_when_no_totp_config_cua_fallback(self) -> None:
        """When credential TOTP returns None and no totp_verification_url or
        totp_identifier is set, poll_otp_value should still be called via
        generate_cua_fallback_actions so the manual code submission flow works."""
        mock_task = MagicMock()
        mock_task.totp_verification_url = None
        mock_task.totp_identifier = None
        mock_task.organization_id = "org_123"
        mock_task.task_id = "task_456"
        mock_task.workflow_run_id = None
        mock_task.navigation_goal = "test goal"

        mock_step = MagicMock()
        mock_step.step_id = "step_789"
        mock_step.order = 0

        mock_otp_value = MagicMock(spec=OTPValue)
        mock_otp_value.get_otp_type.return_value = OTPType.TOTP
        mock_otp_value.value = "123456"

        with (
            patch(
                "skyvern.webeye.actions.parse_actions.try_generate_totp_from_credential",
                return_value=None,
            ),
            patch(
                "skyvern.webeye.actions.parse_actions.poll_otp_value",
                new_callable=AsyncMock,
                return_value=mock_otp_value,
            ) as mock_poll,
            patch("skyvern.webeye.actions.parse_actions.app") as mock_app,
            patch("skyvern.webeye.actions.parse_actions.prompt_engine") as mock_prompt_engine,
        ):
            mock_prompt_engine.load_prompt.return_value = "test prompt"
            # LLM returns get_verification_code action
            mock_app.LLM_API_HANDLER = AsyncMock(
                return_value={"action": "get_verification_code", "useful_information": "Need 2FA code"},
            )

            from skyvern.webeye.actions.parse_actions import generate_cua_fallback_actions

            actions = await generate_cua_fallback_actions(
                task=mock_task,
                step=mock_step,
                assistant_message="Enter verification code",
                reasoning="Need 2FA code",
            )

            # poll_otp_value should have been called even though
            # totp_verification_url and totp_identifier are both None
            mock_poll.assert_called_once_with(
                organization_id="org_123",
                task_id="task_456",
                workflow_run_id=None,
                totp_verification_url=None,
                totp_identifier=None,
            )

            # Verify we got a VerificationCodeAction back
            assert len(actions) == 1
            from skyvern.webeye.actions.actions import VerificationCodeAction

            assert isinstance(actions[0], VerificationCodeAction)
            assert actions[0].verification_code == "123456"

    @pytest.mark.asyncio
    async def test_poll_otp_called_when_no_totp_config_agent(self) -> None:
        """When credential TOTP returns None and no totp_verification_url or
        totp_identifier is set, poll_otp_value should still be called via the
        agent's handle_potential_verification_code so the manual code submission
        flow works."""
        # Return None from poll_otp_value so we hit the early return at line 4548
        # (no valid OTP) — this avoids needing to mock the deeper context/LLM calls
        with (
            patch(
                "skyvern.forge.agent.try_generate_totp_from_credential",
                return_value=None,
            ),
            patch(
                "skyvern.forge.agent.poll_otp_value",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_poll,
            patch("skyvern.forge.agent.app") as mock_app,
        ):
            mock_app.DATABASE.get_workflow_run = AsyncMock(return_value=None)

            from skyvern.forge.agent import ForgeAgent

            agent = ForgeAgent.__new__(ForgeAgent)

            mock_task = MagicMock()
            mock_task.totp_verification_url = None
            mock_task.totp_identifier = None
            mock_task.organization_id = "org_123"
            mock_task.task_id = "task_456"
            mock_task.workflow_run_id = None

            json_response = {
                "place_to_enter_verification_code": True,
                "should_enter_verification_code": True,
            }

            result = await agent.handle_potential_verification_code(
                task=mock_task,
                step=MagicMock(),
                scraped_page=MagicMock(),
                browser_state=MagicMock(),
                json_response=json_response,
            )

            # poll_otp_value should have been called even though
            # totp_verification_url and totp_identifier are both None
            mock_poll.assert_called_once_with(
                organization_id="org_123",
                task_id="task_456",
                workflow_id=None,
                workflow_run_id=None,
                workflow_permanent_id=None,
                totp_verification_url=None,
                totp_identifier=None,
            )

            # When poll_otp_value returns None, the method returns json_response unchanged
            assert result == json_response
