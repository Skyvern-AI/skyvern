"""Tests for skyvern.services.otp_service — MFA key detection and TOTP extraction."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
import structlog.testing

from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp_service import (
    OTPValue,
    _get_otp_value_from_db,
    _get_otp_value_from_url,
    _is_mfa_like_parameter_key,
    extract_totp_from_navigation_inputs,
    parse_otp_login,
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

    def test_untyped_short_numeric_value_defaults_to_totp(self) -> None:
        assert OTPValue(value="123456").get_otp_type() == OTPType.TOTP

    def test_returns_none_for_none_payload(self) -> None:
        assert extract_totp_from_navigation_inputs(None) is None

    def test_returns_none_for_empty_dict(self) -> None:
        assert extract_totp_from_navigation_inputs({}) is None

    def test_extracts_otp_code_from_payload(self) -> None:
        payload = {"otp_code": "123456"}
        result = extract_totp_from_navigation_inputs(payload)
        assert result is not None
        assert result.value == "123456"
        assert result.get_otp_type() == OTPType.TOTP

    def test_extracts_magic_link_from_payload(self) -> None:
        payload = {"verification_code": "https://example.com/login/magic?token=abc123"}
        result = extract_totp_from_navigation_inputs(payload)
        assert result is not None
        assert result.value == "https://example.com/login/magic?token=abc123"
        assert result.get_otp_type() == OTPType.MAGIC_LINK

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


def _patch_totp_url_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int,
    headers: dict[str, str],
    response_body: object,
    is_json_response: bool,
) -> None:
    from skyvern.services import otp_service

    async def post_totp_verification_url(*_args: object, **_kwargs: object) -> tuple[int, dict[str, str], object, bool]:
        return status_code, headers, response_body, is_json_response

    monkeypatch.setattr(otp_service, "_post_totp_verification_url", post_totp_verification_url)


class TestGetOtpValueFromUrl:
    @pytest.mark.asyncio
    async def test_returns_totp_from_valid_json_verification_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "application/json"},
            response_body={"verification_code": "123456"},
            is_json_response=True,
        )

        result = await _get_otp_value_from_url(
            organization_id="o_test",
            url="https://example.com/totp",
            api_key="api-key",
            task_id="tsk_test",
        )

        assert result == OTPValue(value="123456", type="totp")

    @pytest.mark.asyncio
    async def test_text_plain_200_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            response_body="147258",
            is_json_response=False,
        )

        with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
            await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                workflow_run_id="wr_test",
            )

        reason = exc_info.value.reason or ""
        assert "https://example.com/totp" not in reason
        assert "totp_webhook_non_json_response" in reason
        assert "http_status=200" in reason
        assert "content_type=text/plain" in reason
        assert "charset" not in reason
        assert "body_preview='[REDACTED_OTP_BODY](length=6,json_like=false)'" in reason
        assert "147258" not in reason
        assert '{"verification_code":"123456"}' in reason
        assert "api-key" not in reason

    @pytest.mark.asyncio
    async def test_text_plain_200_truncates_long_body_preview(self, monkeypatch: pytest.MonkeyPatch) -> None:
        long_body = "x" * 250
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "text/plain"},
            response_body=long_body,
            is_json_response=False,
        )

        with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
            await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                workflow_run_id="wr_test",
            )

        reason = exc_info.value.reason or ""
        assert "body_preview='[REDACTED_OTP_BODY](length=250,json_like=false)'" in reason
        assert long_body not in reason

    @pytest.mark.asyncio
    async def test_text_plain_200_reports_absent_content_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={},
            response_body="147258",
            is_json_response=False,
        )

        with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
            await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                workflow_run_id="wr_test",
            )

        reason = exc_info.value.reason or ""
        assert "content_type=<absent>" in reason

    @pytest.mark.asyncio
    async def test_text_plain_200_body_preview_uses_single_repr_escaping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "text/plain"},
            response_body="line1\nline2",
            is_json_response=False,
        )

        with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
            await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                workflow_run_id="wr_test",
            )

        reason = exc_info.value.reason or ""
        assert "body_preview='[REDACTED_OTP_BODY](length=11,json_like=false)'" in reason
        assert "line1" not in reason
        assert "line2" not in reason

    @pytest.mark.asyncio
    async def test_json_missing_verification_code_returns_none_and_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import structlog.testing

        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "application/json"},
            response_body={"message": "pending"},
            is_json_response=True,
        )

        with structlog.testing.capture_logs() as logs:
            result = await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                task_id="tsk_test",
            )

        assert result is None
        missing_code_log = next(
            (log for log in logs if "No verification_code found in TOTP webhook response" in log.get("event", "")),
            None,
        )
        assert missing_code_log is not None
        assert "endpoint_url" not in missing_code_log
        assert "https://example.com/totp" not in repr(logs)

    @pytest.mark.asyncio
    async def test_json_non_object_response_returns_none_and_logs_distinct_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import structlog.testing

        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "application/json"},
            response_body=["147258"],
            is_json_response=True,
        )

        with structlog.testing.capture_logs() as logs:
            result = await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                task_id="tsk_test",
            )

        assert result is None
        non_object_log = next(
            (log for log in logs if "TOTP webhook response body is not a JSON object" in log.get("event", "")),
            None,
        )
        assert non_object_log is not None
        assert "endpoint_url" not in non_object_log
        assert "https://example.com/totp" not in repr(logs)
        assert not any("No verification_code found in TOTP webhook response" in log.get("event", "") for log in logs)

    @pytest.mark.asyncio
    async def test_non_200_response_returns_none_and_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import structlog.testing

        _patch_totp_url_response(
            monkeypatch,
            status_code=500,
            headers={"Content-Type": "text/plain"},
            response_body="temporary outage",
            is_json_response=False,
        )

        with structlog.testing.capture_logs() as logs:
            result = await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                task_id="tsk_test",
            )

        assert result is None
        non_200_log = next(
            (log for log in logs if "TOTP webhook returned non-200 response" in log.get("event", "")), None
        )
        assert non_200_log is not None
        assert "endpoint_url" not in non_200_log
        assert "https://example.com/totp" not in repr(logs)

    @pytest.mark.asyncio
    async def test_relayed_email_in_malformed_json_extracts_code_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a customer relays a raw OTP email into verification_code,
        producing a body that isn't strict JSON. The login must recover the code
        via the LLM extractor rather than terminating with a contract error."""
        from types import SimpleNamespace

        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        raw_email = (
            "Verification Code - Your One-Time Passcode for Secure Account Login\r\n"
            "WARNING! EXTERNAL EMAIL.\r\nYour passcode is 654321."
        )
        body = '{\r\n  "task_id": "tsk_test",\r\n  "verification_code": "' + raw_email + '"\r\n}'
        seam = AsyncMock(return_value=TOTPVerificationResponse(status_code=200, body=body, headers={}))
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        async def fake_parse_otp_login(
            content: str, organization_id: str, *_args: object, **_kwargs: object
        ) -> OTPValue:
            assert "654321" in content
            return OTPValue(value="654321", type="totp")

        monkeypatch.setattr(otp_service, "parse_otp_login", fake_parse_otp_login)

        result = await _get_otp_value_from_url(
            organization_id="o_test",
            url="https://example.com/totp",
            api_key="api-key",
            task_id="tsk_test",
        )

        assert result == OTPValue(value="654321", type="totp")

    @pytest.mark.asyncio
    async def test_parse_failure_warning_omits_raw_content_keeps_length(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.services import otp_service

        raw_content = "this-is-a-relayed-email-with-code-135790-inside"
        _patch_totp_url_response(
            monkeypatch,
            status_code=200,
            headers={"Content-Type": "application/json"},
            response_body={"verification_code": raw_content},
            is_json_response=True,
        )

        async def fake_parse_otp_login(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(otp_service, "parse_otp_login", fake_parse_otp_login)

        with structlog.testing.capture_logs() as logs:
            result = await _get_otp_value_from_url(
                organization_id="o_test",
                url="https://example.com/totp",
                api_key="api-key",
                task_id="tsk_test",
            )

        assert result is None

        warning = next((r for r in logs if r.get("event") == "Failed to parse otp login from the totp url"), None)
        assert warning is not None
        assert "content_preview" not in warning
        assert warning["content_length"] == len(raw_content)
        for record in logs:
            assert raw_content not in repr(record)
            for value in record.values():
                assert raw_content not in str(value)


class TestParseOtpLogin:
    @pytest.mark.asyncio
    async def test_parser_log_omits_raw_code_and_reasoning_keeps_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.services import otp_service

        raw = "135790"
        resp = {
            "reasoning": f"the code is {raw}",
            "otp_type": "totp",
            "otp_value_found": True,
            "otp_value": raw,
        }

        async def fake_handler(*_args: object, **_kwargs: object) -> dict[str, object]:
            return resp

        monkeypatch.setattr(otp_service.prompt_engine, "load_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(otp_service.app, "SECONDARY_LLM_API_HANDLER", fake_handler, raising=False)

        with structlog.testing.capture_logs() as logs:
            result = await parse_otp_login(content="raw email body", organization_id="o_test")

        assert result == OTPValue(value=raw, type="totp")

        for record in logs:
            assert raw not in repr(record)
            for value in record.values():
                assert raw not in str(value)

        parser = next((r for r in logs if r.get("event") == "OTP Login Parser Response"), None)
        assert parser is not None
        assert "resp" not in parser
        assert "reasoning" not in parser
        assert "otp_value" not in parser
        assert parser["otp_type"] == "totp"
        assert parser["otp_value_found"] is True
        assert parser["otp_length"] == len(raw)


class TestPollOtpValueRetry:
    """poll_otp_value retries fetch failures across the full wall-clock timeout window."""

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_passes_workflow_permanent_id_to_totp_url_fetch(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
        otp = OTPValue(value="123456")
        mock_fetch.return_value = otp

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            totp_verification_url="https://example.com/mfa",
        )

        assert result == otp
        mock_fetch.assert_awaited_once_with(
            "o_test",
            "https://example.com/mfa",
            "fake-token",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
        )

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
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_db", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_gmail", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.settings")
    async def test_falls_back_to_db_when_gmail_has_no_code(
        self,
        mock_settings: MagicMock,
        mock_gmail: AsyncMock,
        mock_db: AsyncMock,
        mock_sleep: AsyncMock,
    ) -> None:
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_gmail.return_value = None
        otp = OTPValue(value="123456", type=OTPType.TOTP)
        mock_db.return_value = otp

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            totp_identifier="otp@example.com",
        )

        assert result == otp
        mock_gmail.assert_awaited_once()
        mock_db.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_db", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_gmail", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_prefers_url_before_gmail_and_db_when_url_has_code(
        self,
        mock_settings: MagicMock,
        mock_app: MagicMock,
        mock_gmail: AsyncMock,
        mock_url: AsyncMock,
        mock_db: AsyncMock,
        mock_sleep: AsyncMock,
    ) -> None:
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
        otp = OTPValue(value="123456", type=OTPType.TOTP)
        mock_url.return_value = otp
        mock_gmail.return_value = OTPValue(value="654321", type=OTPType.TOTP)

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            totp_identifier="otp@example.com",
            totp_verification_url="https://example.com/mfa",
        )

        assert result == otp
        mock_url.assert_awaited_once()
        mock_gmail.assert_not_awaited()
        mock_db.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_db", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_gmail", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_falls_back_to_gmail_before_db_when_url_has_no_code(
        self,
        mock_settings: MagicMock,
        mock_app: MagicMock,
        mock_gmail: AsyncMock,
        mock_url: AsyncMock,
        mock_db: AsyncMock,
        mock_sleep: AsyncMock,
    ) -> None:
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
        mock_url.return_value = None
        otp = OTPValue(value="123456", type=OTPType.TOTP)
        mock_gmail.return_value = otp

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            totp_identifier="otp@example.com",
            totp_verification_url="https://example.com/mfa",
        )

        assert result == otp
        mock_url.assert_awaited_once()
        mock_gmail.assert_awaited_once()
        mock_db.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_db", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_gmail", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    @patch("skyvern.services.otp_service.datetime")
    async def test_falls_back_to_db_after_url_and_gmail_preserves_db_cutoff_default(
        self,
        mock_datetime: MagicMock,
        mock_settings: MagicMock,
        mock_app: MagicMock,
        mock_gmail: AsyncMock,
        mock_url: AsyncMock,
        mock_db: AsyncMock,
        mock_sleep: AsyncMock,
    ) -> None:
        start = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.utcnow.return_value = start
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
        mock_url.return_value = None
        mock_gmail.return_value = None
        otp = OTPValue(value="123456", type=OTPType.TOTP)
        mock_db.return_value = otp

        result = await poll_otp_value(
            organization_id="o_test",
            task_id="tsk_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            totp_identifier="otp@example.com",
            totp_verification_url="https://example.com/mfa",
        )

        assert result == otp
        mock_url.assert_awaited_once()
        mock_gmail.assert_awaited_once()
        mock_db.assert_awaited_once()
        assert mock_gmail.await_args.kwargs["created_after"] == start
        assert mock_db.await_args.kwargs["created_after"] is None

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
        """When timeout fires while webhook still fails, surface only sanitized failure context."""
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
            raw_identifier = "otp@example.com"
            raw_url = "https://example.com/mfa?token=secret"
            raw_code = "135790"
            mock_fetch.side_effect = FailedToGetTOTPVerificationCode(
                reason=f"connection refused for {raw_identifier} at {raw_url} after code {raw_code}"
            )

            with structlog.testing.capture_logs() as logs:
                with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
                    await poll_otp_value(
                        organization_id="o_test",
                        task_id="tsk_test",
                        totp_identifier=raw_identifier,
                        totp_verification_url=raw_url,
                    )

            assert exc_info.value.reason == "totp_webhook_request_failed"
            assert raw_identifier not in str(exc_info.value)
            assert raw_url not in str(exc_info.value)
            assert raw_code not in str(exc_info.value)

            retry_log = next(
                (r for r in logs if r.get("event") == "OTP fetch failed, will retry until wall-clock timeout"), None
            )
            assert retry_log is not None
            assert "totp_identifier" not in retry_log
            assert "totp_verification_url" not in retry_log

            timeout_log = next(
                (r for r in logs if r.get("event") == "Polling otp value timed out while webhook was still failing"),
                None,
            )
            assert timeout_log is not None
            assert timeout_log["last_error_reason"] == exc_info.value.reason
            for record in logs:
                assert raw_identifier not in repr(record)
                assert raw_url not in repr(record)
                assert raw_code not in repr(record)

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

    @pytest.mark.asyncio
    @patch("skyvern.services.otp_service.asyncio.sleep", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service._get_otp_value_from_url", new_callable=AsyncMock)
    @patch("skyvern.services.otp_service.app")
    @patch("skyvern.services.otp_service.settings")
    async def test_success_log_omits_raw_otp_code_and_otp_locator_fields(
        self, mock_settings: MagicMock, mock_app: MagicMock, mock_fetch: AsyncMock, mock_sleep: AsyncMock
    ) -> None:
        """The success and polling logs must not emit OTP codes, identifiers, or verification URLs."""
        import structlog.testing

        raw = "135790"
        raw_identifier = "otp@example.com"
        raw_url = "https://example.com/mfa"
        mock_settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS = 15
        mock_app.DATABASE.organizations.get_valid_org_auth_token = AsyncMock(return_value=_mock_org_token())
        mock_fetch.return_value = OTPValue(value=raw, type=OTPType.TOTP)

        with structlog.testing.capture_logs() as logs:
            result = await poll_otp_value(
                organization_id="o_test",
                task_id="tsk_test",
                workflow_run_id="wr_test",
                workflow_permanent_id="wpid_test",
                totp_identifier=raw_identifier,
                totp_verification_url=raw_url,
            )

        assert result is not None
        assert result.value == raw

        for record in logs:
            assert raw not in repr(record)
            assert raw_identifier not in repr(record)
            assert raw_url not in repr(record)
            assert raw not in str(record.values())
            for value in record.values():
                assert raw not in str(value)
                assert raw_identifier not in str(value)
                assert raw_url not in str(value)
            assert "otp_value" not in record
            assert record.get("value") != raw

        success = next((r for r in logs if r.get("event") == "Got otp value"), None)
        assert success is not None
        assert success["task_id"] == "tsk_test"
        assert success["workflow_run_id"] == "wr_test"
        assert success["workflow_permanent_id"] == "wpid_test"
        assert "totp_identifier" not in success
        assert "totp_verification_url" not in success
        assert success["otp_type"] == "totp"
        assert success["otp_length"] == 6
        assert isinstance(success["otp_length"], int)
        assert str(success["otp_length"]) != raw

        polling = next((r for r in logs if r.get("event") == "Polling otp value"), None)
        assert polling is not None
        assert "totp_identifier" not in polling
        assert "totp_verification_url" not in polling
        assert raw not in str(polling.values())


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

    def test_active_credential_with_otpauth_uri_uses_uri_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.services import otp_service

        totp_uri = (
            "otpauth://totp/Example:user@example.com"
            "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
        )
        fake = _FakeWorkflowRunContext(
            values={"credentials": {"username": "u_b", "password": "p_b", "totp": "tot_b"}},
            secrets={"tot_b_value": totp_uri},
        )
        self._patch_workflow_context(monkeypatch, fake)
        generate_totp_code_mock = MagicMock(return_value="12345678")
        monkeypatch.setattr(otp_service, "generate_totp_code", generate_totp_code_mock)

        with skyvern_context.scoped(_scoped_context(active="credentials")):
            result = try_generate_totp_from_credential("wr_test")

        assert result is not None
        assert result.value == "12345678"
        assert len(result.value) == 8
        generate_totp_code_mock.assert_called_once_with(totp_uri)

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


class TestPostTotpVerificationUrlSeam:
    """`_post_totp_verification_url` must route through app.AGENT_FUNCTION so the
    cloud override can egress via the NAT proxy (static IP), like webhook/file-upload."""

    @pytest.mark.asyncio
    async def test_routes_through_agent_function_and_adapts_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        seam = AsyncMock(
            return_value=TOTPVerificationResponse(
                status_code=200,
                body='{"verification_code": "123456"}',
                headers={"content-type": "application/json"},
            )
        )
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, body, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={"X-Sig": "abc"},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is True
        assert body == {"verification_code": "123456"}
        seam.assert_awaited_once()
        kwargs = seam.await_args.kwargs
        assert kwargs["url"] == "https://example.com/totp"
        assert kwargs["payload"] == "{}"
        assert kwargs["headers"] == {"X-Sig": "abc"}
        assert kwargs["organization_id"] == "o_test"

    @pytest.mark.asyncio
    async def test_non_json_response_marks_is_json_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        seam = AsyncMock(
            return_value=TOTPVerificationResponse(
                status_code=200,
                body="not a json body",
                headers={"content-type": "text/plain"},
            )
        )
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, body, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is False
        assert body == "not a json body"

    @pytest.mark.asyncio
    async def test_json_body_with_non_json_content_type_marks_is_json_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit non-JSON Content-Type must trump the JSON-shaped body so a
        misconfigured customer endpoint still trips the non-JSON contract error
        downstream instead of being silently accepted."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        seam = AsyncMock(
            return_value=TOTPVerificationResponse(
                status_code=200,
                body='{"verification_code": "123456"}',
                headers={"content-type": "text/plain"},
            )
        )
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, body, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is False
        assert body == '{"verification_code": "123456"}'

    @pytest.mark.asyncio
    async def test_missing_content_type_falls_through_to_optimistic_json_parse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NATEgressProxyClient.deliver_webhook doesn't preserve upstream response
        headers, so the proxy path produces TOTPVerificationResponse(headers={}).
        The helper must still parse a JSON-shaped body as JSON in that case."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        seam = AsyncMock(
            return_value=TOTPVerificationResponse(
                status_code=200,
                body='{"verification_code": "123456"}',
                headers={},
            )
        )
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, body, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is True
        assert body == {"verification_code": "123456"}

    @pytest.mark.asyncio
    async def test_retries_then_raises_on_persistent_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        from skyvern.services import otp_service

        seam = AsyncMock(side_effect=httpx.ConnectError("boom"))
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        with pytest.raises(otp_service._TOTPWebhookRequestError):
            await otp_service._post_totp_verification_url(
                url="https://example.com/totp",
                signed_payload="{}",
                headers={},
                organization_id="o_test",
                max_attempts=2,
                retry_timeout=0,
            )

        assert seam.await_count == 2

    @pytest.mark.asyncio
    async def test_body_with_literal_control_chars_recovers_as_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A customer that relays a raw multi-line OTP email into
        ``verification_code`` produces a body with literal newlines, which strict
        JSON rejects. The helper must still recover it as a JSON object so the
        downstream extractor can read the field instead of failing the login."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        raw_email = "Verification Code\r\nYour one-time passcode is 123456\r\nThanks"
        body = '{\r\n  "task_id": "tsk_1",\r\n  "verification_code": "' + raw_email + '"\r\n}'
        seam = AsyncMock(return_value=TOTPVerificationResponse(status_code=200, body=body, headers={}))
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, parsed, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is True
        assert isinstance(parsed, dict)
        assert parsed["verification_code"] == raw_email

    @pytest.mark.asyncio
    async def test_body_with_unescaped_quotes_recovers_verification_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the relayed email contains unescaped quotes, even lenient JSON
        parsing fails. The helper must best-effort recover the verification_code
        field so the downstream LLM extractor can still pull the code."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        raw_email = 'Your "one-time" passcode is 123456'
        body = '{"task_id": "tsk_1", "verification_code": "' + raw_email + '"}'
        seam = AsyncMock(return_value=TOTPVerificationResponse(status_code=200, body=body, headers={}))
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, parsed, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is True
        assert isinstance(parsed, dict)
        assert parsed["verification_code"] == raw_email

    @pytest.mark.asyncio
    async def test_truly_non_json_body_still_marks_is_json_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A body with no recoverable verification_code (e.g. an HTML error page)
        must still trip the non-JSON contract path so genuinely broken
        integrations stay visible to operators."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        seam = AsyncMock(
            return_value=TOTPVerificationResponse(
                status_code=200,
                body="<html><body>Service Unavailable</body></html>",
                headers={},
            )
        )
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, body, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is False
        assert body == "<html><body>Service Unavailable</body></html>"

    @pytest.mark.asyncio
    async def test_forwarded_email_payload_recovers_verification_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mirrors a real-world payload shape: a customer relays an entire
        forwarded OTP email (multi-line, with literal CRLF line breaks) into
        verification_code. The literal control characters make it invalid strict
        JSON, but the helper must still recover the field so the downstream LLM
        extractor can read the code instead of terminating the login."""
        from skyvern.forge.agent_functions import TOTPVerificationResponse
        from skyvern.services import otp_service

        email_lines = [
            "Fw: Verification Code - Your Example Business One-Time Passcode for Secure Account Login",
            "",
            "Example Company",
            "www.example.comhttps://www.example.com",
            "",
            "CONFIDENTIALITY NOTICE: This e-mail and any attachments are intended only for the named "
            "recipient. If you received this in error, please notify the sender and delete it.",
            "",
            "________________________________",
            "From: Example Business noreply@mail.example.com",
            "Sent: Wednesday, June 3, 2026 11:01 AM",
            "Subject: Verification Code - Your Example Business One-Time Passcode for Secure Account Login",
            "",
            "WARNING! EXTERNAL EMAIL: Please confirm its authenticity before acting on any requests.",
            "________________________________",
            "[Verification Code]",
            "",
            "Hello Jane Doe,",
            "",
            "You requested to verify your Example Business account.",
            "",
            "[Verified]      Your verification code is: 123456",
            "",
            "[Important]     For your security, this code expires 10 minutes after it is requested.",
            "",
            "Terms of Use https://example.com/legal/terms | Your Privacy Center https://example.com/privacy",
            "(c) 2026 Example Corp. All rights reserved.",
            "",
        ]
        email = "\r\n".join(email_lines)
        body = '{\r\n  "task_id": "t_123123123",\r\n  "verification_code": "' + email + '"\r\n}'
        seam = AsyncMock(return_value=TOTPVerificationResponse(status_code=200, body=body, headers={}))
        monkeypatch.setattr(
            otp_service.app, "AGENT_FUNCTION", SimpleNamespace(post_totp_verification_request=seam), raising=False
        )

        status, _headers, parsed, is_json = await otp_service._post_totp_verification_url(
            url="https://example.com/totp",
            signed_payload="{}",
            headers={},
            organization_id="o_test",
            retry_timeout=0,
        )

        assert status == 200
        assert is_json is True
        assert isinstance(parsed, dict)
        assert parsed["task_id"] == "t_123123123"
        assert parsed["verification_code"] == email
        assert "123456" in parsed["verification_code"]


@pytest.mark.asyncio
async def test_get_otp_value_from_db_forwards_created_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_otp_value_from_db forwards created_after so the DB query can disqualify pre-run codes."""
    started_at = datetime(2026, 6, 8, 20, 3, 0)
    get_otp_codes = AsyncMock(return_value=[])
    with patch("skyvern.services.otp_service.app") as mock_app:
        mock_app.DATABASE.otp.get_otp_codes = get_otp_codes
        result = await _get_otp_value_from_db(
            "o_test",
            "otp@example.com",
            workflow_run_id="wr_test",
            created_after=started_at,
        )

    assert result is None
    get_otp_codes.assert_awaited_once()
    assert get_otp_codes.await_args.kwargs["created_after"] == started_at
    assert get_otp_codes.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert get_otp_codes.await_args.kwargs["include_unscoped_workflow_run"] is True


