from __future__ import annotations

from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import HttpException as SkyvernHttpException
from skyvern.forge import app
from skyvern.forge.agent_functions import record_request_audit_event
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialResponse,
    CredentialType,
    CredentialVaultType,
    CreditCardCredentialResponse,
    NonEmptyPasswordCredential,
    PasswordCredentialResponse,
    SecretCredentialResponse,
    TotpType,
)
from skyvern.forge.sdk.services.bitwarden import BitwardenService
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import CustomCredentialConfigurationError
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


async def fetch_credential_item_background(item_id: str) -> None:
    """Fetch a new Bitwarden item early so the next request does not wait for vault sync."""
    try:
        LOG.info("Pre-fetching credential item from Bitwarden in background", item_id=item_id)
        credential_item = await BitwardenService.get_credential_item(item_id)
        LOG.info("Successfully fetched credential item from Bitwarden", item_id=item_id, name=credential_item.name)
    except Exception as e:
        LOG.exception("Failed to fetch credential item from Bitwarden in background", item_id=item_id, error=str(e))


async def validate_credential_browser_profile_id(
    browser_profile_id: str,
    organization_id: str,
    current_credential_id: str | None = None,
) -> None:
    """Only allow linking an unowned, unmanaged profile from the same organization."""
    profile = await app.DATABASE.browser_sessions.get_browser_profile(
        profile_id=browser_profile_id, organization_id=organization_id
    )
    if profile is None:
        raise HTTPException(
            status_code=400, detail=f"Browser profile not found, browser_profile_id={browser_profile_id}"
        )
    if profile.is_managed or profile.workflow_permanent_id is not None:
        raise HTTPException(
            status_code=400,
            detail="A credential can only link a plain browser profile, not a workflow-managed one.",
        )
    links_by_profile = await app.DATABASE.credentials.get_browser_profile_credential_links(
        {browser_profile_id}, organization_id
    )
    if any(
        credential_id != current_credential_id
        for credential_id, _folder_id in links_by_profile.get(browser_profile_id, [])
    ):
        raise HTTPException(status_code=400, detail="This browser profile is already linked to another credential.")


async def update_credential_or_profile_conflict(**kwargs: Any) -> Credential:
    """Translate the profile-link unique constraint into the same 400 as the pre-write check."""
    try:
        return await app.DATABASE.credentials.update_credential(**kwargs)
    except IntegrityError as exc:
        if "uq_credentials_browser_profile_id" in str(getattr(exc, "orig", exc)):
            raise HTTPException(status_code=400, detail="This browser profile is already linked to another credential.")
        raise


async def get_credential_vault_service(
    vault_type_override: CredentialVaultType | None = None,
    organization_id: str | None = None,
) -> CredentialVaultService:
    """Resolve and, when writing, preflight the configured vault service."""
    settings = SettingsManager.get_settings()
    vault_type = vault_type_override or settings.CREDENTIAL_VAULT_TYPE
    service: CredentialVaultService
    if vault_type == CredentialVaultType.SKYVERN:
        if not settings.is_local_credential_vault_enabled():
            raise HTTPException(status_code=400, detail="Skyvern local credential vault is not enabled")
        if not app.SKYVERN_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Skyvern local credential vault is not configured")
        service = app.SKYVERN_CREDENTIAL_VAULT_SERVICE
    elif vault_type == CredentialVaultType.BITWARDEN:
        service = app.BITWARDEN_CREDENTIAL_VAULT_SERVICE
    elif vault_type == CredentialVaultType.AZURE_VAULT:
        if not app.AZURE_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Azure Vault credential is not supported")
        service = app.AZURE_CREDENTIAL_VAULT_SERVICE
    elif vault_type == CredentialVaultType.GCP:
        if not app.GCP_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="GCP credential vault is not supported")
        service = app.GCP_CREDENTIAL_VAULT_SERVICE
    elif vault_type == CredentialVaultType.CUSTOM:
        if not app.CUSTOM_CREDENTIAL_VAULT_SERVICE:
            raise HTTPException(status_code=400, detail="Custom credential vault is not configured")
        service = app.CUSTOM_CREDENTIAL_VAULT_SERVICE
    elif vault_type == CredentialVaultType.ONEPASSWORD:
        service = app.ONEPASSWORD_CREDENTIAL_VAULT_SERVICE
    else:
        raise HTTPException(status_code=400, detail="Credential storage not supported")

    if organization_id is not None:
        try:
            await service.validate_organization_configuration(organization_id)
        except CustomCredentialConfigurationError as e:
            # The vault service already reported this error; avoid double-counting it.
            raise HTTPException(status_code=400, detail=str(e)) from e
    return service


async def prepare_credential_create(
    organization_id: str,
    data: CreateCredentialRequest,
    *,
    is_passkey_setup_pending: bool = False,
) -> CredentialVaultService:
    """Validate a create before it touches a vault and resolve its service."""
    is_passkey_setup = (
        data.credential_type == CredentialType.PASSWORD
        and isinstance(data.credential, NonEmptyPasswordCredential)
        and data.credential.totp_type == TotpType.PASSKEY
    )
    if is_passkey_setup != is_passkey_setup_pending:
        raise HTTPException(status_code=400, detail="Passkey credentials must use the passkey setup route")

    await app.AGENT_FUNCTION.validate_credential_write(
        organization_id=organization_id,
        credential_type=data.credential_type,
        credential=data.credential,
        existing_credential=None,
    )
    if data.browser_profile_id is not None:
        await validate_credential_browser_profile_id(data.browser_profile_id, organization_id)
    return await get_credential_vault_service(
        vault_type_override=data.vault_type,
        organization_id=organization_id,
    )


