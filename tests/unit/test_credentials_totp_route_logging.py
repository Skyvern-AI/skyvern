from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from fastapi import HTTPException

from skyvern.forge.sdk.routes import credentials
from skyvern.forge.sdk.schemas.totp_codes import TOTPCodeCreate


def _database_with_otp_create(create_otp_code: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code))


def _assert_raw_values_not_logged(logs: list[dict[str, object]], *raw_values: str) -> None:
    for record in logs:
        record_repr = repr(record)
        for raw_value in raw_values:
            assert raw_value not in record_repr
            for value in record.values():
                assert raw_value not in str(value)


@pytest.mark.asyncio
async def test_send_totp_code_save_log_redacts_identifier_but_stores_raw_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_identifier = "qa-email-otp@example.test"
    raw_content = "135790"
    create_otp_code = AsyncMock(return_value=SimpleNamespace(totp_code_id="otp_1"))
    monkeypatch.setattr(credentials.app, "DATABASE", _database_with_otp_create(create_otp_code))

    with structlog.testing.capture_logs() as logs:
        result = await credentials.send_totp_code(
            TOTPCodeCreate(totp_identifier=raw_identifier, content=raw_content),
            curr_org=SimpleNamespace(organization_id="o_test"),
        )

    assert result.totp_code_id == "otp_1"
    save_log = next((r for r in logs if r.get("event") == "Saving OTP code"), None)
    assert save_log is not None
    assert save_log["totp_identifier"] == "[REDACTED_OTP_IDENTIFIER]"
    _assert_raw_values_not_logged(logs, raw_identifier, raw_content)

    create_otp_code.assert_awaited_once()
    storage_kwargs = create_otp_code.await_args.kwargs
    assert storage_kwargs["totp_identifier"] == raw_identifier
    assert storage_kwargs["content"] == raw_content
    assert storage_kwargs["code"] == raw_content


@pytest.mark.asyncio
async def test_send_totp_code_parse_failure_log_redacts_identifier_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_identifier = "qa-email-otp@example.test"
    raw_content = "Use 135790 to finish sign in for qa-email-otp@example.test."
    create_otp_code = AsyncMock()
    monkeypatch.setattr(credentials.app, "DATABASE", _database_with_otp_create(create_otp_code))
    monkeypatch.setattr(credentials, "parse_otp_login", AsyncMock(return_value=None))

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(HTTPException) as exc_info:
            await credentials.send_totp_code(
                TOTPCodeCreate(totp_identifier=raw_identifier, content=raw_content),
                curr_org=SimpleNamespace(organization_id="o_test"),
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Failed to parse otp login"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    create_otp_code.assert_not_awaited()

    error_log = next((r for r in logs if r.get("event") == "Failed to parse otp login"), None)
    assert error_log is not None
    assert error_log["organization_id"] == "o_test"
    assert error_log["totp_identifier"] == "[REDACTED_OTP_IDENTIFIER]"
    assert error_log["content_length"] == len(raw_content)
    assert "content" not in error_log
    _assert_raw_values_not_logged(logs, raw_identifier, raw_content, "135790")


@pytest.mark.asyncio
async def test_send_totp_code_parser_exception_log_redacts_raw_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_identifier = "qa-email-otp@example.test"
    raw_content = "Email body says OTP 246810 for qa-email-otp@example.test."
    create_otp_code = AsyncMock()
    monkeypatch.setattr(credentials.app, "DATABASE", _database_with_otp_create(create_otp_code))

    async def parse_otp_login_raises(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"parser saw {raw_content} for {raw_identifier}")

    monkeypatch.setattr(credentials, "parse_otp_login", parse_otp_login_raises)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(HTTPException) as exc_info:
            await credentials.send_totp_code(
                TOTPCodeCreate(totp_identifier=raw_identifier, content=raw_content),
                curr_org=SimpleNamespace(organization_id="o_test"),
            )

    # A raised parser is a backend/dependency failure, not bad caller input, so the
    # endpoint returns a retryable 502 with a static detail that never echoes the payload.
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "OTP extraction is temporarily unavailable. Please retry in a few minutes."
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert raw_identifier not in str(exc_info.value)
    assert raw_content not in str(exc_info.value)
    assert "246810" not in str(exc_info.value)
    create_otp_code.assert_not_awaited()

    error_log = next((r for r in logs if r.get("event") == "Failed to parse otp login"), None)
    assert error_log is not None
    assert error_log["organization_id"] == "o_test"
    assert error_log["totp_identifier"] == "[REDACTED_OTP_IDENTIFIER]"
    assert error_log["content_length"] == len(raw_content)
    assert error_log["exception_type"] == "RuntimeError"
    assert "content" not in error_log
    _assert_raw_values_not_logged(logs, raw_identifier, raw_content, "246810")