@pytest.mark.asyncio
async def test_get_otp_value_from_db_scopes_query_to_workflow_run_when_provided() -> None:
    """Run-scoped polling prefers exact rows but keeps unscoped email/SMS pushes eligible."""
    unscoped = SimpleNamespace(
        code="111111",
        otp_type=OTPType.TOTP,
        workflow_run_id=None,
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )
    other_run = SimpleNamespace(
        code="333333",
        otp_type=OTPType.TOTP,
        workflow_run_id="wr_other",
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )
    scoped = SimpleNamespace(
        code="222222",
        otp_type=OTPType.TOTP,
        workflow_run_id="wr_test",
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )

    async def get_otp_codes(**kwargs: object) -> list[SimpleNamespace]:
        assert kwargs["workflow_run_id"] == "wr_test"
        assert kwargs["include_unscoped_workflow_run"] is True
        return [other_run, scoped, unscoped]

    with patch("skyvern.services.otp_service.app") as mock_app:
        mock_app.DATABASE.otp.get_otp_codes = AsyncMock(side_effect=get_otp_codes)
        result = await _get_otp_value_from_db(
            "o_test",
            "otp@example.com",
            workflow_run_id="wr_test",
            created_after=datetime(2026, 6, 8, 20, 3, 0),
        )

    assert result == OTPValue(value="222222", type=OTPType.TOTP)
    mock_app.DATABASE.otp.get_otp_codes.assert_awaited_once()
    assert mock_app.DATABASE.otp.get_otp_codes.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert mock_app.DATABASE.otp.get_otp_codes.await_args.kwargs["include_unscoped_workflow_run"] is True


