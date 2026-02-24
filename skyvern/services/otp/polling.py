import asyncio
from datetime import datetime, timedelta

import structlog

from skyvern.config import settings
from skyvern.exceptions import FailedToGetTOTPVerificationCode, NoTOTPVerificationCodeFound
from skyvern.forge import app
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.notification.factory import NotificationRegistryFactory
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp.models import OTPPollContext, OTPValue
from skyvern.services.otp.parsing import parse_otp_login

LOG = structlog.get_logger()


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
