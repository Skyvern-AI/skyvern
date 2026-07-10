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
    GoogleOAuthCredentialListResponse,
    GoogleOAuthCredentialResponse,
    UpdateGoogleOAuthClientConfigRequest,
    UpdateGoogleOAuthCredentialRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import google_oauth_service, org_auth_service
from skyvern.forge.sdk.services.google_oauth_service import InvalidAppOriginError
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

google_oauth_router = APIRouter()


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
) -> GoogleOAuthAuthorizeResponse:
    """Kick off the Google OAuth 2.0 authorization flow."""
    try:
        start = await google_oauth_service.start_authorization(
            organization_id=current_org.organization_id,
            redirect_uri=request.redirect_uri,
            credential_name=request.credential_name,
            scope_profile=request.scope_profile,
            app_origin=request.app_origin,
        )
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
) -> GoogleOAuthCredentialResponse:
    """Handle the Google OAuth 2.0 authorization callback."""
    context = await google_oauth_service.load_pending_consent_context(
        organization_id=current_org.organization_id,
        nonce=request.state,
    )
    if context is None or not context.consent_redirect_uri:
        raise HTTPException(status_code=400, detail="Unknown or consumed OAuth consent nonce")
    if not context.consent_code_verifier:
        raise HTTPException(
            status_code=400,
            detail="OAuth consent row is missing the PKCE verifier; restart the consent flow",
        )

    try:
        token_data = await google_oauth_service.exchange_code_for_tokens(
            code=request.code,
            redirect_uri=context.consent_redirect_uri,
            code_verifier=context.consent_code_verifier,
            organization_id=current_org.organization_id,
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
            nonce=request.state,
            refresh_token=refresh_token,
            scopes_granted=scopes_granted,
        )
    except google_oauth_service.InvalidConsentNonceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

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
) -> GoogleOAuthCredentialListResponse:
    """Fetch a list of Google OAuth credentials associated with an organization."""
    credentials = await google_oauth_service.get_credentials_for_org(
        organization_id=current_org.organization_id,
    )
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
