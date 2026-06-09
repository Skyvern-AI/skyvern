import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pyotp
import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext

from skyvern.config import settings
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.aiohttp_helper import DEFAULT_REQUEST_TIMEOUT
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.totp_codes import OTPType

LOG = structlog.get_logger()

_MFA_PARAMETER_KEY_HINTS = ("mfa", "otp", "verification")
# Keys that contain an MFA hint but are TOTP *metadata*, not actual OTP codes.
# "totpidentifier" matches "otp" but carries a lookup key, not a 6-digit code.
_MFA_METADATA_KEY_HINTS = ("identifier", "url", "secret", "seed", "key")
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]")
_EXPECTED_TOTP_WEBHOOK_RESPONSE_SHAPE = '{"verification_code":"123456"}'
# Recovers the verification_code value when the surrounding JSON is malformed
# (e.g. unescaped quotes inside a relayed email). Assumes verification_code is
# the final field, which is the common shape.
_VERIFICATION_CODE_FIELD_PATTERN = re.compile(r'"verification_code"\s*:\s*"(?P<value>.*)"\s*}\s*\Z', re.DOTALL)
_TOTP_WEBHOOK_BODY_PREVIEW_LIMIT = 200
_TOTP_WEBHOOK_REQUEST_MAX_ATTEMPTS = 3
_TOTP_WEBHOOK_REQUEST_RETRY_TIMEOUT_SECONDS = 5

MFANavigationPayload = dict | list | str | None
_TOTPWebhookPostResponse = tuple[int, dict[str, str], Any, bool]


class _TOTPWebhookRequestError(Exception):
    pass


class OTPValue(BaseModel):
    value: str = Field(..., description="The value of the OTP code.")
    type: OTPType | None = Field(None, description="The type of the OTP code.")

    def get_otp_type(self) -> OTPType:
        if self.type:
            return self.type
        value = self.value.strip().lower()
        if value.startswith("https://") or value.startswith("http://"):
            return OTPType.MAGIC_LINK
        return OTPType.TOTP


class OTPResultParsedByLLM(BaseModel):
    reasoning: str = Field(..., description="The reasoning of the OTP code.")
    otp_type: OTPType | None = Field(None, description="The type of the OTP code.")
    otp_value_found: bool = Field(..., description="Whether the OTP value is found.")
    otp_value: str | None = Field(None, description="The OTP value.")


async def parse_otp_login(
    content: str,
    organization_id: str,
    enforced_otp_type: OTPType | None = None,
) -> OTPValue | None:
    prompt = prompt_engine.load_prompt(
        "parse-otp-login",
        content=content,
        enforced_otp_type=enforced_otp_type.value if enforced_otp_type else None,
    )
    resp = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, prompt_name="parse-otp-login", organization_id=organization_id
    )
    LOG.info("OTP Login Parser Response", resp=resp, enforced_otp_type=enforced_otp_type)
    otp_result = OTPResultParsedByLLM.model_validate(resp)
    if otp_result.otp_value_found and otp_result.otp_value:
        return OTPValue(value=otp_result.otp_value, type=otp_result.otp_type)
    return None


def _is_mfa_like_parameter_key(key: object) -> bool:
    """Return True when a payload key appears to represent an MFA/OTP code value.

    Excludes TOTP metadata keys (identifier, url, secret, etc.) that contain an
    MFA hint but carry lookup/config data rather than an actual verification code.
    """
    normalized_key = _NON_ALNUM_PATTERN.sub("", str(key).lower())
    if any(meta in normalized_key for meta in _MFA_METADATA_KEY_HINTS):
        return False
    return any(hint in normalized_key for hint in _MFA_PARAMETER_KEY_HINTS)


def extract_totp_from_navigation_inputs(navigation_payload: MFANavigationPayload) -> OTPValue | None:
    """Extract TOTP from runtime navigation inputs.

    Runtime inline OTP extraction is intentionally payload-only.
    """
    if not isinstance(navigation_payload, (dict, list)):
        return None

    traversal_stack: list[dict | list | str] = [navigation_payload]
    visited_container_ids: set[int] = set()

    while traversal_stack:
        current_item = traversal_stack.pop()

        if isinstance(current_item, str):
            return OTPValue(value=current_item, type=OTPType.TOTP)

        current_id = id(current_item)
        if current_id in visited_container_ids:
            continue
        visited_container_ids.add(current_id)

        if isinstance(current_item, list):
            for item in reversed(current_item):
                if isinstance(item, (dict, list)):
                    traversal_stack.append(item)
            continue

        for key, value in reversed(list(current_item.items())):
            if isinstance(value, (dict, list)):
                traversal_stack.append(value)
            if not _is_mfa_like_parameter_key(key):
                continue
            if not isinstance(value, str):
                continue
            candidate_value = value.strip()
            if candidate_value:
                traversal_stack.append(candidate_value)

    return None