async def store_new_credential(
    organization_id: str,
    data: CreateCredentialRequest,
    created_by: str | None,
    credential_service: CredentialVaultService,
    background_tasks: BackgroundTasks,
    *,
    is_passkey_setup_pending: bool = False,
) -> Credential:
    try:
        if is_passkey_setup_pending:
            credential = await credential_service.create_credential(
                organization_id=organization_id,
                data=data,
                created_by=created_by,
                is_passkey_setup_pending=True,
            )
        else:
            credential = await credential_service.create_credential(
                organization_id=organization_id,
                data=data,
                created_by=created_by,
            )
    except SkyvernHttpException as e:
        detail = (
            f"Custom credential service returned {e.error_message}"
            if e.error_message
            else f"Custom credential service returned HTTP {e.status_code}"
        )
        raise HTTPException(status_code=502, detail=detail) from e

    await record_request_audit_event(organization_id, "credential.create", "credential", credential.credential_id)
    if credential.vault_type == CredentialVaultType.BITWARDEN:
        background_tasks.add_task(fetch_credential_item_background, credential.item_id)

    pin_provided = "pin_saved_session_ip" in data.model_fields_set
    bpid_provided = "browser_profile_id" in data.model_fields_set
    auto_profile_disabled_provided = "auto_profile_disabled" in data.model_fields_set
    if bpid_provided or pin_provided or auto_profile_disabled_provided:
        profile_update: dict[str, Any] = {"browser_profile_id": data.browser_profile_id} if bpid_provided else {}
        credential = await update_credential_or_profile_conflict(
            credential_id=credential.credential_id,
            organization_id=organization_id,
            **profile_update,
            pin_saved_session_ip=data.pin_saved_session_ip if pin_provided else None,
            auto_profile_disabled=data.auto_profile_disabled if auto_profile_disabled_provided else None,
        )

    background_tasks.add_task(
        app.AGENT_FUNCTION.on_credential_saved,
        organization_id=organization_id,
        credential_id=credential.credential_id,
        credential_type=data.credential_type,
        actor_user_id=created_by,
        vault=credential.vault_type.value if credential.vault_type is not None else None,
    )
    return credential


def convert_to_response(credential: Credential) -> CredentialResponse:
    """Convert an internal ``Credential`` to a safe API response.

    SECURITY: Copy only non-sensitive metadata. Never include passwords, TOTP secrets, full card numbers, CVVs,
    expiration dates, card holder names, credit card billing/contact fields, credit card metadata, or secret values.
    See the route module docstring for the full security invariant.
    """
    is_onepassword = credential.vault_type == CredentialVaultType.ONEPASSWORD
    if credential.credential_type == CredentialType.PASSWORD:
        credential_response = PasswordCredentialResponse(
            username=credential.username or credential.credential_id,
            totp_type=credential.totp_type,
            totp_identifier=credential.totp_identifier,
            has_totp=bool(credential.has_totp_seed),
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            vault_type=credential.vault_type,
            vault_id=credential.vault_id if is_onepassword else None,
            item_id=credential.item_id if is_onepassword else None,
            upstream_missing_at=credential.upstream_missing_at if is_onepassword else None,
            browser_profile_id=credential.browser_profile_id,
            auto_profile_disabled=credential.auto_profile_disabled,
            pin_saved_session_ip=credential.pin_saved_session_ip,
            tested_url=credential.tested_url,
            user_context=credential.user_context,
            save_browser_session_intent=credential.save_browser_session_intent,
            run_sequentially=credential.run_sequentially,
            folder_id=credential.folder_id,
            proxy_location=credential.proxy_location,
            proxy_session_id=credential.proxy_session_id,
            created_by=credential.created_by,
        )
    if credential.credential_type == CredentialType.CREDIT_CARD:
        credential_response = CreditCardCredentialResponse(
            last_four=credential.card_last4 or "****",
            brand=credential.card_brand or "Card Brand",
        )
        return CredentialResponse(
            credential=credential_response,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            vault_type=credential.vault_type,
            vault_id=credential.vault_id if is_onepassword else None,
            item_id=credential.item_id if is_onepassword else None,
            upstream_missing_at=credential.upstream_missing_at if is_onepassword else None,
            browser_profile_id=credential.browser_profile_id,
            auto_profile_disabled=credential.auto_profile_disabled,
            pin_saved_session_ip=credential.pin_saved_session_ip,
            tested_url=credential.tested_url,
            user_context=credential.user_context,
            save_browser_session_intent=credential.save_browser_session_intent,
            run_sequentially=credential.run_sequentially,
            folder_id=credential.folder_id,
            proxy_location=credential.proxy_location,
            proxy_session_id=credential.proxy_session_id,
            created_by=credential.created_by,
        )
    if credential.credential_type == CredentialType.SECRET:
        return CredentialResponse(
            credential=SecretCredentialResponse(secret_label=credential.secret_label),
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            name=credential.name,
            vault_type=credential.vault_type,
            vault_id=credential.vault_id if is_onepassword else None,
            item_id=credential.item_id if is_onepassword else None,
            upstream_missing_at=credential.upstream_missing_at if is_onepassword else None,
            browser_profile_id=credential.browser_profile_id,
            auto_profile_disabled=credential.auto_profile_disabled,
            pin_saved_session_ip=credential.pin_saved_session_ip,
            tested_url=credential.tested_url,
            user_context=credential.user_context,
            save_browser_session_intent=credential.save_browser_session_intent,
            run_sequentially=credential.run_sequentially,
            folder_id=credential.folder_id,
            proxy_location=credential.proxy_location,
            proxy_session_id=credential.proxy_session_id,
            created_by=credential.created_by,
        )
    raise HTTPException(status_code=400, detail="Credential type not supported")
