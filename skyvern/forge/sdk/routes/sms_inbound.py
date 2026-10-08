import hmac
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, Response, status
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import TwilioCredential
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services.twilio_service import build_inbound_sms_url, validate_twilio_signature
from skyvern.services.otp_service import parse_otp_login, redact_otp_identifier_for_log
from skyvern.utils.phone_validation import normalize_phone_identifier

LOG = structlog.get_logger()

sms_inbound_router = APIRouter()

SMS_SIGNING_URL_UNAVAILABLE = "SMS signing URL is unavailable"

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
_TWILIO_SMS_BODY_MAX_LENGTH = 1600


def _empty_twiml_response() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="text/xml")


async def _parse_and_promote(organization_id: str, totp_code_id: str, content: str) -> None:
    try:
        otp_value = await parse_otp_login(content, organization_id)
        if otp_value is None:
            return
        await app.DATABASE.otp.promote_raw_otp_code(
            totp_code_id=totp_code_id,
            organization_id=organization_id,
            code=otp_value.value,
            otp_type=otp_value.get_otp_type(),
        )
    except Exception as exc:
        LOG.warning(
            "Failed to parse and promote inbound SMS OTP",
            organization_id=organization_id,
            totp_code_id=totp_code_id,
            exception_type=type(exc).__name__,
        )


@sms_inbound_router.post("/inbound/{sms_config_id}")
async def receive_inbound_sms(
    sms_config_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    token: Annotated[str | None, Query()] = None,
    twilio_signature: Annotated[str | None, Header(alias="X-Twilio-Signature")] = None,
) -> Response:
    try:
        config_with_secret = await app.DATABASE.sms.get_sms_config_with_secret_for_inbound(sms_config_id=sms_config_id)
    except ValidationError:
        LOG.error("Rejecting invalid persisted SMS configuration", sms_config_id=sms_config_id)
        raise HTTPException(status_code=403, detail="Invalid SMS configuration") from None
    if config_with_secret is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SMS configuration not found")

    sms_config, webhook_secret = config_with_secret
    if not hmac.compare_digest(token or "", webhook_secret):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook token")

    form = await request.form()
    form_params = {key: value if isinstance(value, str) else str(value) for key, value in form.items()}

    auth_token: str | None = None
    if sms_config.mode == "managed":
        try:
            auth_token = await app.DATABASE.sms.get_sms_config_signing_token(sms_config_id)
        except Exception as exc:
            LOG.warning(
                "Rejecting managed inbound SMS without a decryptable signing token",
                organization_id=sms_config.organization_id,
                sms_config_id=sms_config.sms_config_id,
                exception_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managed SMS signing token is unavailable",
            ) from exc
        if not auth_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managed SMS signing token is unavailable",
            )
    elif sms_config.mode == "connected":
        twilio_token = await app.DATABASE.organizations.get_valid_org_auth_token(
            sms_config.organization_id,
            OrganizationAuthTokenType.twilio_credential.value,
        )
        if twilio_token is not None and isinstance(twilio_token.credential, TwilioCredential):
            auth_token = twilio_token.credential.auth_token
        if not auth_token:
            LOG.warning(
                "Rejecting inbound SMS because the connected Twilio auth token is unavailable",
                organization_id=sms_config.organization_id,
                sms_config_id=sms_config.sms_config_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Connected SMS signing token is unavailable",
            )

    elif sms_config.mode == "manual":
        # Manual forwarding trusts only the per-config webhook token checked above.
        pass
    else:
        LOG.error("Rejecting unknown SMS mode", sms_config_id=sms_config_id)
        raise HTTPException(status_code=403, detail="Invalid SMS configuration mode")

    if auth_token:
        base_url = settings.SKYVERN_BASE_URL.strip()
        if not base_url:
            LOG.error(
                "Connected inbound SMS requires SKYVERN_BASE_URL for signature verification",
                organization_id=sms_config.organization_id,
                sms_config_id=sms_config.sms_config_id,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=SMS_SIGNING_URL_UNAVAILABLE)
        canonical_url = build_inbound_sms_url(base_url, sms_config_id, webhook_secret)
        if not validate_twilio_signature(auth_token, canonical_url, form_params, twilio_signature or ""):
            LOG.warning(
                "Rejecting inbound SMS with invalid Twilio signature",
                organization_id=sms_config.organization_id,
                sms_config_id=sms_config.sms_config_id,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Twilio signature")

    to_identifier = normalize_phone_identifier(form_params.get("To", ""))
    redacted_identifier = redact_otp_identifier_for_log(to_identifier)
    phone_number = await app.DATABASE.sms.get_phone_number_by_number(
        to_identifier,
        organization_id=sms_config.organization_id,
    )
    if (
        phone_number is None
        or phone_number.sms_config_id != sms_config.sms_config_id
        or phone_number.status not in {"active", "releasing"}
    ):
        LOG.info(
            "Dropping SMS for unregistered number",
            organization_id=sms_config.organization_id,
            totp_identifier=redacted_identifier,
        )
        return _empty_twiml_response()
    external_message_id = form_params.get("MessageSid")
    if not external_message_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MessageSid is required")

    utc_midnight = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_count = await app.DATABASE.otp.count_otp_codes_since(
        sms_config.organization_id,
        to_identifier,
        created_after=utc_midnight,
        source="twilio",
    )
    # Count-then-insert is not atomic; bounded overshoot under burst is accepted (see PR review).
    if daily_count >= sms_config.daily_ingest_cap:
        LOG.warning(
            "Dropping SMS because the daily ingest cap was reached",
            organization_id=sms_config.organization_id,
            totp_identifier=redacted_identifier,
        )
        return _empty_twiml_response()

    body = form_params.get("Body", "")
    stripped_body = body.strip()
    content = body[:_TWILIO_SMS_BODY_MAX_LENGTH]

    if stripped_body.isdigit() and len(stripped_body) <= 10:
        otp_row = await app.DATABASE.otp.create_otp_code_if_new(
            organization_id=sms_config.organization_id,
            totp_identifier=to_identifier,
            content=content,
            code=stripped_body,
            otp_type=OTPType.TOTP,
            source="twilio",
            external_message_id=external_message_id,
        )
        if otp_row is None:
            return _empty_twiml_response()
    else:
        raw_row = await app.DATABASE.otp.create_raw_otp_code_if_new(
            organization_id=sms_config.organization_id,
            totp_identifier=to_identifier,
            content=content,
            source="twilio",
            external_message_id=external_message_id,
        )
        if raw_row is None:
            return _empty_twiml_response()
        background_tasks.add_task(
            _parse_and_promote,
            sms_config.organization_id,
            raw_row.totp_code_id,
            content,
        )

    return _empty_twiml_response()
