from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from skyvern.forge.sdk.schemas.microsoft_oauth import (
    CreateMicrosoftOAuthAuthorizeRequest,
    CreateMicrosoftOAuthCallbackRequest,
    MicrosoftOAuthAuthorizeResponse,
    MicrosoftOAuthCredentialListResponse,
    MicrosoftOAuthCredentialResponse,
    UpdateMicrosoftOAuthCredentialRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import microsoft_oauth_service, org_auth_service
from skyvern.forge.sdk.services.microsoft_oauth_service import InvalidAppOriginError

LOG = structlog.get_logger()

microsoft_oauth_router = APIRouter()


def _require_scopes_from_token(token_data: dict) -> list[str]:
    raw = token_data.get("scope")
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail="Microsoft did not return any granted scopes. Please re-authorize and grant all requested scopes.",
        )
    if isinstance(raw, str):
        parts = [p for p in raw.replace(",", " ").split() if p]
        if not parts:
            raise HTTPException(
                status_code=400,
                detail="Microsoft returned an empty scope. Please re-authorize and grant all requested scopes.",
            )
        return parts
    parts = [p for p in (str(s).strip() for s in raw) if p]
    if not parts:
        raise HTTPException(
            status_code=400,
            detail="Microsoft returned no granted scopes. Please re-authorize and grant all requested scopes.",
        )
    return parts


@microsoft_oauth_router.post("/oauth/authorize")
async def microsoft_oauth_authorize(
    request: CreateMicrosoftOAuthAuthorizeRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> MicrosoftOAuthAuthorizeResponse:
    try:
        start = await microsoft_oauth_service.start_authorization(
            organization_id=current_org.organization_id,
            redirect_uri=request.redirect_uri,
            credential_name=request.credential_name,
            scope_profile=request.scope_profile,
            app_origin=request.app_origin,
        )
    except InvalidAppOriginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except microsoft_oauth_service.UnsupportedScopeProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except microsoft_oauth_service.InvalidRedirectURIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except microsoft_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return MicrosoftOAuthAuthorizeResponse(authorize_url=start.authorize_url, state=start.state)


@microsoft_oauth_router.post("/oauth/callback")
async def microsoft_oauth_callback(
    request: CreateMicrosoftOAuthCallbackRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> MicrosoftOAuthCredentialResponse:
    context = await microsoft_oauth_service.load_pending_consent_context(
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
        token_data = await microsoft_oauth_service.exchange_code_for_tokens(
            code=request.code,
            redirect_uri=context.consent_redirect_uri,
            code_verifier=context.consent_code_verifier,
            scopes=context.scopes_requested,
        )
    except ValueError as exc:
        LOG.exception("Microsoft OAuth client credentials not configured")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except microsoft_oauth_service.MicrosoftOAuthError as exc:
        LOG.warning("Microsoft OAuth exchange failed", error=str(exc))
        raise HTTPException(
            status_code=400,
            detail="Microsoft authorization failed. Please reconnect the Microsoft account.",
        ) from exc
    except microsoft_oauth_service.MissingAccessTokenError as exc:
        LOG.warning("Microsoft OAuth token response missing access token", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        LOG.exception("Unexpected failure exchanging Microsoft OAuth code for tokens")
        raise HTTPException(status_code=500, detail="Failed to exchange authorization code") from exc

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token received. Ensure offline_access is included in the OAuth flow.",
        )
    scopes_granted = _require_scopes_from_token(token_data)
    if not microsoft_oauth_service.has_required_scopes(scopes_granted, ["Mail.Read"]):
        raise HTTPException(
            status_code=400,
            detail="Microsoft did not grant Mail.Read. Please re-connect and accept all requested permissions.",
        )

    try:
        credential = await microsoft_oauth_service.promote_pending_credential(
            organization_id=current_org.organization_id,
            nonce=request.state,
            refresh_token=refresh_token,
            scopes_granted=scopes_granted,
        )
    except microsoft_oauth_service.InvalidConsentNonceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except microsoft_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return MicrosoftOAuthCredentialResponse(credential=credential, app_origin=context.consent_app_origin)


@microsoft_oauth_router.get("/oauth/credentials")
async def list_microsoft_oauth_credentials(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> MicrosoftOAuthCredentialListResponse:
    credentials = await microsoft_oauth_service.get_credentials_for_org(
        organization_id=current_org.organization_id,
    )
    return MicrosoftOAuthCredentialListResponse(credentials=credentials)


@microsoft_oauth_router.patch("/oauth/credentials/{credential_id}")
async def rename_microsoft_oauth_credential(
    credential_id: str,
    request: UpdateMicrosoftOAuthCredentialRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> MicrosoftOAuthCredentialResponse:
    updated = await microsoft_oauth_service.rename_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
        credential_name=request.credential_name,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return MicrosoftOAuthCredentialResponse(credential=updated)


@microsoft_oauth_router.delete(
    "/oauth/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_microsoft_oauth_credential(
    credential_id: str,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> None:
    revoked = await microsoft_oauth_service.revoke_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Credential not found")
