import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyotp
import structlog
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.notification.factory import NotificationRegistryFactory
from skyvern.forge.sdk.schemas.totp_codes import OTPType

LOG = structlog.get_logger()

MFANavigationPayload = dict | list | str | None

_MIN_OTP_DIGITS = 4
_MAX_OTP_DIGITS = 10
_OTP_CONTEXT_TERMS = (
    "verification code",
    "authentication code",
    "security code",
    "otp",
    "mfa",
    "2fa",
    "two-factor",
    "two factor",
    "one-time password",
    "one time password",
    "one-time code",
    "one time code",
)
_OTP_INPUT_ACTION_TERMS = ("input", "enter", "type", "fill", "use", "submit")
_MFA_NAVIGATION_PAYLOAD_KEYS_NORMALIZED = {
    "verificationcode",
    "mfachoice",
    "mfacode",
    "otp",
    "otpcode",
    "twofactorcode",
    "2facode",
    "authenticationcode",
    "authcode",
}
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]")


def _build_regex_alternation(terms: tuple[str, ...]) -> str:
    """Build a safe regex alternation fragment from plain text terms."""
    return "|".join(re.escape(term) for term in terms)


_OTP_TERM_ALTERNATION = _build_regex_alternation(_OTP_CONTEXT_TERMS)
_OTP_ACTION_TERM_ALTERNATION = _build_regex_alternation(_OTP_INPUT_ACTION_TERMS)
_OTP_DIGITS_PATTERN = rf"\d{{{_MIN_OTP_DIGITS},{_MAX_OTP_DIGITS}}}"
_OTP_CODE_PATTERN = re.compile(rf"^{_OTP_DIGITS_PATTERN}$")
_OTP_TEXT_BEFORE_CODE_PATTERN = re.compile(
    rf"\b(?:{_OTP_TERM_ALTERNATION})\b[^\d]{{0,40}}({_OTP_DIGITS_PATTERN})\b",
    re.IGNORECASE,
)
_OTP_CODE_BEFORE_TEXT_PATTERN = re.compile(
    rf"\b({_OTP_DIGITS_PATTERN})\b[^\w]{{0,20}}(?:{_OTP_TERM_ALTERNATION})\b",
    re.IGNORECASE,
)
_OTP_CONTEXT_PATTERN = re.compile(
    rf"\b(?:{_OTP_TERM_ALTERNATION})\b",
    re.IGNORECASE,
)
_OTP_INPUT_ACTION_CODE_PATTERN = re.compile(
    rf"\b(?:{_OTP_ACTION_TERM_ALTERNATION})\b[^\d]{{0,30}}({_OTP_DIGITS_PATTERN})\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class OTPPollContext:
    organization_id: str
    task_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_permanent_id: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None

    @property
    def needs_manual_input(self) -> bool:
        return not self.totp_verification_url


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


def _iter_mfa_payload_values(payload: MFANavigationPayload) -> list[str]:
    """Collect candidate MFA values while preserving recursive traversal order.

    Traversal is cycle-safe to avoid recursive blowups for malformed payload objects.
    """
    if not isinstance(payload, (dict, list)):
        return []

    values: list[str] = []
    traversal_stack: list[dict | list | str] = [payload]
    visited_container_ids: set[int] = set()

    while traversal_stack:
        current_item = traversal_stack.pop()
        if isinstance(current_item, str):
            values.append(current_item)
            continue

        current_id = id(current_item)
        if current_id in visited_container_ids:
            continue
        visited_container_ids.add(current_id)

        if isinstance(current_item, dict):
            for key, value in reversed(list(current_item.items())):
                if isinstance(value, (dict, list)):
                    traversal_stack.append(value)
                if _normalize_payload_key(key) in _MFA_NAVIGATION_PAYLOAD_KEYS_NORMALIZED:
                    candidate_value = _coerce_candidate_code_source(value)
                    if candidate_value is not None:
                        traversal_stack.append(candidate_value)
        else:
            for item in reversed(current_item):
                if isinstance(item, (dict, list)):
                    traversal_stack.append(item)

    return values


def _normalize_payload_key(key: object) -> str:
    """Normalize payload keys for alias matching across separators and casing."""
    return _NON_ALNUM_PATTERN.sub("", str(key).lower())


def _coerce_candidate_code_source(value: object) -> str | None:
    """Coerce alias values to strings while intentionally rejecting bools."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def extract_totp_from_text(text: object, *, assume_otp_context: bool = False) -> OTPValue | None:
    """Extract a numeric OTP from free-form text with optional OTP-context override."""
    if not isinstance(text, str) or not text:
        return None

    stripped_text = text.strip()
    if not stripped_text:
        return None

    for pattern in (_OTP_TEXT_BEFORE_CODE_PATTERN, _OTP_CODE_BEFORE_TEXT_PATTERN):
        match = pattern.search(stripped_text)
        if match:
            return OTPValue(value=match.group(1), type=OTPType.TOTP)

    context_found = assume_otp_context or bool(_OTP_CONTEXT_PATTERN.search(stripped_text))
    if not context_found:
        return None

    input_action_match = _OTP_INPUT_ACTION_CODE_PATTERN.search(stripped_text)
    if input_action_match:
        return OTPValue(value=input_action_match.group(1), type=OTPType.TOTP)

    return None


def extract_totp_from_navigation_payload(payload: MFANavigationPayload) -> OTPValue | None:
    """Extract a TOTP code from navigation payload using explicit MFA aliases.

    The extractor is intentionally strict:
    - only exact alias keys are considered
    - values must be numeric and between 4 and 10 digits
    """
    for value in _iter_mfa_payload_values(payload):
        stripped_value = value.strip()
        if _OTP_CODE_PATTERN.fullmatch(stripped_value):
            return OTPValue(value=stripped_value, type=OTPType.TOTP)
        otp_from_text = extract_totp_from_text(stripped_value, assume_otp_context=True)
        if otp_from_text:
            return otp_from_text

    if isinstance(payload, str):
        return extract_totp_from_text(payload, assume_otp_context=True)

    return None


def extract_totp_from_navigation_inputs(
    navigation_payload: MFANavigationPayload, navigation_goal: object
) -> OTPValue | None:
    """Extract TOTP from runtime navigation inputs with explicit precedence.

    Priority:
    1. `navigation_payload` explicit MFA aliases
    2. `navigation_goal` textual instructions (e.g. "Input 520265")
    """
    otp_value = extract_totp_from_navigation_payload(navigation_payload)
    if otp_value:
        return otp_value
    return extract_totp_from_text(navigation_goal)


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


def try_generate_totp_from_credential(workflow_run_id: str | None) -> OTPValue | None:
    """Try to generate a TOTP code from a credential secret stored in the workflow run context.

    Scans workflow_run_context.values for credential entries with a "totp" key
    (e.g. Bitwarden, 1Password, Azure Key Vault credentials) and generates a
    TOTP code using pyotp. This should be checked BEFORE poll_otp_value so that
    credential-based TOTP takes priority over webhook (totp_url) and totp_identifier.
    """
    if not workflow_run_id:
        return None

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if not workflow_run_context:
        return None

    for key, value in workflow_run_context.values.items():
        if isinstance(value, dict) and "totp" in value:
            totp_secret_id = value.get("totp")
            if not totp_secret_id or not isinstance(totp_secret_id, str):
                continue
            totp_secret_key = workflow_run_context.totp_secret_value_key(totp_secret_id)
            totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
            if totp_secret:
                try:
                    code = pyotp.TOTP(totp_secret).now()
                    LOG.info(
                        "Generated TOTP from credential secret",
                        workflow_run_id=workflow_run_id,
                        credential_key=key,
                    )
                    return OTPValue(value=code, type=OTPType.TOTP)
                except Exception:
                    LOG.warning(
                        "Failed to generate TOTP from credential secret",
                        workflow_run_id=workflow_run_id,
                        credential_key=key,
                        exc_info=True,
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
    ctx = OTPPollContext(
        organization_id=organization_id,
        task_id=task_id,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
    )
    timeout = timedelta(minutes=settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_api_key: str | None = None
    if ctx.totp_verification_url:
        org_token = await app.DATABASE.get_valid_org_auth_token(
            ctx.organization_id, OrganizationAuthTokenType.api.value
        )
        if not org_token:
            LOG.error("Failed to get organization token when trying to get otp value")
            return None
        org_api_key = org_token.token
    LOG.info(
        "Polling otp value",
        task_id=ctx.task_id,
        workflow_run_id=ctx.workflow_run_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
        totp_verification_url=ctx.totp_verification_url,
        totp_identifier=ctx.totp_identifier,
    )

    await _set_waiting_state(ctx, start_datetime)

    try:
        while True:
            await asyncio.sleep(10)
            # check timeout
            if datetime.utcnow() > timeout_datetime:
                LOG.warning("Polling otp value timed out")
                raise NoTOTPVerificationCodeFound(
                    task_id=ctx.task_id,
                    workflow_run_id=ctx.workflow_run_id,
                    workflow_id=ctx.workflow_permanent_id,
                    totp_verification_url=ctx.totp_verification_url,
                    totp_identifier=ctx.totp_identifier,
                )
            otp_value: OTPValue | None = None
            if ctx.totp_verification_url:
                assert org_api_key is not None
                otp_value = await _get_otp_value_from_url(
                    ctx.organization_id,
                    ctx.totp_verification_url,
                    org_api_key,
                    task_id=ctx.task_id,
                    workflow_run_id=ctx.workflow_run_id,
                )
            elif ctx.totp_identifier:
                otp_value = await _get_otp_value_from_db(
                    ctx.organization_id,
                    ctx.totp_identifier,
                    task_id=ctx.task_id,
                    workflow_id=ctx.workflow_id,
                    workflow_run_id=ctx.workflow_run_id,
                )
                if not otp_value:
                    otp_value = await _get_otp_value_by_run(
                        ctx.organization_id,
                        task_id=ctx.task_id,
                        workflow_run_id=ctx.workflow_run_id,
                    )
            else:
                # No pre-configured TOTP — poll for manually submitted codes by run context
                otp_value = await _get_otp_value_by_run(
                    ctx.organization_id,
                    task_id=ctx.task_id,
                    workflow_run_id=ctx.workflow_run_id,
                )
            if otp_value:
                LOG.info("Got otp value", otp_value=otp_value)
                return otp_value
    finally:
        await _clear_waiting_state(ctx)


async def _set_waiting_state(ctx: OTPPollContext, started_at: datetime) -> None:
    if not ctx.needs_manual_input:
        return

    identifier_for_ui = ctx.totp_identifier
    if ctx.workflow_run_id:
        try:
            await app.DATABASE.update_workflow_run(
                workflow_run_id=ctx.workflow_run_id,
                waiting_for_verification_code=True,
                verification_code_identifier=identifier_for_ui,
                verification_code_polling_started_at=started_at,
            )
            LOG.info(
                "Set 2FA waiting state for workflow run",
                workflow_run_id=ctx.workflow_run_id,
                verification_code_identifier=identifier_for_ui,
            )
            try:
                NotificationRegistryFactory.get_registry().publish(
                    ctx.organization_id,
                    {
                        "type": "verification_code_required",
                        "workflow_run_id": ctx.workflow_run_id,
                        "task_id": ctx.task_id,
                        "identifier": identifier_for_ui,
                        "polling_started_at": started_at.isoformat(),
                    },
                )
            except Exception:
                LOG.warning("Failed to publish 2FA required notification for workflow run", exc_info=True)
        except Exception:
            LOG.warning("Failed to set 2FA waiting state for workflow run", exc_info=True)
    elif ctx.task_id:
        try:
            await app.DATABASE.update_task_2fa_state(
                task_id=ctx.task_id,
                organization_id=ctx.organization_id,
                waiting_for_verification_code=True,
                verification_code_identifier=identifier_for_ui,
                verification_code_polling_started_at=started_at,
            )
            LOG.info(
                "Set 2FA waiting state for task",
                task_id=ctx.task_id,
                verification_code_identifier=identifier_for_ui,
            )
            try:
                NotificationRegistryFactory.get_registry().publish(
                    ctx.organization_id,
                    {
                        "type": "verification_code_required",
                        "task_id": ctx.task_id,
                        "identifier": identifier_for_ui,
                        "polling_started_at": started_at.isoformat(),
                    },
                )
            except Exception:
                LOG.warning("Failed to publish 2FA required notification for task", exc_info=True)
        except Exception:
            LOG.warning("Failed to set 2FA waiting state for task", exc_info=True)


async def _clear_waiting_state(ctx: OTPPollContext) -> None:
    if not ctx.needs_manual_input:
        return

    if ctx.workflow_run_id:
        try:
            await app.DATABASE.update_workflow_run(
                workflow_run_id=ctx.workflow_run_id,
                waiting_for_verification_code=False,
            )
            LOG.info("Cleared 2FA waiting state for workflow run", workflow_run_id=ctx.workflow_run_id)
            try:
                NotificationRegistryFactory.get_registry().publish(
                    ctx.organization_id,
                    {
                        "type": "verification_code_resolved",
                        "workflow_run_id": ctx.workflow_run_id,
                        "task_id": ctx.task_id,
                    },
                )
            except Exception:
                LOG.warning("Failed to publish 2FA resolved notification for workflow run", exc_info=True)
        except Exception:
            LOG.warning("Failed to clear 2FA waiting state for workflow run", exc_info=True)
    elif ctx.task_id:
        try:
            await app.DATABASE.update_task_2fa_state(
                task_id=ctx.task_id,
                organization_id=ctx.organization_id,
                waiting_for_verification_code=False,
            )
            LOG.info("Cleared 2FA waiting state for task", task_id=ctx.task_id)
            try:
                NotificationRegistryFactory.get_registry().publish(
                    ctx.organization_id,
                    {"type": "verification_code_resolved", "task_id": ctx.task_id},
                )
            except Exception:
                LOG.warning("Failed to publish 2FA resolved notification for task", exc_info=True)
        except Exception:
            LOG.warning("Failed to clear 2FA waiting state for task", exc_info=True)


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
        json_resp = await aiohttp_post(
            url=url,
            str_data=signed_data.signed_payload,
            headers=signed_data.headers,
            raise_exception=False,
            retry=2,
            retry_timeout=5,
        )
    except Exception as e:
        LOG.error("Failed to get otp value from url", exc_info=True)
        raise FailedToGetTOTPVerificationCode(
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_permanent_id,
            totp_verification_url=url,
            reason=str(e),
        )
    if not json_resp:
        return None

    content = json_resp.get("verification_code", None)
    if not content:
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
            content=content,
        )
        return None

    return otp_value


async def _get_otp_value_by_run(
    organization_id: str,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
) -> OTPValue | None:
    """Look up OTP codes by task_id/workflow_run_id when no totp_identifier is configured.

    Used for the manual 2FA input flow where users submit codes through the UI
    without pre-configured TOTP credentials.
    """
    codes = await app.DATABASE.get_otp_codes_by_run(
        organization_id=organization_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        limit=1,
    )
    if codes:
        code = codes[0]
        return OTPValue(value=code.code, type=code.otp_type)
    return None


async def _get_otp_value_from_db(
    organization_id: str,
    totp_identifier: str,
    task_id: str | None = None,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> OTPValue | None:
    totp_codes = await app.DATABASE.get_otp_codes(organization_id=organization_id, totp_identifier=totp_identifier)
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
