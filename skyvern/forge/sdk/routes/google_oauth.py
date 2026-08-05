import asyncio
import random
import time
from typing import Annotated

import httpx
import requests  # google-auth-oauthlib's Flow.fetch_token uses requests under the hood; we catch its transport errors.
import structlog
from fastapi import APIRouter, Depends, HTTPException
from google.auth.exceptions import GoogleAuthError
from oauthlib.oauth2 import InvalidGrantError, OAuth2Error

from skyvern.forge.sdk.schemas.google_oauth import (
    CreateGoogleOAuthAuthorizeRequest,
    CreateGoogleOAuthCallbackRequest,
    GoogleOAuthAuthorizeResponse,
    GoogleOAuthClientConfig,
    GoogleOAuthClientConfigResponse,
    GoogleOAuthCredentialBase,
    GoogleOAuthCredentialListResponse,
    GoogleOAuthCredentialResponse,
    UpdateGoogleOAuthClientConfigRequest,
    UpdateGoogleOAuthCredentialRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import google_gmail_service, google_oauth_service, org_auth_service
from skyvern.forge.sdk.services.google_oauth_service import InvalidAppOriginError
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.utils.email_validation import normalize_email_address

LOG = structlog.get_logger()

google_oauth_router = APIRouter()

_EMAIL_BACKFILL_FAILURE_TTL_SECONDS = 3600.0
# Per-process cache bounds provider spend, not correctness.
_EMAIL_BACKFILL_FAILURES: dict[str, float] = {}


async def _backfill_google_email_addresses(
    *,
    organization_id: str,
    credentials: list[GoogleOAuthCredentialBase],
) -> None:
    now = time.monotonic()
    for credential_id, deadline in list(_EMAIL_BACKFILL_FAILURES.items()):
        if deadline <= now:
            _EMAIL_BACKFILL_FAILURES.pop(credential_id, None)
    eligible_candidates = [
        credential
        for credential in credentials
        if credential.email_address is None
        and credential.id not in _EMAIL_BACKFILL_FAILURES
        and credential.state == google_oauth_service.STATE_ACTIVE
        and google_oauth_service.has_required_scopes(
            credential.scopes_granted,
            google_oauth_service.GOOGLE_GMAIL_SCOPES,
        )
    ]
    candidates = random.sample(eligible_candidates, k=min(3, len(eligible_candidates)))
    for credential in candidates:
        try:
            secrets = await google_oauth_service.load_credential_secrets(
                organization_id=organization_id,
                credential_id=credential.id,
            )
            refresh_result = await google_oauth_service.refresh_and_rotate(
                organization_id=organization_id,
                credential_id=credential.id,
                credential_secrets=secrets,
            )
            email_address = await google_gmail_service.fetch_profile_email(access_token=refresh_result.access_token)
            if email_address is None:
                _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
                continue
            email_address = normalize_email_address(email_address)
            updated = await google_oauth_service.update_email_address(
                organization_id=organization_id,
                credential_id=credential.id,
                email_address=email_address,
                only_if_null=True,
                expected_version=refresh_result.credential_version,
            )
            if updated:
                credential.email_address = email_address
        except asyncio.CancelledError:
            _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
            raise
        except Exception:
            _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
            LOG.warning(
                "Failed to backfill Google account email",
                credential_id=credential.id,
                exc_info=True,
            )


def _require_organization_client_config_enabled() -> None:
    if not SettingsManager.get_settings().ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG:
        raise HTTPException(status_code=404, detail="Google OAuth client config is not available")


def _require_scopes_from_token(token_data: dict) -> list[str]:
    """Extract the granted scopes from Google's token response, failing closed on empty/missing"""
    raw = token_data.get("scope")
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail="Google did not return any granted scopes. Please re-authorize and grant all requested scopes.",
        )
    if isinstance(raw, str):
        parts = [p for p in raw.replace(",", " ").split() if p]
        if not parts:
            raise HTTPException(
                status_code=400,
                detail="Google returned an empty scope. Please re-authorize and grant all requested scopes.",
            )
        return parts
    parts = [p for p in (str(s).strip() for s in raw) if p]
    if not parts:
        raise HTTPException(
            status_code=400,
            detail="Google returned no granted scopes. Please re-authorize and grant all requested scopes.",
        )
    return parts


