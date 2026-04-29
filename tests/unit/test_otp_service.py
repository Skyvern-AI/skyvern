"""Tests for skyvern.services.otp_service — MFA key detection and TOTP extraction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import FailedToGetTOTPVerificationCode
from skyvern.services.otp_service import (
    OTPValue,
    _is_mfa_like_parameter_key,
    extract_totp_from_navigation_inputs,
    poll_otp_value,
)


class TestIsMfaLikeParameterKey:
    """_is_mfa_like_parameter_key should match OTP *code* keys but reject metadata keys."""

    @pytest.mark.parametrize(
        "key",
        [
            "otp_code",
            "mfa_code",
            "verification_code",
            "otp",
            "mfa",
            "verification",
            "OTP_CODE",
            "MFA_Code",
        ],
    )
    def test_matches_actual_otp_code_keys(self, key: str) -> None:
        assert _is_mfa_like_parameter_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            # The root-cause bug: totp_identifier contains "otp" but is metadata
            "totp_identifier",
            "totp_url",
            "totp_secret",
            "totp_seed",
            "otp_identifier",
            "mfa_secret_key",
            "verification_url",
            # Unrelated keys
            "username",
            "password",
            "email",
            "url",
        ],
    )
    def test_rejects_metadata_and_unrelated_keys(self, key: str) -> None:
        assert _is_mfa_like_parameter_key(key) is False


class TestExtractTotpFromNavigationInputs:
    """extract_totp_from_navigation_inputs should only return actual OTP codes."""

    def test_returns_none_for_none_payload(self) -> None:
        assert extract_totp_from_navigation_inputs(None) is None

    def test_returns_none_for_empty_dict(self) -> None:
        assert extract_totp_from_navigation_inputs({}) is None

    def test_extracts_otp_code_from_payload(self) -> None:
        payload = {"otp_code": "123456"}
        result = extract_totp_from_navigation_inputs(payload)
        assert result is not None
        assert result.value == "123456"

    def test_ignores_totp_identifier_key(self) -> None:
        """The core bug: totp_identifier value must NOT be returned as OTP code."""
        payload = {
            "totp_identifier": "2fa.00000000-0000-0000-0000-000000000000.wpid_000000000000000000",
            "totp_url": "https://example.com/totp",
        }
        result = extract_totp_from_navigation_inputs(payload)
        assert result is None

    def test_extracts_code_even_with_metadata_keys_present(self) -> None:
        """When both metadata and code keys exist, only the code value is returned."""
        payload = {
            "totp_identifier": "2fa.ab4a9cf0-xxx",
            "otp_code": "654321",
        }
        result = extract_totp_from_navigation_inputs(payload)
        assert result is not None
        assert result.value == "654321"

    def test_extracts_mfa_code_from_nested_dict(self) -> None:
        payload = {"data": {"mfa_code": "789012"}}
        result = extract_totp_from_navigation_inputs(payload)
        assert result is not None
        assert result.value == "789012"

    def test_returns_none_for_only_metadata_keys(self) -> None:
        payload = {
            "totp_identifier": "user@example.com",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        result = extract_totp_from_navigation_inputs(payload)
        assert result is None


def _mock_org_token() -> MagicMock:
    token = MagicMock()
    token.token = "fake-token"
    return token


class TestPollOtpValueRetry:
    """poll_otp_value should tolerate transient FailedToGetTOTPVerificationCode up to max consecutive failures."""

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_retries_on_transient_failure_then_succeeds(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Fail twice, then return OTP on third attempt. Counter resets, OTP is returned."""
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_settings.VERIFICATION_CODE_POLLING_MAX_CONSECUTIVE_FAILURES = 3
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())

        otp = OTPValue(value="123456")
        mock_fetch.side_effect = [
            FailedToGetTOTPVerificationCode(reason="connection error"),
            FailedToGetTOTPVerificationCode(reason="connection error"),
            otp,
        ]

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            totp_verification_url="https://example.com/mfa",
        )
        assert result == otp
        assert mock_fetch.call_count == 3

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_raises_after_max_consecutive_failures(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """After N consecutive failures, re-raise FailedToGetTOTPVerificationCode."""
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_settings.VERIFICATION_CODE_POLLING_MAX_CONSECUTIVE_FAILURES = 3
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())

        mock_fetch.side_effect = FailedToGetTOTPVerificationCode(reason="connection error")

        with pytest.raises(FailedToGetTOTPVerificationCode):
            await poll_otp_value(
                organization_id="o_test",
                task_id="tsk_test",
                totp_verification_url="https://example.com/mfa",
            )
        assert mock_fetch.call_count == 3

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_counter_resets_after_success(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Fail twice, succeed (None), fail twice again, then return OTP. Counter resets between failure bursts."""
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_settings.VERIFICATION_CODE_POLLING_MAX_CONSECUTIVE_FAILURES = 3
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())

        otp = OTPValue(value="654321")
        mock_fetch.side_effect = [
            FailedToGetTOTPVerificationCode(reason="err"),
            FailedToGetTOTPVerificationCode(reason="err"),
            None,  # success (no OTP yet, but resets counter)
            FailedToGetTOTPVerificationCode(reason="err"),
            FailedToGetTOTPVerificationCode(reason="err"),
            otp,
        ]

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            totp_verification_url="https://example.com/mfa",
        )
        assert result == otp
        assert mock_fetch.call_count == 6
