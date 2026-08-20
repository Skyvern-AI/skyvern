import asyncio
import random
import time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from skyvern.forge.sdk.schemas.microsoft_oauth import (
    CreateMicrosoftOAuthAuthorizeRequest,
    CreateMicrosoftOAuthCallbackRequest,
    MicrosoftOAuthAuthorizeResponse,
    MicrosoftOAuthCredentialBase,
    MicrosoftOAuthCredentialListResponse,
    MicrosoftOAuthCredentialResponse,
    UpdateMicrosoftOAuthCredentialRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import microsoft_oauth_service, org_auth_service
from skyvern.forge.sdk.services.microsoft_oauth_service import InvalidAppOriginError
from skyvern.services.email import outlook
from skyvern.utils.email_validation import normalize_email_address

LOG = structlog.get_logger()

microsoft_oauth_router = APIRouter()

_EMAIL_BACKFILL_FAILURE_TTL_SECONDS = 3600.0
# Per-process cache bounds provider spend, not correctness.
_EMAIL_BACKFILL_FAILURES: dict[str, float] = {}


async def _backfill_microsoft_email_addresses(
    *,
    organization_id: str,
    credentials: list[MicrosoftOAuthCredentialBase],
) -> None:
    now = time.monotonic()
    for credential_id, deadline in list(_EMAIL_BACKFILL_FAILURES.items()):
        if deadline <= now:
            _EMAIL_BACKFILL_FAILURES.pop(credential_id, None)
    eligible_candidates = [
        credential
        for credential in credentials
        if credential.email_address is None and credential.id not in _EMAIL_BACKFILL_FAILURES
    ]
    candidates = random.sample(eligible_candidates, k=min(3, len(eligible_candidates)))
    for credential in candidates:
        try:
            secrets = await microsoft_oauth_service.load_credential_secrets(
                organization_id=organization_id,
                credential_id=credential.id,
            )
            access_token = await microsoft_oauth_service.refresh_and_rotate(
                organization_id=organization_id,
                credential_id=credential.id,
                credential_secrets=secrets,
            )
            email_address = await outlook.fetch_primary_account_email(access_token=access_token)
            if email_address is None:
                _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
                continue
            email_address = normalize_email_address(email_address)
            updated = await microsoft_oauth_service.update_email_address(
                organization_id=organization_id,
                credential_id=credential.id,
                email_address=email_address,
                only_if_null=True,
            )
            if updated:
                credential.email_address = email_address
        except asyncio.CancelledError:
            _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
            raise
        except Exception:
            _EMAIL_BACKFILL_FAILURES[credential.id] = time.monotonic() + _EMAIL_BACKFILL_FAILURE_TTL_SECONDS
            LOG.warning(
                "Failed to backfill Microsoft account email",
                credential_id=credential.id,
                exc_info=True,
            )


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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
) -> MicrosoftOAuthAuthorizeResponse:
    try:
        start = await microsoft_oauth_service.start_authorization(
            organization_id=current_org.organization_id,
            redirect_uri=request.redirect_uri,
            credential_name=request.credential_name,
            scope_profile=request.scope_profile,
            app_origin=request.app_origin,
            initiator_id=current_user_id,
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    current_user_id: Annotated[str | None, Depends(org_auth_service.get_current_user_id_or_none)],
) -> MicrosoftOAuthCredentialResponse:
    context = await microsoft_oauth_service.load_pending_consent_context(
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
            state=request.state,
            initiator_id=current_user_id,
            refresh_token=refresh_token,
            scopes_granted=scopes_granted,
        )
    except microsoft_oauth_service.InvalidConsentNonceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except microsoft_oauth_service.EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    email_address = None
    should_resolve_email = True
    try:
        email_address = await outlook.fetch_primary_account_email(
            access_token=token_data["access_token"],
        )
    except outlook.OutlookAPIError as exc:
        if 400 <= exc.status < 500 and exc.status not in {401, 408, 429}:
            LOG.warning("Graph mail unavailable; falling back to Microsoft identity claims", exc_info=True)
        else:
            LOG.debug(
                "Skipped Microsoft account email update after transient Graph failure",
                credential_id=credential.id,
                exc_info=True,
            )
            should_resolve_email = False
    except Exception:
        LOG.warning("Failed to resolve Microsoft account email", exc_info=True)
        should_resolve_email = False

    if should_resolve_email:
        try:
            if email_address is None:
                # Claims are safe after Graph definitively returns no mail or a non-transient client error.
                id_token = token_data.get("id_token")
                email_address = (
                    microsoft_oauth_service.email_from_id_token(id_token) if isinstance(id_token, str) else None
                )
            if email_address is not None:
                email_address = normalize_email_address(email_address)
                updated = await microsoft_oauth_service.update_email_address(
                    organization_id=current_org.organization_id,
                    credential_id=credential.id,
                    email_address=email_address,
                    only_if_null=False,
                )
                if updated:
                    credential.email_address = email_address
        except Exception:
            LOG.warning("Failed to resolve Microsoft account email", exc_info=True)

    return MicrosoftOAuthCredentialResponse(credential=credential, app_origin=context.consent_app_origin)


@microsoft_oauth_router.get("/oauth/credentials")
async def list_microsoft_oauth_credentials(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    include_email: bool = False,
) -> MicrosoftOAuthCredentialListResponse:
    credentials = await microsoft_oauth_service.get_credentials_for_org(
        organization_id=current_org.organization_id,
    )
    if include_email:
        try:
            async with asyncio.timeout(10):
                await _backfill_microsoft_email_addresses(
                    organization_id=current_org.organization_id,
                    credentials=credentials,
                )
        except TimeoutError:
            LOG.warning("Timed out backfilling Microsoft account emails")
        except Exception:
            LOG.warning("Failed to backfill Microsoft account emails", exc_info=True)
    return MicrosoftOAuthCredentialListResponse(credentials=credentials)


@microsoft_oauth_router.patch("/oauth/credentials/{credential_id}")
async def rename_microsoft_oauth_credential(
    credential_id: str,
    request: UpdateMicrosoftOAuthCredentialRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
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
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> None:
    revoked = await microsoft_oauth_service.revoke_credential(
        organization_id=current_org.organization_id,
        credential_id=credential_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Credential not found")