@google_oauth_router.post("/oauth/authorize")
async def google_oauth_authorize(
    request: CreateGoogleOAuthAuthorizeRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
) -> GoogleOAuthAuthorizeResponse:
    """Kick off the Google OAuth 2.0 authorization flow."""
    try:
        start = await google_oauth_service.start_authorization(
            organization_id=current_org.organization_id,
            redirect_uri=request.redirect_uri,
            credential_name=request.credential_name,
            scope_profile=request.scope_profile,
            app_origin=request.app_origin,
            credential_id=request.credential_id,
            initiator_id=current_user_id,
        )
    except google_oauth_service.CredentialNotReauthorizableError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidAppOriginError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.UnsupportedScopeProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.InvalidRedirectURIError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.OrganizationClientConfigUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return GoogleOAuthAuthorizeResponse(authorize_url=start.authorize_url, state=start.state)


@google_oauth_router.post("/oauth/callback")
async def google_oauth_callback(
    request: CreateGoogleOAuthCallbackRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
) -> GoogleOAuthCredentialResponse:
    """Handle the Google OAuth 2.0 authorization callback."""
    context = await google_oauth_service.load_pending_consent_context(
        organization_id=current_org.organization_id,
        state=request.state,
        initiator_id=current_user_id,
    )
    if context is None or not context.consent_redirect_uri:
        raise HTTPException(
            status_code=400,
            detail="This OAuth consent request is unknown, expired, or was not started by you. Restart the connection.",
        )
    if not context.consent_code_verifier:
        raise HTTPException(
            status_code=400,
            detail="OAuth consent row is missing the PKCE verifier; restart the consent flow",
        )
    try:
        resolved = await google_oauth_service.resolve_client_config(current_org.organization_id)
    except google_oauth_service.OrganizationClientConfigUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if context.client_id is not None and (resolved.config is None or resolved.config.client_id != context.client_id):
        raise HTTPException(
            status_code=409,
            detail="Google OAuth client configuration changed since consent started; restart the connection",
        )

    try:
        token_data = await google_oauth_service.exchange_code_for_tokens(
            code=request.code,
            redirect_uri=context.consent_redirect_uri,
            code_verifier=context.consent_code_verifier,
            organization_id=current_org.organization_id,
            client_config=resolved.config,
        )
    except google_oauth_service.OrganizationClientConfigUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        LOG.exception("Google OAuth client credentials not configured")
        raise HTTPException(status_code=503, detail=str(exc))
    except InvalidGrantError as exc:
        LOG.warning("Google OAuth invalid_grant on code exchange", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
    except OAuth2Error:
        # Don't echo the exception string — OAuth2Error messages can carry the
        # short-lived auth code or token-endpoint URLs. The full trace is in LOG.exception.
        LOG.exception("Google OAuth protocol error on code exchange")
        raise HTTPException(status_code=502, detail="Google OAuth exchange failed")
    except (httpx.HTTPError, requests.RequestException, GoogleAuthError):
        LOG.exception("Transport failure exchanging Google OAuth code")
        raise HTTPException(status_code=502, detail="Upstream Google token endpoint failed")
    except Exception:
        LOG.exception("Unexpected failure exchanging Google OAuth code for tokens")
        raise HTTPException(status_code=500, detail="Failed to exchange authorization code")

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token received. Ensure access_type=offline and prompt=consent in the OAuth flow.",
        )
    scopes_granted = _require_scopes_from_token(token_data)

    try:
        credential = await google_oauth_service.promote_pending_credential(
            organization_id=current_org.organization_id,
            state=request.state,
            initiator_id=current_user_id,
            refresh_token=refresh_token,
            scopes_granted=scopes_granted,
        )
    except google_oauth_service.InvalidConsentNonceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        if google_oauth_service.has_required_scopes(
            scopes_granted,
            google_oauth_service.GOOGLE_GMAIL_SCOPES,
        ):
            email_address = await google_gmail_service.fetch_profile_email(
                access_token=token_data["access_token"],
            )
            if email_address is not None:
                email_address = normalize_email_address(email_address)
                updated = await google_oauth_service.update_email_address(
                    organization_id=current_org.organization_id,
                    credential_id=credential.id,
                    email_address=email_address,
                    only_if_null=False,
                    expected_version=credential.modified_at,
                )
                if updated:
                    credential.email_address = email_address
                else:
                    LOG.debug(
                        "Skipped Google account email update after credential changed",
                        credential_id=credential.id,
                    )
    except Exception:
        LOG.warning("Failed to resolve Google account email", exc_info=True)

    # ``consent_app_origin`` was validated against ``GOOGLE_OAUTH_APP_ORIGINS`` in
    # ``start_authorization`` before being persisted to the pending row, so reading
    # it back here is safe. A future refactor that bypasses that pre-storage check
    # must re-validate before echoing this value to the client.
    return GoogleOAuthCredentialResponse(credential=credential, app_origin=context.consent_app_origin)


