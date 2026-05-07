"""Tests for skyvern.services.otp_service — MFA key detection and TOTP extraction."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest

from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.services.otp_service import (
    OTPValue,
    _is_mfa_like_parameter_key,
    extract_totp_from_navigation_inputs,
    poll_otp_value,
    try_generate_totp_from_credential,
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
    """poll_otp_value retries fetch failures across the full wall-clock timeout window."""

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_retries_on_transient_failure_then_succeeds(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """Fail twice, then return OTP on third attempt."""
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
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
    async def test_does_not_short_circuit_during_extended_outage(self) -> None:
        """Persistent webhook errors must not exit polling before the wall-clock timeout fires (SKY-9553)."""
        base = datetime(2026, 1, 1, 12, 0, 0)
        utcnow_returns = [base] + [base + timedelta(seconds=60 * (i + 1)) for i in range(8)]

        with (
            patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
            patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock) as mock_fetch,
            patch("skyvern.services.otp_service.app") as mock_app,
            patch("skyvern.services.otp_service.settings") as mock_settings,
            patch("skyvern.services.otp_service.datetime") as mock_datetime,
        ):
            mock_datetime.utcnow.side_effect = utcnow_returns
            mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
            mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())

            otp = OTPValue(value="424242")
            mock_fetch.side_effect = [
                *([FailedToGetTOTPVerificationCode(reason="connection refused")] * 7),
                otp,
            ]

            result = await poll_otp_value(
                organization_id="o_test",
                task_id="tsk_test",
                totp_verification_url="https://example.com/mfa",
            )
            assert result == otp
            assert mock_fetch.call_count == 8

    @pytest.mark.asyncio
    async def test_raises_failed_with_reason_when_timeout_during_failure_streak(self) -> None:
        """When the wall-clock timeout fires while the webhook is still failing, surface the underlying reason."""
        base = datetime(2026, 1, 1, 12, 0, 0)
        utcnow_returns = [
            base,  # start_datetime
            base + timedelta(seconds=30),  # iter 1: not timed out, fetch fails
            base + timedelta(seconds=60),  # iter 2: not timed out, fetch fails
            base + timedelta(minutes=16),  # iter 3: timed out
        ]

        with (
            patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
            patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock) as mock_fetch,
            patch("skyvern.services.otp_service.app") as mock_app,
            patch("skyvern.services.otp_service.settings") as mock_settings,
            patch("skyvern.services.otp_service.datetime") as mock_datetime,
        ):
            mock_datetime.utcnow.side_effect = utcnow_returns
            mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
            mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
            mock_fetch.side_effect = FailedToGetTOTPVerificationCode(reason="connection refused")

            with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
                await poll_otp_value(
                    organization_id="o_test",
                    task_id="tsk_test",
                    totp_verification_url="https://example.com/mfa",
                )
            assert exc_info.value.reason == "connection refused"

    @pytest.mark.asyncio
    async def test_raises_no_otp_at_timeout_when_polls_were_clean(self) -> None:
        """When polling timed out without webhook errors (just no OTP yet), raise NoTOTPVerificationCodeFound."""
        base = datetime(2026, 1, 1, 12, 0, 0)
        utcnow_returns = [
            base,
            base + timedelta(seconds=30),
            base + timedelta(minutes=16),
        ]

        with (
            patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock),
            patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock) as mock_fetch,
            patch("skyvern.services.otp_service.app") as mock_app,
            patch("skyvern.services.otp_service.settings") as mock_settings,
            patch("skyvern.services.otp_service.datetime") as mock_datetime,
        ):
            mock_datetime.utcnow.side_effect = utcnow_returns
            mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
            mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
            mock_fetch.return_value = None

            with pytest.raises(NoTOTPVerificationCodeFound):
                await poll_otp_value(
                    organization_id="o_test",
                    task_id="tsk_test",
                    totp_verification_url="https://example.com/mfa",
                )


class _FakeWorkflowRunContext:
    """Minimal stub mirroring WorkflowRunContext shape for try_generate_totp_from_credential."""

    def __init__(self, values: dict[str, dict[str, str]], secrets: dict[str, str]) -> None:
        self.values = values
        self.secrets = secrets

    def totp_secret_value_key(self, totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"

    def get_original_secret_value_or_none(self, key: str) -> str | None:
        return self.secrets.get(key)


_VALID_TOTP_SEED = "JBSWY3DPEHPK3PXP"
_OTHER_TOTP_SEED = "KRSXG5DJEBKWG33SMR2A"


def _scoped_context(active: str | None) -> SkyvernContext:
    ctx = SkyvernContext(active_credential_parameter_key=active)
    return ctx


class TestTryGenerateTotpFromCredential:
    """Credential-aware lookup must scope to the credential the agent is typing into."""

    def _patch_workflow_context(self, monkeypatch: pytest.MonkeyPatch, fake: _FakeWorkflowRunContext) -> None:
        from skyvern.services import otp_service

        fake_app = SimpleNamespace(
            WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(get_workflow_run_context=lambda _wr_id: fake),
        )
        monkeypatch.setattr(otp_service, "app", fake_app)

    def test_active_credential_returns_its_own_totp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When two credentials exist, only the active one's TOTP is generated."""
        fake = _FakeWorkflowRunContext(
            values={
                "credentials_1": {"username": "u_a", "password": "p_a"},
                "credentials": {"username": "u_b", "password": "p_b", "totp": "tot_b"},
            },
            secrets={"tot_b_value": _OTHER_TOTP_SEED},
        )
        self._patch_workflow_context(monkeypatch, fake)

        with skyvern_context.scoped(_scoped_context(active="credentials_1")):
            result = try_generate_totp_from_credential("wr_test")

        assert result is None

    def test_active_credential_with_totp_returns_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeWorkflowRunContext(
            values={
                "credentials_1": {"username": "u_a", "password": "p_a"},
                "credentials": {"username": "u_b", "password": "p_b", "totp": "tot_b"},
            },
            secrets={"tot_b_value": _OTHER_TOTP_SEED},
        )
        self._patch_workflow_context(monkeypatch, fake)
        monkeypatch.setattr(pyotp.TOTP, "now", lambda _self: "424242")

        with skyvern_context.scoped(_scoped_context(active="credentials")):
            result = try_generate_totp_from_credential("wr_test")

        assert result is not None
        assert result.value == "424242"

    def test_no_active_credential_with_multiple_totps_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Avoid the original bug: walking all credentials when which is active is unknown."""
        fake = _FakeWorkflowRunContext(
            values={
                "credentials_1": {"username": "u_a", "totp": "tot_a"},
                "credentials": {"username": "u_b", "totp": "tot_b"},
            },
            secrets={"tot_a_value": _VALID_TOTP_SEED, "tot_b_value": _OTHER_TOTP_SEED},
        )
        self._patch_workflow_context(monkeypatch, fake)

        with skyvern_context.scoped(_scoped_context(active=None)):
            result = try_generate_totp_from_credential("wr_test")

        assert result is None

    def test_no_active_credential_with_single_totp_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single-credential workflows still work even before any field has been typed."""
        fake = _FakeWorkflowRunContext(
            values={"credentials": {"username": "u_b", "totp": "tot_b"}},
            secrets={"tot_b_value": _VALID_TOTP_SEED},
        )
        self._patch_workflow_context(monkeypatch, fake)
        monkeypatch.setattr(pyotp.TOTP, "now", lambda _self: "131313")

        with skyvern_context.scoped(_scoped_context(active=None)):
            result = try_generate_totp_from_credential("wr_test")

        assert result is not None
        assert result.value == "131313"

    def test_workflow_run_id_none_returns_none(self) -> None:
        assert try_generate_totp_from_credential(None) is None

    def test_no_workflow_run_context_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.services import otp_service

        fake_app = SimpleNamespace(
            WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(get_workflow_run_context=lambda _wr_id: None),
        )
        monkeypatch.setattr(otp_service, "app", fake_app)

        with skyvern_context.scoped(_scoped_context(active="credentials")):
            assert try_generate_totp_from_credential("wr_test") is None
