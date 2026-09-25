import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.repositories.sms import SMSLifecycleLockTimeout
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.routes.sms_inbound import SMS_SIGNING_URL_UNAVAILABLE
from skyvern.forge.sdk.schemas.organizations import (
    CreateTwilioCredentialRequest,
    Organization,
    TwilioCredential,
    TwilioCredentialSafe,
    _TwilioJSONModel,
    _validate_twilio_without_error_inputs,
)
from skyvern.forge.sdk.schemas.sms import (
    CreateSMSConfigRequest,
    DisablePhoneNumberRequest,
    EnablePhoneNumberRequest,
    OrganizationPhoneNumber,
    RegisterManualPhoneNumberRequest,
    SMSConfig,
    SMSConfigCreated,
    TwilioPhoneNumberInfo,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.twilio_service import (
    TwilioApiError,
    build_client_from_credential,
    build_inbound_sms_url,
)
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.secret_encryption import decrypt_secret_field_value, is_encrypted_secret
from skyvern.utils.phone_validation import is_e164_phone_number, normalize_phone_identifier
from skyvern.utils.url_validators import redact_url_for_display, redact_url_query

twilio_integration_router = APIRouter()
LOG = structlog.get_logger()


class OrganizationPhoneNumberResponse(BaseModel):
    """Safe phone-number metadata without provider routing or cost secrets."""

    model_config = ConfigDict(from_attributes=True)

    phone_number_id: str
    organization_id: str
    sms_config_id: str
    phone_number: str
    provider: str
    provider_number_sid: str | None = None
    credential_id: str | None = None
    price_cents: int | None = None
    status: str
    quarantined_until: Any | None = None
    created_at: Any
    modified_at: Any
    deleted_at: Any | None = None


class TwilioCredentialResponse(_TwilioJSONModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    credential: TwilioCredentialSafe | None = None

    _hide_validation_inputs = model_validator(mode="wrap")(_validate_twilio_without_error_inputs)


def _require_twilio_sms_enabled() -> None:
    if not SettingsManager.get_settings().TWILIO_SMS_2FA_ENABLED:
        raise HTTPException(status_code=404, detail="Twilio SMS 2FA is not available")


def _twilio_api_http_exception(error: TwilioApiError) -> HTTPException:
    if error.status_code in {401, 403}:
        return HTTPException(status_code=400, detail="Invalid Twilio credentials")
    if error.status_code == 404:
        return HTTPException(status_code=404, detail="Phone number not found on Twilio account")
    if error.status_code == 429:
        return HTTPException(status_code=503, detail="Twilio rate limit, retry later")
    return HTTPException(status_code=502, detail="Twilio API error")


def _safe_credential(credential: TwilioCredential) -> TwilioCredentialResponse:
    return TwilioCredentialResponse(
        credential=TwilioCredentialSafe(
            account_sid=credential.account_sid,
            api_key_sid=credential.api_key_sid,
            has_auth_token=bool(credential.auth_token),
        )
    )


async def _get_credential(organization_id: str) -> TwilioCredential:
    token = await app.DATABASE.organizations.get_valid_org_auth_token(
        organization_id,
        OrganizationAuthTokenType.twilio_credential.value,
    )
    if token is None:
        raise HTTPException(status_code=404, detail="Twilio credentials not found")
    return token.credential


async def _validate_twilio_credential(credential: TwilioCredential) -> None:
    clients = [build_client_from_credential(credential)]
    if credential.auth_token and credential.api_key_sid:
        clients.append(
            build_client_from_credential(
                TwilioCredential(
                    account_sid=credential.account_sid,
                    auth_token=credential.auth_token,
                )
            )
        )
    try:
        for client in clients:
            await client.get_account()
    except TwilioApiError as error:
        raise _twilio_api_http_exception(error) from None


def _sms_capable(number: dict[str, Any]) -> bool:
    if "sms_capable" in number:
        return bool(number["sms_capable"])
    capabilities = number.get("capabilities") or {}
    return bool(capabilities.get("SMS", capabilities.get("sms", False)))


def _current_sms_url(number: dict[str, Any]) -> str | None:
    value = number.get("sms_url")
    return str(value) if value else None


def _current_sms_method(number: dict[str, Any]) -> str | None:
    value = number.get("sms_method")
    return str(value) if value else None


def _current_sms_application_sid(number: dict[str, Any]) -> str | None:
    value = number.get("sms_application_sid")
    return str(value) if value else None


def _routing_is_ours(
    *,
    current_sms_url: str | None,
    current_sms_method: str | None,
    current_sms_application_sid: str | None,
    inbound_url: str,
) -> bool:
    return (
        current_sms_url == inbound_url
        and (current_sms_method is None or current_sms_method.upper() == "POST")
        and not current_sms_application_sid
    )


async def _get_registered_phone_number(phone_number: str, organization_id: str) -> OrganizationPhoneNumber | None:
    return await app.DATABASE.sms.get_phone_number_by_number(phone_number, organization_id)


def _require_twilio_provider(phone_number: OrganizationPhoneNumber | None) -> None:
    if phone_number is not None and phone_number.provider != "twilio":
        raise HTTPException(
            status_code=409,
            detail="Skyvern-managed numbers must be released from the Skyvern Numbers integration",
        )


def _require_sms_base_url() -> None:
    if not settings.SKYVERN_BASE_URL.strip():
        raise HTTPException(status_code=503, detail=SMS_SIGNING_URL_UNAVAILABLE)


def _inbound_url(sms_config_id: str, webhook_secret: str) -> str:
    return build_inbound_sms_url(settings.SKYVERN_BASE_URL, sms_config_id, webhook_secret)


async def _load_sms_capable_provider_number(
    client: Any,
    phone_number_sid: str,
) -> tuple[dict[str, Any], str]:
    try:
        provider_number = await client.get_incoming_phone_number(phone_number_sid)
    except TwilioApiError as error:
        raise _twilio_api_http_exception(error) from None
    if not _sms_capable(provider_number):
        raise HTTPException(status_code=400, detail="Phone number is not SMS-capable")
    phone_number = normalize_phone_identifier(str(provider_number.get("phone_number") or ""))
    if not is_e164_phone_number(phone_number):
        raise HTTPException(status_code=502, detail="Twilio returned an invalid phone number")
    return provider_number, phone_number


async def _active_connected_phone_count(organization_id: str) -> int:
    connected_config = await app.DATABASE.sms.get_connected_sms_config(organization_id)
    if connected_config is None:
        return 0
    config, _ = connected_config
    return await app.DATABASE.sms.count_active_twilio_phone_numbers_for_config(
        config.sms_config_id,
        organization_id,
    )


async def _shielded(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_lifecycle(awaitable: Any) -> Any:
    """Finish a lifecycle child task before propagating cancellation to its caller."""
    child = asyncio.create_task(awaitable)
    cancellation_requested = False
    child_error: BaseException | None = None
    result: Any = None
    try:
        result = await asyncio.shield(child)
    except asyncio.CancelledError:
        cancellation_requested = True
        while not child.done():
            try:
                result = await asyncio.shield(child)
            except asyncio.CancelledError:
                cancellation_requested = True
            except BaseException as error:
                child_error = error
                break
        if child_error is None and child.done() and not child.cancelled():
            child_error = child.exception()
    if cancellation_requested:
        if child_error is not None and not isinstance(child_error, asyncio.CancelledError):
            LOG.warning("Twilio SMS lifecycle failed while caller cancellation was pending", error=repr(child_error))
        raise asyncio.CancelledError
    if child_error is not None:
        raise child_error
    return result


async def _compensate_phone_number_state(
    *,
    organization_id: str,
    phone_number_id: str,
    registered: OrganizationPhoneNumber | None,
) -> None:
    if registered is None:
        fields: dict[str, Any] = {"status": "disabled", "quarantined_until": None}
    else:
        fields = {
            "status": registered.status,
            "sms_config_id": registered.sms_config_id,
            "provider_number_sid": registered.provider_number_sid,
            "previous_sms_url": registered.previous_sms_url,
            "previous_sms_method": registered.previous_sms_method,
            "previous_sms_application_sid": registered.previous_sms_application_sid,
            "credential_id": registered.credential_id,
            "quarantined_until": None,
        }
    try:
        result = await app.DATABASE.sms.update_phone_number(phone_number_id, organization_id, **fields)
        if result is None:
            LOG.warning("Twilio SMS lifecycle compensation found no local row", phone_number_id=phone_number_id)
    except BaseException:
        LOG.exception("Twilio SMS lifecycle compensation failed", phone_number_id=phone_number_id)


_DISABLE_STAGING_FENCE_FIELDS = set(
    (
        "organization_id phone_number_id phone_number sms_config_id provider provider_number_sid "
        "previous_sms_url previous_sms_method previous_sms_application_sid credential_id"
    ).split()
)


async def _compensate_disable_staging(organization_id: str, registered: OrganizationPhoneNumber) -> None:
    try:
        current = await app.DATABASE.sms.get_phone_number(registered.phone_number_id, organization_id)
    except BaseException:
        LOG.exception("Twilio SMS disable compensation could not read local state")
        return
    if (
        current is None
        or current.status != "quarantined"
        or current.model_dump(include=_DISABLE_STAGING_FENCE_FIELDS)
        != registered.model_dump(include=_DISABLE_STAGING_FENCE_FIELDS)
    ):
        return
    await _compensate_phone_number_state(
        organization_id=organization_id, phone_number_id=registered.phone_number_id, registered=registered
    )


_Routing = tuple[str | None, str | None, str | None]


def _routing_matches(number: dict[str, Any], expected: _Routing) -> bool:
    current = (
        _current_sms_url(number) or "",
        (_current_sms_method(number) or "POST").upper(),
        _current_sms_application_sid(number) or "",
    )
    return current == (expected[0] or "", (expected[1] or "POST").upper(), expected[2] or "")


async def _provider_mutation_outcome(
    client: Any,
    phone_number_sid: str,
    *,
    desired: _Routing,
    prior: _Routing,
) -> str:
    try:
        provider_number = await _shielded(client.get_incoming_phone_number(phone_number_sid))
    except TwilioApiError as error:
        if error.status_code == 404:
            return "absent"
        LOG.warning("Twilio SMS provider outcome could not be read back", status_code=error.status_code)
        return "unknown"
    except BaseException:
        LOG.exception("Twilio SMS provider outcome could not be read back")
        return "unknown"
    if _routing_matches(provider_number, desired):
        return "desired"
    if _routing_matches(provider_number, prior):
        return "prior"
    return "unknown"


async def _reconcile_provider_failure(
    client: Any,
    phone_number_sid: str,
    *,
    desired: _Routing,
    prior: _Routing,
    phone_number_id: str,
    organization_id: str,
    registered: OrganizationPhoneNumber | None,
    status: str,
    absent_is_desired: bool,
) -> None:
    outcome = await _provider_mutation_outcome(client, phone_number_sid, desired=desired, prior=prior)
    if outcome == "desired" or (outcome == "absent" and absent_is_desired):
        await _finalize_phone_number(phone_number_id, organization_id, status=status)
    elif outcome in {"prior", "absent"}:
        await _compensate_phone_number_state(
            organization_id=organization_id,
            phone_number_id=phone_number_id,
            registered=registered,
        )


async def _finalize_phone_number(
    phone_number_id: str,
    organization_id: str,
    *,
    status: str,
) -> OrganizationPhoneNumber:
    updated = await app.DATABASE.sms.update_phone_number(
        phone_number_id,
        organization_id,
        status=status,
        quarantined_until=None,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Phone number not found")
    return updated


@asynccontextmanager
async def _sms_lifecycle_lock(organization_id: str, phone_number: str | None = None) -> AsyncIterator[None]:
    try:
        if phone_number is None:
            async with app.DATABASE.sms.organization_lock(organization_id):
                yield
        else:
            async with app.DATABASE.sms.lifecycle_lock(organization_id, normalize_phone_identifier(phone_number)):
                yield
    except SMSLifecycleLockTimeout as exc:
        raise HTTPException(status_code=409, detail="SMS number operation is busy. Try again.") from exc


@asynccontextmanager
async def _organization_sms_lifecycle_lock(organization_id: str) -> AsyncIterator[None]:
    async with _sms_lifecycle_lock(organization_id):
        yield


@twilio_integration_router.post(
    "/phone-numbers/enable",
    response_model=OrganizationPhoneNumberResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "enable_phone_number",
    },
)
async def enable_twilio_phone_number(
    request: EnablePhoneNumberRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> OrganizationPhoneNumber:
    _require_twilio_sms_enabled()
    _require_sms_base_url()
    return await _run_lifecycle(_enable_twilio_phone_number_locked(request, current_org.organization_id))


async def _enable_twilio_phone_number_locked(
    request: EnablePhoneNumberRequest,
    organization_id: str,
) -> OrganizationPhoneNumber:
    if request.credential_id is not None:
        bound_credential = await app.DATABASE.credentials.get_credential(
            request.credential_id, organization_id=organization_id
        )
        if bound_credential is None:
            raise HTTPException(status_code=400, detail="Credential not found")

    credential = await _get_credential(organization_id)
    if not credential.auth_token:
        raise HTTPException(status_code=400, detail="Twilio auth token is required for signed inbound delivery")
    client = build_client_from_credential(credential)
    _, phone_number = await _load_sms_capable_provider_number(client, request.phone_number_sid)
    async with _sms_lifecycle_lock(organization_id, phone_number):
        credential = await _get_credential(organization_id)
        if not credential.auth_token:
            raise HTTPException(status_code=400, detail="Twilio auth token is required for signed inbound delivery")
        client = build_client_from_credential(credential)
        provider_number, refreshed_phone_number = await _load_sms_capable_provider_number(
            client, request.phone_number_sid
        )
        if refreshed_phone_number != phone_number:
            raise HTTPException(status_code=409, detail="Phone number changed during lifecycle operation")
        registered = await _get_registered_phone_number(phone_number, organization_id)
        _require_twilio_provider(registered)
        connected_config = await app.DATABASE.sms.get_connected_sms_config(organization_id)
        if connected_config is None:
            connected_config = await app.DATABASE.sms.create_sms_config(
                organization_id=organization_id, mode="connected", webhook_secret=secrets.token_urlsafe(32)
            )
        config, webhook_secret = connected_config
        inbound_url = _inbound_url(config.sms_config_id, webhook_secret)
        current_sms_url = _current_sms_url(provider_number)
        current_sms_method = _current_sms_method(provider_number)
        current_sms_application_sid = _current_sms_application_sid(provider_number)
        routing_is_ours = _routing_is_ours(
            current_sms_url=current_sms_url,
            current_sms_method=current_sms_method,
            current_sms_application_sid=current_sms_application_sid,
            inbound_url=inbound_url,
        )
        takeover_required = (
            bool(current_sms_url and current_sms_url != inbound_url)
            or bool(current_sms_method and current_sms_method.upper() != "POST")
            or bool(current_sms_application_sid)
        )
        if takeover_required and not request.confirm_takeover:
            raise HTTPException(
                status_code=409,
                detail={
                    "current_sms_url": redact_url_query(redact_url_for_display(current_sms_url) or "") or None,
                    "current_sms_method": current_sms_method,
                    "current_sms_application_sid": current_sms_application_sid,
                },
            )

        saved: OrganizationPhoneNumber | None = None
        if registered is None:
            try:
                saved = await app.DATABASE.sms.create_phone_number(
                    organization_id=organization_id,
                    sms_config_id=config.sms_config_id,
                    phone_number=phone_number,
                    provider="twilio",
                    provider_number_sid=request.phone_number_sid,
                    previous_sms_url=current_sms_url if not routing_is_ours else None,
                    previous_sms_method=current_sms_method if not routing_is_ours else None,
                    previous_sms_application_sid=current_sms_application_sid if not routing_is_ours else None,
                    credential_id=request.credential_id,
                    status="quarantined",
                )
            except BaseException:
                if saved is not None:
                    await _compensate_phone_number_state(
                        organization_id=organization_id,
                        phone_number_id=saved.phone_number_id,
                        registered=None,
                    )
                raise
        else:
            update_fields: dict[str, Any] = {
                "status": "quarantined",
                "provider": "twilio",
                "provider_number_sid": request.phone_number_sid,
                "sms_config_id": config.sms_config_id,
                "credential_id": request.credential_id,
                "quarantined_until": None,
            }
            if (
                registered.status == "active"
                and routing_is_ours
                and registered.sms_config_id == config.sms_config_id
                and registered.provider_number_sid == request.phone_number_sid
                and registered.credential_id == request.credential_id
            ):
                update_fields["status"] = "active"
            if not routing_is_ours:
                update_fields.update(
                    previous_sms_url=current_sms_url,
                    previous_sms_method=current_sms_method,
                    previous_sms_application_sid=current_sms_application_sid,
                )
            try:
                saved = await app.DATABASE.sms.update_phone_number(
                    registered.phone_number_id, organization_id, **update_fields
                )
                if saved is None:
                    raise HTTPException(status_code=404, detail="Phone number not found")
            except BaseException:
                if registered.status != "quarantined":
                    await _compensate_phone_number_state(
                        organization_id=organization_id,
                        phone_number_id=registered.phone_number_id,
                        registered=registered,
                    )
                raise

        try:
            await _shielded(client.update_inbound_sms_config(request.phone_number_sid, inbound_url, "POST", ""))
        except BaseException as error:
            await _reconcile_provider_failure(
                client,
                request.phone_number_sid,
                desired=(inbound_url, "POST", ""),
                prior=(current_sms_url, current_sms_method, current_sms_application_sid),
                phone_number_id=saved.phone_number_id,
                organization_id=organization_id,
                registered=registered,
                status="active",
                absent_is_desired=False,
            )
            if isinstance(error, TwilioApiError):
                raise _twilio_api_http_exception(error) from None
            raise
        return await _finalize_phone_number(saved.phone_number_id, organization_id, status="active")


@twilio_integration_router.post(
    "/credentials",
    response_model=TwilioCredentialResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "create_credentials",
    },
)
async def create_twilio_credential(
    request: CreateTwilioCredentialRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> TwilioCredentialResponse:
    _require_twilio_sms_enabled()
    organization_id = current_org.organization_id
    async with _organization_sms_lifecycle_lock(organization_id):
        active_number_count = await _active_connected_phone_count(organization_id)
        if active_number_count:
            existing_credential = await _get_credential(organization_id)
            if request.credential.account_sid != existing_credential.account_sid:
                raise HTTPException(
                    status_code=409,
                    detail="Disable all connected Twilio phone numbers before changing Twilio accounts",
                )
            if not request.credential.auth_token:
                raise HTTPException(
                    status_code=400,
                    detail="Twilio auth token is required while connected phone numbers are active or quarantined",
                )
        await _validate_twilio_credential(request.credential)

        await app.DATABASE.organizations.replace_org_auth_token(
            organization_id=organization_id,
            token_type=OrganizationAuthTokenType.twilio_credential,
            token=request.credential,
            encrypted_method=EncryptMethod.AES,
        )
        return _safe_credential(request.credential)


@twilio_integration_router.get(
    "/credentials",
    response_model=TwilioCredentialResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "get_credentials",
    },
)
async def get_twilio_credential(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> TwilioCredentialResponse:
    _require_twilio_sms_enabled()
    token = await app.DATABASE.organizations.get_valid_org_auth_token(
        current_org.organization_id,
        OrganizationAuthTokenType.twilio_credential.value,
    )
    if token is None:
        return TwilioCredentialResponse(credential=None)
    return _safe_credential(token.credential)


@twilio_integration_router.delete(
    "/credentials",
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "delete_credentials",
    },
)
async def delete_twilio_credential(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> dict[str, bool]:
    _require_twilio_sms_enabled()
    organization_id = current_org.organization_id
    async with _organization_sms_lifecycle_lock(organization_id):
        if await _active_connected_phone_count(organization_id):
            raise HTTPException(
                status_code=409,
                detail="Disable all connected Twilio phone numbers before disconnecting Twilio",
            )
        await app.DATABASE.organizations.invalidate_org_auth_tokens(
            organization_id=organization_id,
            token_type=OrganizationAuthTokenType.twilio_credential,
        )
        return {"success": True}


@twilio_integration_router.get(
    "/phone-number-registry",
    response_model=list[OrganizationPhoneNumberResponse],
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "list_registered_phone_numbers",
    },
)
async def list_registered_phone_numbers(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> list[OrganizationPhoneNumber]:
    _require_twilio_sms_enabled()
    return await app.DATABASE.sms.list_phone_numbers(current_org.organization_id)


@twilio_integration_router.get(
    "/phone-numbers",
    response_model=list[TwilioPhoneNumberInfo],
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "list_phone_numbers",
    },
)
async def list_twilio_phone_numbers(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> list[TwilioPhoneNumberInfo]:
    _require_twilio_sms_enabled()
    credential = await _get_credential(current_org.organization_id)
    client = build_client_from_credential(credential)
    try:
        provider_numbers = await client.list_incoming_phone_numbers()
    except TwilioApiError as error:
        raise _twilio_api_http_exception(error) from None
    registry_numbers = await app.DATABASE.sms.list_phone_numbers(current_org.organization_id)
    registry_by_number = {normalize_phone_identifier(number.phone_number): number for number in registry_numbers}
    connected_config = await app.DATABASE.sms.get_connected_sms_config(current_org.organization_id)
    connected_config_id: str | None = None
    connected_inbound_url: str | None = None
    if connected_config is not None:
        config, webhook_secret = connected_config
        connected_config_id = config.sms_config_id
        connected_inbound_url = _inbound_url(config.sms_config_id, webhook_secret)
    result: list[TwilioPhoneNumberInfo] = []
    for provider_number in provider_numbers:
        phone_number = normalize_phone_identifier(str(provider_number.get("phone_number") or ""))
        registered = registry_by_number.get(phone_number)
        current_sms_url = _current_sms_url(provider_number)
        routing_is_ours = connected_inbound_url is not None and _routing_is_ours(
            current_sms_url=current_sms_url,
            current_sms_method=_current_sms_method(provider_number),
            current_sms_application_sid=_current_sms_application_sid(provider_number),
            inbound_url=connected_inbound_url,
        )
        result.append(
            TwilioPhoneNumberInfo(
                sid=str(provider_number.get("sid") or ""),
                phone_number=phone_number,
                friendly_name=str(provider_number.get("friendly_name") or phone_number),
                sms_capable=_sms_capable(provider_number),
                current_sms_url=redact_url_query(redact_url_for_display(current_sms_url) or "") or None,
                enabled=(
                    registered is not None
                    and registered.status == "active"
                    and registered.sms_config_id == connected_config_id
                    and routing_is_ours
                ),
                phone_number_id=registered.phone_number_id if registered is not None else None,
            )
        )
    return result


@twilio_integration_router.post(
    "/phone-numbers/disable",
    response_model=OrganizationPhoneNumberResponse,
    openapi_extra={
        "x-fern-sdk-group-name": "twilio",
        "x-fern-sdk-method-name": "disable_phone_number",
    },
)
async def disable_twilio_phone_number(
    request: DisablePhoneNumberRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> OrganizationPhoneNumber:
    _require_twilio_sms_enabled()
    return await _run_lifecycle(_disable_twilio_phone_number_locked(request, current_org.organization_id))


async def _disable_twilio_phone_number_locked(
    request: DisablePhoneNumberRequest,
    organization_id: str,
) -> OrganizationPhoneNumber:
    initial = await app.DATABASE.sms.get_phone_number(request.phone_number_id, organization_id)
    if initial is None:
        raise HTTPException(status_code=404, detail="Phone number not found")
    _require_twilio_provider(initial)
    async with _sms_lifecycle_lock(organization_id, initial.phone_number):
        registered = await app.DATABASE.sms.get_phone_number(request.phone_number_id, organization_id)
        if registered is None:
            raise HTTPException(status_code=404, detail="Phone number not found")
        _require_twilio_provider(registered)
        was_quarantined = registered.status == "quarantined"
        client: Any | None = None
        if registered.provider_number_sid:
            credential = await _get_credential(organization_id)
            client = build_client_from_credential(credential)
        if not was_quarantined:
            staged_row = await app.DATABASE.sms.update_phone_number(
                registered.phone_number_id,
                organization_id,
                status="quarantined",
                quarantined_until=None,
            )
            if staged_row is None:
                raise HTTPException(status_code=404, detail="Phone number not found")

        if registered.provider_number_sid and client is not None:
            try:
                provider_number = await client.get_incoming_phone_number(registered.provider_number_sid)
            except TwilioApiError as error:
                if error.status_code != 404:
                    if not was_quarantined:
                        await _compensate_disable_staging(organization_id, registered)
                    raise _twilio_api_http_exception(error) from None
            else:
                config_with_secret = await app.DATABASE.sms.get_sms_config_with_secret(
                    sms_config_id=registered.sms_config_id, organization_id=organization_id
                )
                if config_with_secret is not None:
                    config, webhook_secret = config_with_secret
                    inbound_url = _inbound_url(config.sms_config_id, webhook_secret)
                    if _routing_is_ours(
                        current_sms_url=_current_sms_url(provider_number),
                        current_sms_method=_current_sms_method(provider_number),
                        current_sms_application_sid=_current_sms_application_sid(provider_number),
                        inbound_url=inbound_url,
                    ):
                        prior_url = _current_sms_url(provider_number)
                        prior_method = _current_sms_method(provider_number)
                        prior_application_sid = _current_sms_application_sid(provider_number)
                        desired_url = registered.previous_sms_url or ""
                        try:
                            if is_encrypted_secret(desired_url):
                                desired_url = await decrypt_secret_field_value(
                                    desired_url, organization_id=organization_id, field_name="previous_sms_url"
                                )
                        except BaseException:
                            if not was_quarantined:
                                await _compensate_disable_staging(organization_id, registered)
                            raise
                        desired_method = registered.previous_sms_method or "POST"
                        desired_application_sid = registered.previous_sms_application_sid or ""
                        try:
                            await _shielded(
                                client.update_inbound_sms_config(
                                    registered.provider_number_sid,
                                    desired_url,
                                    desired_method,
                                    desired_application_sid,
                                )
                            )
                        except BaseException as error:
                            await _reconcile_provider_failure(
                                client,
                                registered.provider_number_sid,
                                desired=(desired_url, desired_method, desired_application_sid),
                                prior=(prior_url, prior_method, prior_application_sid),
                                phone_number_id=registered.phone_number_id,
                                organization_id=organization_id,
                                registered=registered,
                                status="disabled",
                                absent_is_desired=True,
                            )
                            if isinstance(error, TwilioApiError):
                                raise _twilio_api_http_exception(error) from None
                            raise
        return await _finalize_phone_number(registered.phone_number_id, organization_id, status="disabled")


@twilio_integration_router.post("/sms-configs", response_model=SMSConfigCreated)
async def create_sms_config(
    request: CreateSMSConfigRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> SMSConfigCreated:
    _require_twilio_sms_enabled()
    _require_sms_base_url()
    organization_id = current_org.organization_id
    async with _organization_sms_lifecycle_lock(organization_id):
        config, webhook_secret = await app.DATABASE.sms.create_sms_config(
            organization_id=organization_id,
            mode=request.mode,
            webhook_secret=secrets.token_urlsafe(32),
        )
        return SMSConfigCreated(
            **config.model_dump(),
            inbound_url=_inbound_url(config.sms_config_id, webhook_secret),
        )


@twilio_integration_router.get("/sms-configs", response_model=list[SMSConfig])
async def list_sms_configs(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> list[SMSConfig]:
    _require_twilio_sms_enabled()
    return await app.DATABASE.sms.list_sms_configs(current_org.organization_id)


@twilio_integration_router.delete("/sms-configs/{sms_config_id}")
async def delete_sms_config(
    sms_config_id: str,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> dict[str, bool]:
    _require_twilio_sms_enabled()
    organization_id = current_org.organization_id
    async with _organization_sms_lifecycle_lock(organization_id):
        config = await app.DATABASE.sms.get_sms_config(
            sms_config_id,
            organization_id,
            include_deleted=True,
        )
        if config is None:
            raise HTTPException(status_code=404, detail="SMS config not found")
        if config.mode == "managed":
            raise HTTPException(
                status_code=409,
                detail="Skyvern-managed SMS configurations must be released from the Skyvern Numbers integration",
            )
        active_phone_count = await app.DATABASE.sms.count_active_phone_numbers_for_config(
            sms_config_id,
            organization_id,
        )
        if active_phone_count > 0:
            raise HTTPException(status_code=409, detail="SMS configuration still has active phone numbers")
        await app.DATABASE.sms.delete_sms_config(sms_config_id, organization_id)
        return {"success": True}


@twilio_integration_router.post(
    "/sms-configs/{sms_config_id}/phone-numbers",
    response_model=OrganizationPhoneNumberResponse,
)
async def register_manual_phone_number(
    sms_config_id: str,
    request: RegisterManualPhoneNumberRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> OrganizationPhoneNumber:
    _require_twilio_sms_enabled()
    organization_id = current_org.organization_id
    config = await app.DATABASE.sms.get_sms_config(sms_config_id, organization_id)
    if config is None:
        raise HTTPException(status_code=404, detail="SMS config not found")
    if config.mode != "manual":
        raise HTTPException(status_code=400, detail="SMS config is not manual")
    phone_number = normalize_phone_identifier(request.phone_number)
    if not is_e164_phone_number(phone_number):
        raise HTTPException(status_code=400, detail="Phone number must use E.164 format")
    async with _sms_lifecycle_lock(organization_id, phone_number):
        config = await app.DATABASE.sms.get_sms_config(sms_config_id, organization_id)
        if config is None:
            raise HTTPException(status_code=404, detail="SMS config not found")
        if config.mode != "manual":
            raise HTTPException(status_code=400, detail="SMS config is not manual")
        registered = await _get_registered_phone_number(phone_number, organization_id)
        _require_twilio_provider(registered)
        if registered is None:
            return await app.DATABASE.sms.create_phone_number(
                organization_id=organization_id,
                sms_config_id=sms_config_id,
                phone_number=phone_number,
                provider="twilio",
            )
        if (
            registered.status != "disabled"
            or registered.provider_number_sid is not None
            or registered.provider != "twilio"
        ):
            raise HTTPException(status_code=409, detail="Phone number is already registered")
        updated = await app.DATABASE.sms.update_phone_number(
            registered.phone_number_id,
            organization_id,
            sms_config_id=sms_config_id,
            status="active",
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Phone number not found")
        return updated