@pytest.mark.asyncio
async def test_get_otp_value_from_db_allows_unscoped_code_for_run_scoped_poll() -> None:
    unscoped = SimpleNamespace(
        code="111111",
        otp_type=OTPType.TOTP,
        workflow_run_id=None,
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )
    other_run = SimpleNamespace(
        code="333333",
        otp_type=OTPType.TOTP,
        workflow_run_id="wr_other",
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )
    get_otp_codes = AsyncMock(return_value=[other_run, unscoped])

    with patch("skyvern.services.otp_service.app") as mock_app:
        mock_app.DATABASE.otp.get_otp_codes = get_otp_codes
        result = await _get_otp_value_from_db(
            "o_test",
            "otp@example.com",
            workflow_run_id="wr_test",
            created_after=datetime(2026, 6, 8, 20, 3, 0),
        )

    assert result == OTPValue(value="111111", type=OTPType.TOTP)
    assert get_otp_codes.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert get_otp_codes.await_args.kwargs["include_unscoped_workflow_run"] is True


@pytest.mark.asyncio
async def test_get_otp_value_from_db_preserves_unscoped_lookup_without_workflow_run() -> None:
    unscoped = SimpleNamespace(
        code="111111",
        otp_type=OTPType.TOTP,
        workflow_run_id=None,
        workflow_id=None,
        task_id=None,
        expired_at=None,
    )
    get_otp_codes = AsyncMock(return_value=[unscoped])

    with patch("skyvern.services.otp_service.app") as mock_app:
        mock_app.DATABASE.otp.get_otp_codes = get_otp_codes
        result = await _get_otp_value_from_db("o_test", "otp@example.com")

    assert result == OTPValue(value="111111", type=OTPType.TOTP)
    assert get_otp_codes.await_args.kwargs["workflow_run_id"] is None
    assert get_otp_codes.await_args.kwargs["include_unscoped_workflow_run"] is False
