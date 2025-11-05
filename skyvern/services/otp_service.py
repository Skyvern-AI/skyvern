import asyncio
from datetime import datetime, timedelta

import structlog
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.totp_codes import OTPType

LOG = structlog.get_logger()


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


async def parse_otp_login(content: str, organization_id: str) -> OTPValue | None:
    prompt = prompt_engine.load_prompt("parse-otp-login", content=content)
    resp = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, prompt_name="parse-otp-login", organization_id=organization_id
    )
    LOG.info("OTP Login Parser Response", resp=resp)
    otp_result = OTPResultParsedByLLM.model_validate(resp)
    if otp_result.otp_value_found and otp_result.otp_value:
        return OTPValue(value=otp_result.otp_value, type=otp_result.otp_type)
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
    org_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api.value)
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
    while True:
        await asyncio.sleep(10)
        # check timeout
        if datetime.utcnow() > timeout_datetime:
            LOG.warning("Polling otp value timed out")
            raise NoTOTPVerificationCodeFound(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_permanent_id,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
            )
        otp_value: OTPValue | None = None
        if totp_verification_url:
            otp_value = await _get_otp_value_from_url(
                organization_id,
                totp_verification_url,
                org_token.token,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
            )
        elif totp_identifier:
            otp_value = await _get_otp_value_from_db(
                organization_id,
                totp_identifier,
                task_id=task_id,
                workflow_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
            )
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
        json_resp = await aiohttp_post(
            url=url, str_data=signed_data.signed_payload, headers=signed_data.headers, raise_exception=False
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
