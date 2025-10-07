import asyncio
import json
from datetime import datetime, timedelta

import structlog

from skyvern.config import settings
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge import app
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType

LOG = structlog.get_logger()


async def poll_verification_code(
    organization_id: str,
    task_id: str | None = None,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
    totp_verification_url: str | None = None,
    totp_identifier: str | None = None,
) -> str | None:
    timeout = timedelta(minutes=settings.VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api.value)
    if not org_token:
        LOG.error("Failed to get organization token when trying to get verification code")
        return None
    LOG.info(
        "Polling verification code",
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
            LOG.warning("Polling verification code timed out")
            raise NoTOTPVerificationCodeFound(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_permanent_id,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
            )
        verification_code = None
        if totp_verification_url:
            verification_code = await _get_verification_code_from_url(
                totp_verification_url,
                org_token.token,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
            )
        elif totp_identifier:
            verification_code = await _get_verification_code_from_db(
                organization_id,
                totp_identifier,
                task_id=task_id,
                workflow_id=workflow_permanent_id,
                workflow_run_id=workflow_run_id,
            )
        if verification_code:
            LOG.info("Got verification code", verification_code=verification_code)
            return verification_code


async def _get_verification_code_from_url(
    url: str,
    api_key: str,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    workflow_permanent_id: str | None = None,
) -> str | None:
    request_data = {}
    if task_id:
        request_data["task_id"] = task_id
    if workflow_run_id:
        request_data["workflow_run_id"] = workflow_run_id
    if workflow_permanent_id:
        request_data["workflow_permanent_id"] = workflow_permanent_id
    payload = json.dumps(request_data)
    signature = generate_skyvern_signature(
        payload=payload,
        api_key=api_key,
    )
    timestamp = str(int(datetime.utcnow().timestamp()))
    headers = {
        "x-skyvern-timestamp": timestamp,
        "x-skyvern-signature": signature,
        "Content-Type": "application/json",
    }
    try:
        json_resp = await aiohttp_post(url=url, data=request_data, headers=headers, raise_exception=False)
    except Exception as e:
        LOG.error("Failed to get verification code from url", exc_info=True)
        raise FailedToGetTOTPVerificationCode(
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_permanent_id,
            totp_verification_url=url,
            reason=str(e),
        )
    return json_resp.get("verification_code", None)


async def _get_verification_code_from_db(
    organization_id: str,
    totp_identifier: str,
    task_id: str | None = None,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> str | None:
    totp_codes = await app.DATABASE.get_totp_codes(organization_id=organization_id, totp_identifier=totp_identifier)
    for totp_code in totp_codes:
        if totp_code.workflow_run_id and workflow_run_id and totp_code.workflow_run_id != workflow_run_id:
            continue
        if totp_code.workflow_id and workflow_id and totp_code.workflow_id != workflow_id:
            continue
        if totp_code.task_id and totp_code.task_id != task_id:
            continue
        if totp_code.expired_at and totp_code.expired_at < datetime.utcnow():
            continue
        return totp_code.code
    return None
