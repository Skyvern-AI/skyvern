from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

REDACTED_OTP_VALUE = "<redacted otp value>"
REDACTED_OTP_IDENTIFIER = "<redacted otp identifier>"
REDACTED_OTP_URL = "<redacted otp url>"
REDACTED_OTP_SECRET = "<redacted otp secret>"
SDK_INPUT_TEXT_ACTION_TYPE = "ai_input_text"  # Mirrors SdkActionType.AI_INPUT_TEXT without importing sdk_actions.


def redact_action_for_log(action: BaseModel) -> dict[str, Any]:
    return redact_action_payload_for_log(action.model_dump())


def redact_action_payload_for_log(action_payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted_payload = dict(action_payload)
    action_type = redacted_payload.get("action_type")
    sdk_action_type = redacted_payload.get("type")

    if action_type == "input_text":
        return redact_input_text_payload_for_log(redacted_payload, value_key="text")

    if sdk_action_type == SDK_INPUT_TEXT_ACTION_TYPE:
        return redact_input_text_payload_for_log(redacted_payload, value_key="value")

    return redacted_payload


def redact_input_text_payload_for_log(action_payload: Mapping[str, Any], *, value_key: str) -> dict[str, Any]:
    redacted_payload = dict(action_payload)
    if _is_otp_input_payload(redacted_payload):
        if redacted_payload.get(value_key):
            redacted_payload[value_key] = REDACTED_OTP_VALUE
        if redacted_payload.get("response"):
            redacted_payload["response"] = REDACTED_OTP_VALUE
        if redacted_payload.get("totp_identifier"):
            redacted_payload["totp_identifier"] = REDACTED_OTP_IDENTIFIER
        if redacted_payload.get("totp_url"):
            redacted_payload["totp_url"] = REDACTED_OTP_URL
        if isinstance(redacted_payload.get("totp_timing_info"), Mapping):
            timing_info = dict(redacted_payload["totp_timing_info"])
            if timing_info.get("totp_secret"):
                timing_info["totp_secret"] = REDACTED_OTP_SECRET
            redacted_payload["totp_timing_info"] = timing_info
    return redacted_payload


def _is_otp_input_payload(action_payload: Mapping[str, Any]) -> bool:
    # Keep this in sync with OTP marker fields on SDK/web InputTextAction models.
    if action_payload.get("totp_identifier") or action_payload.get("totp_url"):
        return True
    if action_payload.get("totp_code_required"):
        return True
    if isinstance(action_payload.get("totp_timing_info"), Mapping):
        return True

    return False