@google_oauth_router.get("/oauth/config")
async def get_google_oauth_client_config(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GoogleOAuthClientConfigResponse:
    """Return the effective Google OAuth client configuration without the client secret."""
    _require_organization_client_config_enabled()
    try:
        resolved = await google_oauth_service.resolve_client_config(current_org.organization_id)
    except google_oauth_service.OrganizationClientConfigUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return GoogleOAuthClientConfigResponse(config=resolved.safe())


@google_oauth_router.put("/oauth/config")
async def update_google_oauth_client_config(
    request: UpdateGoogleOAuthClientConfigRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GoogleOAuthClientConfigResponse:
    """Store an organization-level Google OAuth client configuration."""
    _require_organization_client_config_enabled()
    try:
        resolved = await google_oauth_service.resolve_client_config(current_org.organization_id)
    except google_oauth_service.OrganizationClientConfigUnavailableError:
        resolved = None
    client_secret = request.client_secret
    if (
        client_secret is None
        and resolved
        and resolved.source == "organization"
        and resolved.config
        and resolved.config.client_id == request.client_id
    ):
        client_secret = resolved.config.client_secret
    if not client_secret:
        raise HTTPException(status_code=400, detail="Google OAuth client secret is required")

    config = GoogleOAuthClientConfig(
        client_id=request.client_id,
        client_secret=client_secret,
        redirect_hosts=request.redirect_hosts,
        app_origins=request.app_origins,
    )
    try:
        saved = await google_oauth_service.save_client_config(current_org.organization_id, config)
    except google_oauth_service.OrganizationGoogleOAuthConfigDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return GoogleOAuthClientConfigResponse(config=saved.safe())


@google_oauth_router.delete("/oauth/config")
async def delete_google_oauth_client_config(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> dict[str, bool]:
    """Clear the organization-level Google OAuth client config and fall back to environment config."""
    _require_organization_client_config_enabled()
    try:
        await google_oauth_service.delete_client_config(current_org.organization_id)
    except google_oauth_service.OrganizationGoogleOAuthConfigDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"success": True}


@google_oauth_router.get("/oauth/credentials")
async def list_google_oauth_credentials(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    include_email: bool = False,
) -> GoogleOAuthCredentialListResponse:
    """Fetch a list of Google OAuth credentials associated with an organization."""
    credentials = await google_oauth_service.get_visible_credentials_for_org(
        organization_id=current_org.organization_id,
    )
    if include_email:
        try:
            async with asyncio.timeout(10):
                await _backfill_google_email_addresses(
                    organization_id=current_org.organization_id,
                    credentials=credentials,
                )
        except TimeoutError:
            LOG.warning("Timed out backfilling Google account emails")
        except Exception:
            LOG.warning("Failed to backfill Google account emails", exc_info=True)
    return GoogleOAuthCredentialListResponse(credentials=credentials)


@google_oauth_router.patch("/oauth/credentials/{credential_id}")
async def rename_google_oauth_credential(
    credential_id: str,
    request: UpdateGoogleOAuthCredentialRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GoogleOAuthCredentialResponse:
    """Renames an existing Google OAuth credential for the specified organization"""
    updated = await google_oauth_service.rename_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
        credential_name=request.credential_name,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return GoogleOAuthCredentialResponse(credential=updated)


@google_oauth_router.delete(
    "/oauth/credentials/{credential_id}",
)
async def delete_google_oauth_credential(
    credential_id: str,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> dict[str, bool]:
    """Deletes a specific Google OAuth credential associated with an organization"""
    revoked = await google_oauth_service.revoke_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}
