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

from skyvern.forge import app
from skyvern.forge.agent_functions import record_request_audit_event
from skyvern.forge.sdk.integrations.catalog import has_native_gmail_credential
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
from skyvern.forge.sdk.services import (
    google_gmail_service,
    google_oauth_service,
    oauth_redirect_uri,
    org_auth_service,
)
from skyvern.forge.sdk.services.google_oauth_service import InvalidAppOriginError
from skyvern.forge.sdk.services.route_authorization import require_request_actions
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.utils.email_validation import normalize_email_address

LOG = structlog.get_logger()
REDIRECT_URIS_REQUIRED_MESSAGE = (
    "Enter the exact redirect URIs registered for this Google OAuth client, for example "
    "https://app.example.com/integrations/google/callback. Host-only redirect entries are no longer accepted."
)

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


CONNECTED_GMAIL_DEFAULT_MESSAGE = "New Gmail connections use Connected Gmail. Connect Gmail from the Integrations page."
_GMAIL_SCOPE_PROFILES = frozenset(
    {
        google_oauth_service.GOOGLE_OAUTH_SCOPE_PROFILE_GMAIL,
        google_oauth_service.GOOGLE_OAUTH_SCOPE_PROFILE_GMAIL_SEND,
        google_oauth_service.GOOGLE_OAUTH_SCOPE_PROFILE_GMAIL_READ_SEND,
    }
)


async def _connected_gmail_replaces_new_native_gmail(
    request: CreateGoogleOAuthAuthorizeRequest, organization_id: str, user_id: str | None
) -> bool:
    # Connected Gmail is per user, so a caller with no user keeps the native path as its only way to add Gmail.
    if request.credential_id is not None or request.scope_profile not in _GMAIL_SCOPE_PROFILES or user_id is None:
        return False
    try:
        if not await app.AGENT_FUNCTION.nango_gmail_catalog_available(organization_id):
            return False
    except Exception:  # noqa: BLE001 - an unknown availability keeps the native path open.
        return False
    try:
        credentials = await google_oauth_service.get_visible_credentials_for_org(organization_id)
    except Exception:  # noqa: BLE001 - as on the page, an unreadable native list keeps the native path open.
        return False
    return not has_native_gmail_credential(credentials)


