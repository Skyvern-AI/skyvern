import asyncio
import secrets
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from skyvern.forge import app
from skyvern.forge.sdk.db.repositories.email_forwarding import (
    CredentialBindingAccessChanged,
    CredentialTOTPBindingConflict,
    ForwardingAddressAlreadyExists,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.email_forwarding import (
    EmailConfig,
    EmailConfigCreate,
    EmailConfigWithSecret,
    ForwardingAddress,
    ForwardingAddressCreate,
    ForwardingAddressRotate,
    ForwardingAddressRotation,
    ForwardingAddressStatus,
    ForwardingMatchMode,
    InboundEmail,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.org_auth_service import get_current_org_for_email_forwarding
from skyvern.forge.sdk.services.request_principal import get_request_principal
from skyvern.forge.sdk.services.route_authorization import require_request_actions
from skyvern.services.email_forwarding import (
    MAX_INBOUND_EMAIL_BYTES,
    hash_webhook_secret,
    ingest_inbound_email,
    normalize_inbound_payload,
    webhook_secret_matches,
)
from skyvern.utils.organization_slug import get_or_derive_org_slug
from skyvern.utils.phone_validation import normalize_identifier

LOG = structlog.get_logger()
INBOUND_BODY_READ_TIMEOUT_SECONDS = 30
_MUTABLE_ADDRESS_STATUSES = (
    ForwardingAddressStatus.PENDING,
    ForwardingAddressStatus.ACTIVE,
    ForwardingAddressStatus.DISABLED,
)


async def _require_email_forwarding_available() -> None:
    await app.AGENT_FUNCTION.ensure_email_forwarding_available()


email_forwarding_router = APIRouter(dependencies=[Depends(_require_email_forwarding_available)])


async def _read_limited_request_body(request: Request) -> bytes | None:
    body = bytearray()
    async with asyncio.timeout(INBOUND_BODY_READ_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            remaining = MAX_INBOUND_EMAIL_BYTES + 1 - len(body)
            if remaining <= 0:
                return None
            body.extend(chunk[:remaining])
            if len(body) > MAX_INBOUND_EMAIL_BYTES:
                return None
    bounded_body = bytes(body)
    request._body = bounded_body  # type: ignore[attr-defined]
    return bounded_body


@email_forwarding_router.post(
    "/email/inbound/{email_config_id}",
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
    name="receive_inbound_email",
)
async def receive_inbound_email(
    email_config_id: str,
    request: Request,
    # Provider webhook forms require a callback query secret; rotate-secret mitigates its transport-log exposure.
    secret: str | None = Query(default=None),
) -> Response:
    # Keep DB calls outside the parse-only try so transient storage failures return 5xx and providers retry.
    config = await app.DATABASE.email_forwarding.get_config(email_config_id)
    if config is None or not webhook_secret_matches(config, secret):
        return Response(status_code=status.HTTP_200_OK)
    if not await app.AGENT_FUNCTION.ensure_email_forwarding_available(config.organization_id):
        return Response(status_code=status.HTTP_200_OK)

    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > MAX_INBOUND_EMAIL_BYTES:
            LOG.info(
                "Dropped oversized inbound email",
                email_config_id=email_config_id,
                content_length=int(content_length),
            )
            return Response(status_code=status.HTTP_200_OK)
        body = await _read_limited_request_body(request)
        if body is None:
            LOG.info("Dropped oversized inbound email", email_config_id=email_config_id)
            return Response(status_code=status.HTTP_200_OK)

        content_type = request.headers.get("content-type", "")
        fields = None
        if content_type.casefold().startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
            form = await request.form(max_part_size=MAX_INBOUND_EMAIL_BYTES)
            fields = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        normalized = await asyncio.to_thread(normalize_inbound_payload, content_type, body, fields)
    except TimeoutError:
        LOG.info(
            "Dropped inbound email after body read timeout",
            email_config_id=email_config_id,
            timeout_seconds=INBOUND_BODY_READ_TIMEOUT_SECONDS,
        )
        return Response(status_code=status.HTTP_200_OK)
    except Exception:
        LOG.warning("Dropped malformed inbound email", email_config_id=email_config_id, exc_info=True)
        return Response(status_code=status.HTTP_200_OK)
    await ingest_inbound_email(config, normalized)
    return Response(status_code=status.HTTP_200_OK)


@email_forwarding_router.post(
    "/email-forwarding/configs",
    include_in_schema=False,
    response_model=EmailConfigWithSecret,
    status_code=status.HTTP_201_CREATED,
    tags=["Credentials"],
    summary="Create an inbound email config",
    description="Create a webhook receiver and return its secret URL once.",
    responses={201: {"description": "Email config created"}},
    openapi_extra={"x-fern-sdk-method-name": "createEmailForwardingConfig"},
)
@email_forwarding_router.post(
    "/email-forwarding/configs/",
    response_model=EmailConfigWithSecret,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_email_config(
    data: EmailConfigCreate,
    request: Request,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
    _request_authorization: None = Depends(
        require_request_actions(frozenset({"email_forwarding_config.create"}), always_enforce=True)
    ),
) -> EmailConfigWithSecret:
    secret = secrets.token_urlsafe(32)
    config = await app.DATABASE.email_forwarding.create_config(
        organization_id=current_org.organization_id,
        webhook_secret_hash=hash_webhook_secret(secret),
        daily_inbound_cap=data.daily_inbound_cap,
        drop_flagged_spam=data.drop_flagged_spam,
    )
    receiver = request.url_for("receive_inbound_email", email_config_id=config.email_config_id)
    return EmailConfigWithSecret(
        **EmailConfig.model_validate(config).model_dump(),
        receiver_url=f"{receiver}?secret={quote(secret)}",
    )


@email_forwarding_router.post(
    "/email-forwarding/configs/{email_config_id}/rotate-secret",
    include_in_schema=False,
    response_model=EmailConfigWithSecret,
    tags=["Credentials"],
    summary="Rotate an inbound email config secret",
    description="Replace the webhook receiver secret and return its new receiver URL once.",
    responses={200: {"description": "Email config secret rotated"}, 404: {"description": "Email config not found"}},
    openapi_extra={"x-fern-sdk-method-name": "rotateEmailForwardingConfigSecret"},
)
@email_forwarding_router.post(
    "/email-forwarding/configs/{email_config_id}/rotate-secret/",
    response_model=EmailConfigWithSecret,
    include_in_schema=False,
)
async def rotate_email_config_secret(
    email_config_id: str,
    request: Request,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
    _request_authorization: None = Depends(
        require_request_actions(frozenset({"email_forwarding_config.rotate_secret"}), always_enforce=True)
    ),
) -> EmailConfigWithSecret:
    secret = secrets.token_urlsafe(32)
    config = await app.DATABASE.email_forwarding.rotate_config_secret(
        email_config_id,
        current_org.organization_id,
        hash_webhook_secret(secret),
    )
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email config not found")
    receiver = request.url_for("receive_inbound_email", email_config_id=config.email_config_id)
    return EmailConfigWithSecret(
        **EmailConfig.model_validate(config).model_dump(),
        receiver_url=f"{receiver}?secret={quote(secret)}",
    )


@email_forwarding_router.get(
    "/email-forwarding/configs",
    include_in_schema=False,
    response_model=list[EmailConfig],
    tags=["Credentials"],
    summary="List inbound email configs",
    description="List webhook receiver configs for the organization.",
    responses={200: {"description": "Email configs returned"}},
    openapi_extra={"x-fern-sdk-method-name": "listEmailForwardingConfigs"},
)
@email_forwarding_router.get(
    "/email-forwarding/configs/",
    response_model=list[EmailConfig],
    include_in_schema=False,
)
async def list_email_configs(
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
    _request_authorization: None = Depends(
        require_request_actions(frozenset({"email_forwarding_config.list"}), always_enforce=True)
    ),
) -> list[EmailConfig]:
    return await app.DATABASE.email_forwarding.list_configs(current_org.organization_id)


@email_forwarding_router.delete(
    "/email-forwarding/configs/{email_config_id}",
    include_in_schema=False,
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Credentials"],
    summary="Delete an inbound email config",
    description="Disable the config and revoke all forwarding addresses that use it.",
    responses={204: {"description": "Email config deleted"}, 404: {"description": "Email config not found"}},
    openapi_extra={"x-fern-sdk-method-name": "deleteEmailForwardingConfig"},
)
@email_forwarding_router.delete(
    "/email-forwarding/configs/{email_config_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def delete_email_config(
    email_config_id: str,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
    _request_authorization: None = Depends(
        require_request_actions(frozenset({"email_forwarding_config.delete"}), always_enforce=True)
    ),
) -> Response:
    addresses = await app.DATABASE.email_forwarding.list_addresses(current_org.organization_id)
    credential_ids = {
        address.credential_id
        for address in addresses
        if address.email_config_id == email_config_id
        and address.status != ForwardingAddressStatus.REVOKED
        and address.credential_id is not None
    }
    if credential_ids:
        await app.AGENT_FUNCTION.enforce_credential_folder_access_for_ids(
            get_request_principal(),
            "credential.use",
            {("credential_id", credential_id) for credential_id in credential_ids},
        )
    try:
        deleted = await app.DATABASE.email_forwarding.delete_config(
            email_config_id,
            current_org.organization_id,
            authorized_credential_ids=credential_ids,
        )
    except CredentialBindingAccessChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to a bound credential is required",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email config not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@email_forwarding_router.post(
    "/email-forwarding/addresses",
    include_in_schema=False,
    response_model=ForwardingAddress,
    status_code=status.HTTP_201_CREATED,
    tags=["Credentials"],
    summary="Register a forwarding address",
    description="Register a provider-owned forwarding address and optionally bind it to a credential.",
    responses={
        201: {"description": "Forwarding address registered"},
        404: {"description": "Config not found"},
        409: {"description": "Address or credential binding conflicts with an active registration"},
    },
    openapi_extra={"x-fern-sdk-method-name": "createEmailForwardingAddress"},
)
@email_forwarding_router.post(
    "/email-forwarding/addresses/",
    response_model=ForwardingAddress,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_forwarding_address(
    data: ForwardingAddressCreate,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> ForwardingAddress:
    config = await app.DATABASE.email_forwarding.get_config(data.email_config_id, current_org.organization_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email config not found")
    org_slug = await get_or_derive_org_slug(current_org)
    try:
        return await app.DATABASE.email_forwarding.create_address(
            organization_id=current_org.organization_id,
            email_config_id=data.email_config_id,
            email_address=data.email_address,
            org_slug=org_slug,
            match_mode=data.match_mode,
            credential_id=data.credential_id,
            sender_allowlist=data.sender_allowlist,
        )
    except ForwardingAddressAlreadyExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CredentialTOTPBindingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@email_forwarding_router.get(
    "/email-forwarding/addresses",
    include_in_schema=False,
    response_model=list[ForwardingAddress],
    tags=["Credentials"],
    summary="List forwarding addresses",
    description="List forwarding addresses and their latest receipt state.",
    responses={200: {"description": "Forwarding addresses returned"}},
    openapi_extra={"x-fern-sdk-method-name": "listEmailForwardingAddresses"},
)
@email_forwarding_router.get(
    "/email-forwarding/addresses/",
    response_model=list[ForwardingAddress],
    include_in_schema=False,
)
async def list_forwarding_addresses(
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> list[ForwardingAddress]:
    addresses = await app.DATABASE.email_forwarding.list_addresses(current_org.organization_id)
    credential_ids = {address.credential_id for address in addresses if address.credential_id is not None}
    if credential_ids and await app.AGENT_FUNCTION.credential_folders_restricted(current_org.organization_id):
        accessible_credential_ids = await app.AGENT_FUNCTION.accessible_credential_ids(
            get_request_principal(), "credential.read", credential_ids
        )
        for address in addresses:
            if address.credential_id not in accessible_credential_ids:
                address.credential_id = None
    return addresses


async def _get_address_or_404(forwarding_address_id: str, organization_id: str) -> ForwardingAddress:
    address = await app.DATABASE.email_forwarding.get_address(forwarding_address_id, organization_id)
    if address is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forwarding address not found")
    if address.credential_id is not None:
        # The guard cannot see the credential behind an address id, so every route that reaches an
        # address by id checks the binding here before reading its inbox or changing it.
        await app.AGENT_FUNCTION.enforce_credential_folder_access_for_ids(
            get_request_principal(), "credential.use", {("credential_id", address.credential_id)}
        )
    return address


@email_forwarding_router.get(
    "/email-forwarding/addresses/{forwarding_address_id}/emails",
    include_in_schema=False,
    response_model=list[InboundEmail],
    tags=["Credentials"],
    summary="List received emails",
    description="List the text-only inbox for a forwarding address.",
    responses={200: {"description": "Inbound emails returned"}, 404: {"description": "Address not found"}},
    openapi_extra={"x-fern-sdk-method-name": "listEmailForwardingMessages"},
)
@email_forwarding_router.get(
    "/email-forwarding/addresses/{forwarding_address_id}/emails/",
    response_model=list[InboundEmail],
    include_in_schema=False,
)
async def list_forwarded_emails(
    forwarding_address_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> list[InboundEmail]:
    address = await _get_address_or_404(forwarding_address_id, current_org.organization_id)
    emails = await app.DATABASE.email_forwarding.list_inbound_emails(
        forwarding_address_id, current_org.organization_id, limit, offset
    )
    if address.credential_id is not None or not emails:
        return emails
    if not await app.AGENT_FUNCTION.credential_folders_restricted(current_org.organization_id):
        return emails
    principal = get_request_principal()
    if await app.AGENT_FUNCTION.credential_folder_admin_bypass(principal):
        return emails

    def email_identifier(email: InboundEmail) -> str:
        return normalize_identifier(
            email.original_recipient
            if address.match_mode == ForwardingMatchMode.ORIGINAL_RECIPIENT and email.original_recipient
            else address.email_address
        )

    identifiers = {email_identifier(email) for email in emails}
    credential_ids_by_identifier = await app.DATABASE.credentials.get_credential_ids_by_totp_identifiers(
        identifiers, current_org.organization_id
    )
    credential_ids = set().union(*credential_ids_by_identifier.values())
    if not credential_ids:
        return []
    allowed_credential_ids = await app.AGENT_FUNCTION.accessible_credential_ids(
        principal, "credential.use", credential_ids
    )
    return [
        email
        for email in emails
        if (linked_ids := credential_ids_by_identifier.get(email_identifier(email))) is not None
        and linked_ids <= allowed_credential_ids
    ]


@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/rotate",
    include_in_schema=False,
    response_model=ForwardingAddressRotation,
    tags=["Credentials"],
    summary="Rotate a forwarding address",
    description="Revoke an address and register its provider-owned replacement atomically.",
    responses={
        200: {"description": "Address rotated"},
        404: {"description": "Address not found"},
        409: {"description": "Address rotation conflicts with an active registration"},
    },
    openapi_extra={"x-fern-sdk-method-name": "rotateEmailForwardingAddress"},
)
@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/rotate/",
    response_model=ForwardingAddressRotation,
    include_in_schema=False,
)
async def rotate_forwarding_address(
    forwarding_address_id: str,
    data: ForwardingAddressRotate,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> ForwardingAddressRotation:
    current = await _get_address_or_404(forwarding_address_id, current_org.organization_id)
    if current.status == ForwardingAddressStatus.REVOKED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revoked addresses cannot be rotated")
    try:
        rotation = await app.DATABASE.email_forwarding.rotate_address(
            forwarding_address_id, current_org.organization_id, data.email_address
        )
    except ForwardingAddressAlreadyExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if rotation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Forwarding address changed concurrently; retry with its current state",
        )
    revoked, replacement = rotation
    return ForwardingAddressRotation(revoked=revoked, replacement=replacement)


async def _set_address_status(
    forwarding_address_id: str, organization_id: str, target_status: ForwardingAddressStatus
) -> ForwardingAddress:
    current = await _get_address_or_404(forwarding_address_id, organization_id)
    if current.status == ForwardingAddressStatus.REVOKED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revoked addresses cannot be changed")
    updated = await app.DATABASE.email_forwarding.set_address_status(
        forwarding_address_id,
        organization_id,
        target_status,
        _MUTABLE_ADDRESS_STATUSES,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Forwarding address changed concurrently; retry with its current state",
        )
    return updated


@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/disable",
    include_in_schema=False,
    response_model=ForwardingAddress,
    tags=["Credentials"],
    summary="Disable a forwarding address",
    description="Pause ingestion for a forwarding address.",
    responses={200: {"description": "Address disabled"}, 404: {"description": "Address not found"}},
    openapi_extra={"x-fern-sdk-method-name": "disableEmailForwardingAddress"},
)
@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/disable/",
    response_model=ForwardingAddress,
    include_in_schema=False,
)
async def disable_forwarding_address(
    forwarding_address_id: str,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> ForwardingAddress:
    return await _set_address_status(
        forwarding_address_id, current_org.organization_id, ForwardingAddressStatus.DISABLED
    )


@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/enable",
    include_in_schema=False,
    response_model=ForwardingAddress,
    tags=["Credentials"],
    summary="Enable a forwarding address",
    description="Resume ingestion for a forwarding address.",
    responses={200: {"description": "Address enabled"}, 404: {"description": "Address not found"}},
    openapi_extra={"x-fern-sdk-method-name": "enableEmailForwardingAddress"},
)
@email_forwarding_router.post(
    "/email-forwarding/addresses/{forwarding_address_id}/enable/",
    response_model=ForwardingAddress,
    include_in_schema=False,
)
async def enable_forwarding_address(
    forwarding_address_id: str,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> ForwardingAddress:
    return await _set_address_status(forwarding_address_id, current_org.organization_id, ForwardingAddressStatus.ACTIVE)


@email_forwarding_router.delete(
    "/email-forwarding/addresses/{forwarding_address_id}",
    include_in_schema=False,
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Credentials"],
    summary="Revoke a forwarding address",
    description="Permanently stop ingestion for a forwarding address.",
    responses={204: {"description": "Address revoked"}, 404: {"description": "Address not found"}},
    openapi_extra={"x-fern-sdk-method-name": "revokeEmailForwardingAddress"},
)
@email_forwarding_router.delete(
    "/email-forwarding/addresses/{forwarding_address_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def revoke_forwarding_address(
    forwarding_address_id: str,
    current_org: Organization = Depends(get_current_org_for_email_forwarding),
) -> Response:
    current = await _get_address_or_404(forwarding_address_id, current_org.organization_id)
    if current.status == ForwardingAddressStatus.REVOKED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Forwarding address is already revoked")
    updated = await app.DATABASE.email_forwarding.set_address_status(
        forwarding_address_id,
        current_org.organization_id,
        ForwardingAddressStatus.REVOKED,
        _MUTABLE_ADDRESS_STATUSES,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Forwarding address changed concurrently; retry with its current state",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


base_router.include_router(email_forwarding_router)
