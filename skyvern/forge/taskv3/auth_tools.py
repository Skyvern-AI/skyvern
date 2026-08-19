"""Auth/verification tools for the native Task V3 engine.

The tool-loop is LLM-driven, so — like the CUA engine — verification codes are resolved on demand:
the model recognizes a code field, calls ``get_verification_code``, and types the returned value.
Resolution reuses the shared ``otp_service`` waterfall (payload -> credential TOTP -> webhook/email/
DB poll), which routes cloud behavior through the ``AGENT_FUNCTION`` seam, so this module stays
OSS-clean. The resolved code is registered for redaction from this task's artifacts/logs.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.services.otp_service import (
    extract_totp_from_navigation_inputs,
    has_credential_totp_candidate,
    resolve_otp_value,
)

LOG = structlog.get_logger()

_GUIDANCE = (
    "\n- If the page asks for a one-time / 2FA / verification code, call `get_verification_code` and "
    "`type` the returned value into the field. Never invent or guess a code."
)


def build_auth_tools(task: Task) -> tuple[list[ToolSpec], str]:
    """Return (tools, system-prompt guidance) for verification-code handling, or ([], "") when the
    task has no verification-code source configured (so the tool isn't offered needlessly)."""
    # v3 only runs code sources it can actually service: a totp_verification_url task stays on the step
    # engine (the v3 dispatch gate excludes it), so it never reaches this builder — gating on it would be
    # dead. Credential-vault TOTP discrimination needs active_credential_parameter_key, which only v1's
    # action handler sets, so multiple TOTP credentials can't disambiguate on v3 — not reachable today
    # (v3 runs bare tasks, not workflow blocks); revisit with v3 credential support.
    payload_otp_value = extract_totp_from_navigation_inputs(task.navigation_payload)
    has_code_source = bool(
        (payload_otp_value is not None and payload_otp_value.get_otp_type() == OTPType.TOTP)
        or task.totp_identifier
        or has_credential_totp_candidate(task.workflow_run_id)
    )
    if not has_code_source:
        return [], ""

    async def _get_verification_code(args: dict[str, Any]) -> ToolResult:
        try:
            otp_value = await resolve_otp_value(task, expected_otp_type=OTPType.TOTP)
        except (NoTOTPVerificationCodeFound, FailedToGetTOTPVerificationCode) as exc:
            return ToolResult.error(
                f"no verification code available yet ({type(exc).__name__}). If the page has not sent "
                "one, trigger it first, then call get_verification_code again."
            )
        except Exception as exc:
            LOG.warning("task_v3 get_verification_code failed", task_id=task.task_id, exc_info=True)
            return ToolResult.error(f"verification code lookup failed: {type(exc).__name__}")

        if otp_value is None or otp_value.get_otp_type() != OTPType.TOTP:
            return ToolResult.error("no verification code available for this task")

        code = otp_value.value
        context = skyvern_context.current()
        if context is not None:
            # Redact the code from this task's artifacts/logs (task-scoped, so bare tasks are covered).
            context.register_secret_value(code)
        return ToolResult.ok(f"verification_code: {code}")

    tool = ToolSpec(
        name="get_verification_code",
        description=(
            "Fetch the one-time / 2FA verification code for this task (from the connected email inbox, "
            "the configured verification webhook, or the saved credential's authenticator). Call this "
            "when the page asks for a verification / OTP / 2FA code, then type the returned value. "
            "Never invent a code."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_get_verification_code,
        billable=False,
    )
    return [tool], _GUIDANCE
