"""Redaction coverage for OTP webhook/email handling."""

from __future__ import annotations

from skyvern.services.otp_service import _response_body_preview, _totp_webhook_contract_error_reason


def test_response_body_preview_redacts_raw_verification_code_and_email_body() -> None:
    body = '{"verification_code":"Your sign-in code is 246810. Do not share this code. Reply-To: otp@example.test"}'

    preview = _response_body_preview(body)

    assert "246810" not in preview
    assert "otp@example.test" not in preview
    assert "[REDACTED_OTP_BODY]" in preview
    assert "json_like=true" in preview


def test_contract_error_reason_redacts_body_preview() -> None:
    reason = _totp_webhook_contract_error_reason(
        status_code=200,
        content_type="text/plain; charset=utf-8",
        response_body="verification_code=135790 email text",
    )

    assert "https://otp.example.test/hook" not in reason
    assert "135790" not in reason
    assert "verification_code=135790" not in reason
    assert "content_type=text/plain" in reason
    assert "charset" not in reason
    assert "[REDACTED_OTP_BODY]" in reason