def _get_header_value(headers: dict[str, str], header_name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == header_name.lower():
            return value
    return None


def _format_content_type_for_error(content_type: str | None) -> str:
    return content_type if content_type is not None else "<absent>"


def _response_body_preview(response_body: Any) -> str:
    body = response_body if isinstance(response_body, str) else str(response_body)
    if len(body) <= _TOTP_WEBHOOK_BODY_PREVIEW_LIMIT:
        return body
    return f"{body[:_TOTP_WEBHOOK_BODY_PREVIEW_LIMIT]}... (truncated)"


def _totp_webhook_contract_error_reason(
    *,
    url: str,
    status_code: int,
    content_type: str | None,
    response_body: Any,
) -> str:
    return (
        "TOTP webhook returned HTTP 200 but the response was not JSON. "
        f"endpoint_url={url} "
        f"HTTP status={status_code} "
        f"content_type={_format_content_type_for_error(content_type)} "
        f"body_preview={_response_body_preview(response_body)!r} "
        f"expected_response_shape={_EXPECTED_TOTP_WEBHOOK_RESPONSE_SHAPE}"
    )


def _coerce_totp_response_body(body: str) -> tuple[Any, bool]:
    """Decode a TOTP webhook body into a JSON value, tolerating the malformations
    customers produce when relaying a raw OTP email into ``verification_code``.

    Returns ``(value, True)`` when a JSON value is recovered, else ``(body, False)``.
    The downstream extractor runs the value through the LLM, so an imperfectly
    recovered string is still useful — better than failing the whole login.
    """
    try:
        return json.loads(body), True
    except (json.JSONDecodeError, ValueError):
        pass
    # Literal control characters (raw email newlines/tabs) are the most common
    # malformation; strict=False tolerates them inside string values.
    try:
        return json.loads(body, strict=False), True
    except (json.JSONDecodeError, ValueError):
        pass
    match = _VERIFICATION_CODE_FIELD_PATTERN.search(body)
    if match is not None:
        return {"verification_code": match.group("value")}, True
    return body, False


async def _post_totp_verification_url(
    *,
    url: str,
    signed_payload: str,
    headers: dict[str, str],
    organization_id: str,
    max_attempts: int = _TOTP_WEBHOOK_REQUEST_MAX_ATTEMPTS,
    retry_timeout: float = _TOTP_WEBHOOK_REQUEST_RETRY_TIMEOUT_SECONDS,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> _TOTPWebhookPostResponse:
    # Routed through app.AGENT_FUNCTION so cloud egresses via the NAT proxy
    # (static IP), matching webhook and file-upload delivery.
    for attempt in range(max_attempts):
        try:
            response = await app.AGENT_FUNCTION.post_totp_verification_request(
                url=url,
                payload=signed_payload,
                headers=headers,
                timeout_seconds=timeout,
                organization_id=organization_id,
            )
            # Content-Type gate: only trust an explicit non-JSON header to mean
            # "this is not JSON". Missing header (e.g. proxy responses, which
            # don't preserve upstream headers) falls through to tolerant JSON
            # parsing — customer TOTP endpoints contractually return JSON.
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "json" not in content_type:
                return response.status_code, response.headers, response.body, False
            parsed, is_json = _coerce_totp_response_body(response.body)
            return response.status_code, response.headers, parsed, is_json
        except Exception:
            LOG.debug(
                "TOTP webhook request attempt failed",
                endpoint_url=url,
                attempt=attempt + 1,
                max_attempts=max_attempts,
                exc_info=True,
            )
            if attempt < max_attempts - 1 and retry_timeout > 0:
                await asyncio.sleep(retry_timeout)
    raise _TOTPWebhookRequestError(f"Failed post request url={url}")


def _try_generate_totp_for_credential(
    workflow_run_context: "WorkflowRunContext",
    credential_key: str,
    workflow_run_id: str,
) -> OTPValue | None:
    value = workflow_run_context.values.get(credential_key)
    if not isinstance(value, dict):
        return None
    totp_secret_id = value.get("totp")
    if not totp_secret_id or not isinstance(totp_secret_id, str):
        return None
    totp_secret_key = workflow_run_context.totp_secret_value_key(totp_secret_id)
    totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
    if not totp_secret:
        return None
    try:
        code = pyotp.TOTP(totp_secret).now()
        LOG.info(
            "Generated TOTP from credential secret",
            workflow_run_id=workflow_run_id,
            credential_key=credential_key,
        )
        return OTPValue(value=code, type=OTPType.TOTP)
    except Exception:
        LOG.warning(
            "Failed to generate TOTP from credential secret",
            workflow_run_id=workflow_run_id,
            credential_key=credential_key,
            exc_info=True,
        )
        return None


def has_credential_totp_candidate(workflow_run_id: str | None) -> bool:
    """Return True when try_generate_totp_from_credential would have a credential to consult.

    Mirrors try_generate_totp_from_credential's selection: active-with-TOTP if an
    active credential is recorded, else exactly one TOTP-bearing candidate.
    Used to drive prompt gating and classifier branches without actually
    generating a code.
    """
    if not workflow_run_id:
        return False

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if not workflow_run_context:
        return False

    current_context = skyvern_context.current()
    active_credential_key = current_context.active_credential_parameter_key if current_context else None
    if active_credential_key:
        value = workflow_run_context.values.get(active_credential_key)
        return isinstance(value, dict) and isinstance(value.get("totp"), str)

    candidate_keys = [
        key
        for key, value in workflow_run_context.values.items()
        if isinstance(value, dict) and isinstance(value.get("totp"), str)
    ]
    return len(candidate_keys) == 1


def try_generate_totp_from_credential(workflow_run_id: str | None) -> OTPValue | None:
    """Generate a TOTP only for the credential the agent is currently typing into.

    Falls back to single-credential heuristic when no active credential is recorded.
    """
    if not workflow_run_id:
        return None

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if not workflow_run_context:
        return None

    current_context = skyvern_context.current()
    active_credential_key = current_context.active_credential_parameter_key if current_context else None

    if active_credential_key:
        return _try_generate_totp_for_credential(workflow_run_context, active_credential_key, workflow_run_id)

    candidate_keys = [
        key
        for key, value in workflow_run_context.values.items()
        if isinstance(value, dict) and isinstance(value.get("totp"), str)
    ]
    if len(candidate_keys) != 1:
        if len(candidate_keys) > 1:
            LOG.info(
                "Skipping credential-TOTP: multiple credentials with TOTP and no active credential",
                workflow_run_id=workflow_run_id,
                candidate_credential_keys=candidate_keys,
            )
        return None
    return _try_generate_totp_for_credential(workflow_run_context, candidate_keys[0], workflow_run_id)


async def resolve_otp_value(task: "Task") -> OTPValue | None:
    """Resolve the OTP value to use for a verification step.

    Priority is payload -> credential-backed TOTP -> webhook polling. The
    workflow-run metadata lookup needed by polling is performed lazily so
    payload/credential resolutions do not touch the database. Polling raises
    NoTOTPVerificationCodeFound or FailedToGetTOTPVerificationCode on timeout;
    those propagate so callers can build the right terminate action. Returns
    None when no source is configured.
    """
    otp_value = extract_totp_from_navigation_inputs(task.navigation_payload)
    if otp_value:
        return otp_value

    otp_value = try_generate_totp_from_credential(task.workflow_run_id)
    if otp_value:
        return otp_value

    if (task.totp_verification_url or task.totp_identifier) and task.organization_id:
        workflow_id: str | None = None
        workflow_permanent_id: str | None = None
        if task.workflow_run_id:
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(task.workflow_run_id)
            if workflow_run:
                workflow_id = workflow_run.workflow_id
                workflow_permanent_id = workflow_run.workflow_permanent_id
        return await poll_otp_value(
            organization_id=task.organization_id,
            task_id=task.task_id,
            workflow_id=workflow_id,
            workflow_run_id=task.workflow_run_id,
            workflow_permanent_id=workflow_permanent_id,
            totp_verification_url=task.totp_verification_url,
            totp_identifier=task.totp_identifier,
        )

    return None


async def poll_otp_value(
    organization_id: str,
    task_id: str | None = None,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
) -> OTPValue | None:
    timeout = timedelta(minutes=settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_token = await app.DATABASE.organizations.get_valid_org_auth_token(
        organization_id, OrganizationAuthTokenType.api.value
    )
    if not org_token:
        LOG.error("Failed to get organization token when trying to get otp value")
        return None
    LOG.info(
        "Polling otp value",
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
    )
    consecutive_failures = 0
    last_error_reason: str | None = None
    while True:
        await asyncio.sleep(10)
        if datetime.utcnow() > timeout_datetime:
            if consecutive_failures > 0 and last_error_reason is not None:
                LOG.warning(
                    "Polling otp value timed out while webhook was still failing",
                    consecutive_failures=consecutive_failures,
                    last_error_reason=last_error_reason,
                )
                raise FailedToGetTOTPVerificationCode(
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    workflow_id=workflow_id or workflow_permanent_id,
                    totp_verification_url=totp_verification_url,
                    totp_identifier=totp_identifier,
                    reason=last_error_reason,
                )
            LOG.warning("Polling otp value timed out")
            raise NoTOTPVerificationCodeFound(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id or workflow_permanent_id,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
            )
        otp_value: OTPValue | None = None
        try:
            if totp_verification_url:
                otp_value = await _get_otp_value_from_url(
                    organization_id,
                    totp_verification_url,
                    org_token.token,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    workflow_permanent_id=workflow_permanent_id,
                )
            elif totp_identifier:
                otp_value = await _get_otp_value_from_db(
                    organization_id,
                    totp_identifier,
                    task_id=task_id,
                    workflow_id=workflow_permanent_id,
                    workflow_run_id=workflow_run_id,
                )
        except FailedToGetTOTPVerificationCode as e:
            consecutive_failures += 1
            last_error_reason = e.reason
            LOG.warning(
                "OTP fetch failed, will retry until wall-clock timeout",
                consecutive_failures=consecutive_failures,
                last_error_reason=e.reason,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
            )
            continue
        consecutive_failures = 0
        last_error_reason = None
        if otp_value:
            LOG.info("Got otp value", otp_value=otp_value)
            return otp_value


async def _get_otp_value_from_url(
    organization_id: str,
    url: str,
    api_key: str,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
) -> OTPValue | None:
    request_data = {}
    if task_id:
        request_data["task_id"] = task_id
    if workflow_run_id:
        request_data["workflow_run_id"] = workflow_run_id
    if workflow_permanent_id:
        request_data["workflow_permanent_id"] = workflow_permanent_id
    signed_data = generate_skyvern_webhook_signature(
        payload=request_data,
        api_key=api_key,
    )
    try:
        status_code, response_headers, response_body, is_json_response = await _post_totp_verification_url(
            url=url,
            signed_payload=signed_data.signed_payload,
            headers=signed_data.headers,
            organization_id=organization_id,
        )
    except Exception as e:
        LOG.error("Failed to get otp value from url", totp_verification_url=url, exc_info=True)
        raise FailedToGetTOTPVerificationCode(
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_permanent_id,
            totp_verification_url=url,
            reason=str(e),
        )
    content_type = _get_header_value(response_headers, "Content-Type")
    if status_code != 200:
        LOG.warning(
            "TOTP webhook returned non-200 response",
            endpoint_url=url,
            http_status=status_code,
            content_type=content_type,
            body_preview=_response_body_preview(response_body),
        )
        return None

    if not is_json_response:
        reason = _totp_webhook_contract_error_reason(
            url=url,
            status_code=status_code,
            content_type=content_type,
            response_body=response_body,
        )
        LOG.error(
            "TOTP webhook returned non-JSON response",
            endpoint_url=url,
            http_status=status_code,
            content_type=content_type,
            body_preview=_response_body_preview(response_body),
            expected_response_shape=_EXPECTED_TOTP_WEBHOOK_RESPONSE_SHAPE,
        )
        raise FailedToGetTOTPVerificationCode(
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_permanent_id,
            totp_verification_url=url,
            reason=reason,
        )

    if not isinstance(response_body, dict):
        LOG.warning(
            "TOTP webhook response body is not a JSON object",
            endpoint_url=url,
            http_status=status_code,
            content_type=content_type,
            response_json_type=type(response_body).__name__,
            expected_response_shape=_EXPECTED_TOTP_WEBHOOK_RESPONSE_SHAPE,
        )
        return None

    content = response_body.get("verification_code", None)
    if not content:
        LOG.warning(
            "No verification_code found in TOTP webhook response",
            endpoint_url=url,
            http_status=status_code,
            content_type=content_type,
            response_keys=list(response_body.keys()),
            expected_response_shape=_EXPECTED_TOTP_WEBHOOK_RESPONSE_SHAPE,
        )
        return None

    otp_value: OTPValue | None = OTPValue(value=content, type=OTPType.TOTP)
    if isinstance(content, str) and len(content) > 10:
        try:
            otp_value = await parse_otp_login(content, organization_id)
        except Exception:
            LOG.warning("faile to parse content by LLM call", exc_info=True)

    if not otp_value:
        LOG.warning(
            "Failed to parse otp login from the totp url",
            content_preview=_response_body_preview(content),
        )
        return None

    return otp_value


async def _get_otp_value_from_db(
    organization_id: str,
    totp_identifier: str,
    task_id: str | None = None,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> OTPValue | None:
    totp_codes = await app.DATABASE.otp.get_otp_codes(organization_id=organization_id, totp_identifier=totp_identifier)
    for totp_code in totp_codes:
        if totp_code.workflow_run_id and workflow_run_id and totp_code.workflow_run_id != workflow_run_id:
            continue
        if totp_code.workflow_id and workflow_id and totp_code.workflow_id != workflow_id:
            continue
        if totp_code.task_id and totp_code.task_id != task_id:
            continue
        if totp_code.expired_at and totp_code.expired_at < datetime.utcnow():
            continue
        return OTPValue(value=totp_code.code, type=totp_code.otp_type)
    return None