@google_oauth_router.post("/oauth/authorize")
async def google_oauth_authorize(
    request: CreateGoogleOAuthAuthorizeRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.authorize"}))),
) -> GoogleOAuthAuthorizeResponse:
    """Kick off the Google OAuth 2.0 authorization flow."""
    if await _connected_gmail_replaces_new_native_gmail(request, current_org.organization_id, current_user_id):
        raise HTTPException(status_code=409, detail=CONNECTED_GMAIL_DEFAULT_MESSAGE)
    try:
        start = await google_oauth_service.start_authorization(
            organization_id=current_org.organization_id,
            redirect_uri=request.redirect_uri,
            credential_name=request.credential_name,
            scope_profile=request.scope_profile,
            app_origin=request.app_origin,
            credential_id=request.credential_id,
            initiator_id=current_user_id,
            set_account=request.set_account,
        )
    except google_oauth_service.CredentialNotReauthorizableError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except google_oauth_service.GmailSendUpgradeUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.callback"}))),
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
    except InvalidGrantError:
        LOG.warning("Google OAuth invalid_grant on code exchange")
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
    except OAuth2Error as exc:
        LOG.warning("Google OAuth protocol error on code exchange", error_type=type(exc).__name__)
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

    identity = None
    requested_gmail_send = (
        google_oauth_service.GOOGLE_GMAIL_SEND_SCOPE in context.scopes_requested
        and google_oauth_service.GOOGLE_GMAIL_SEND_SCOPE not in context.scopes_granted
    )
    is_reconnect = context.state in {google_oauth_service.STATE_ACTIVE, google_oauth_service.STATE_ERROR}
    if requested_gmail_send or is_reconnect:
        try:
            identity = await google_oauth_service.verify_gmail_send_consent(
                context,
                scopes_granted,
                token_data.get("id_token"),
                resolved.config.client_id if resolved.config else "",
                gmail_send=requested_gmail_send,
            )
        except google_oauth_service.GmailSendConsentRejectedError as exc:
            LOG.info(
                "Rejected Google OAuth consent",
                organization_id=current_org.organization_id,
                credential_id=context.credential_id,
                rejection=exc.rejection.value,
            )
            raise HTTPException(status_code=409, detail=str(exc))
    if identity is None:
        identity = await google_oauth_service.verify_google_identity(
            token_data.get("id_token"),
            resolved.config.client_id if resolved.config else "",
        )
        if identity is None:
            if is_reconnect:
                raise HTTPException(
                    status_code=409,
                    detail="Google did not return a verified account. Try again. The previous connection is unchanged.",
                )
            LOG.warning(
                "Google connection stored without a verified account; it cannot be reconnected in place",
                organization_id=current_org.organization_id,
                credential_id=context.credential_id,
            )

    prior_state = context.state
    try:
        credential = await google_oauth_service.promote_pending_credential(
            organization_id=current_org.organization_id,
            state=request.state,
            initiator_id=current_user_id,
            refresh_token=refresh_token,
            scopes_granted=scopes_granted,
            identity=identity,
        )
    except google_oauth_service.InvalidConsentNonceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if prior_state in {google_oauth_service.STATE_ACTIVE, google_oauth_service.STATE_ERROR}:
        await record_request_audit_event(
            current_org.organization_id,
            "google_oauth.update",
            "google_oauth_credential",
            credential.id,
        )
    else:
        await record_request_audit_event(
            current_org.organization_id,
            "google_oauth.create",
            "google_oauth_credential",
            credential.id,
        )

    try:
        await app.AGENT_FUNCTION.on_integration_connected(
            organization_id=current_org.organization_id,
            provider="google",
            credential_id=credential.id,
            is_reconnect=is_reconnect,
            actor_user_id=current_user_id,
            connected_at=credential.modified_at,
        )
    except Exception:
        LOG.warning(
            "Google integration lifecycle analytics hook failed",
            organization_id=current_org.organization_id,
            credential_id=credential.id,
            exc_info=True,
        )

    try:
        # A send connection's address comes from the verified ID token and is never replaced by a profile lookup.
        if identity is None and google_oauth_service.has_required_scopes(
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.read"}))),
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.update"}))),
) -> GoogleOAuthClientConfigResponse:
    """Store an organization-level Google OAuth client configuration."""
    _require_organization_client_config_enabled()
    if not request.redirect_uris:
        raise HTTPException(status_code=400, detail=REDIRECT_URIS_REQUIRED_MESSAGE)
    for redirect_uri in request.redirect_uris:
        rejection = oauth_redirect_uri.registration_rejection(redirect_uri)
        if rejection:
            raise HTTPException(status_code=422, detail=f"Invalid redirect URI {redirect_uri}: {rejection}")
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
        redirect_uris=request.redirect_uris,
        app_origins=request.app_origins,
    )
    try:
        saved = await google_oauth_service.save_client_config(current_org.organization_id, config)
    except google_oauth_service.OrganizationGoogleOAuthConfigDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except google_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    existing_config = resolved.config if resolved and resolved.source == "organization" else None
    changed_fields = tuple(
        sorted(
            field_name
            for field_name in GoogleOAuthClientConfig.model_fields
            if existing_config is None or getattr(config, field_name) != getattr(existing_config, field_name)
        )
    )
    if existing_config is None:
        await record_request_audit_event(
            current_org.organization_id,
            "google_oauth.create",
            "google_oauth_client_config",
            current_org.organization_id,
            changed_fields=changed_fields,
        )
    elif changed_fields:
        await record_request_audit_event(
            current_org.organization_id,
            "google_oauth.update",
            "google_oauth_client_config",
            current_org.organization_id,
            changed_fields=changed_fields,
        )
    return GoogleOAuthClientConfigResponse(config=saved.safe())


@google_oauth_router.delete("/oauth/config")
async def delete_google_oauth_client_config(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.delete"}))),
) -> dict[str, bool]:
    """Clear the organization-level Google OAuth client config and fall back to environment config."""
    _require_organization_client_config_enabled()
    try:
        changed = await google_oauth_service.delete_client_config(current_org.organization_id)
    except google_oauth_service.OrganizationGoogleOAuthConfigDisabledError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if changed:
        await record_request_audit_event(
            current_org.organization_id,
            "google_oauth.delete",
            "google_oauth_client_config",
            current_org.organization_id,
        )
    return {"success": True}


@google_oauth_router.get("/oauth/credentials")
async def list_google_oauth_credentials(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.update"}))),
) -> GoogleOAuthCredentialResponse:
    """Renames an existing Google OAuth credential for the specified organization"""
    updated = await google_oauth_service.rename_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
        credential_name=request.credential_name,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    await record_request_audit_event(
        current_org.organization_id,
        "google_oauth.update",
        "google_oauth_credential",
        credential_id,
        changed_fields=("credential_name",),
    )
    return GoogleOAuthCredentialResponse(credential=updated)


@google_oauth_router.delete(
    "/oauth/credentials/{credential_id}",
)
async def delete_google_oauth_credential(
    credential_id: str,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    _request_authorization: None = Depends(require_request_actions(frozenset({"google_oauth.delete"}))),
) -> dict[str, bool]:
    """Deletes a specific Google OAuth credential associated with an organization"""
    revoked = await google_oauth_service.revoke_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Credential not found")
    await record_request_audit_event(
        current_org.organization_id,
        "google_oauth.delete",
        "google_oauth_credential",
        credential_id,
    )
    return {"success": True}
