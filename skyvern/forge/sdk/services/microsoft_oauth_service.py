from __future__ import annotations

import base64
import datetime
import hashlib
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import httpx
import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.id import generate_microsoft_oauth_credential_id
from skyvern.forge.sdk.db.repositories.microsoft_oauth import (  # noqa: F401
    STATE_ACTIVE,
    STATE_ERROR,
    STATE_PENDING_CONSENT,
    STATE_REVOKED,
    InvalidConsentNonceError,
    PendingConsentContext,
)
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase

LOG = structlog.get_logger()

MICROSOFT_AUTHORIZE_ENDPOINT_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_ENDPOINT_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

MICROSOFT_OAUTH_SCOPE_PROFILE_OUTLOOK_MAIL = "outlook_mail"
OUTLOOK_MAIL_SCOPES = ("Mail.Read", "offline_access", "openid", "profile", "User.Read")
MICROSOFT_OAUTH_SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    MICROSOFT_OAUTH_SCOPE_PROFILE_OUTLOOK_MAIL: OUTLOOK_MAIL_SCOPES,
}

CONSENT_TTL_SECONDS = 600


def microsoft_access_token_cache_key(organization_id: str, credential_id: str) -> str:
    return f"microsoft:access_token:{organization_id}:{credential_id}"


class EncryptionNotConfiguredError(RuntimeError):
    pass


class MissingAccessTokenError(RuntimeError):
    pass


class MicrosoftOAuthError(RuntimeError):
    pass


class InvalidRedirectURIError(ValueError):
    pass


class InvalidAppOriginError(ValueError):
    pass


class UnsupportedScopeProfileError(ValueError):
    pass


@dataclass(frozen=True)
class MicrosoftCredentialSecrets:
    refresh_token: str
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MicrosoftAuthorizationStart:
    authorize_url: str
    state: str


def _authorize_endpoint() -> str:
    return MICROSOFT_AUTHORIZE_ENDPOINT_TEMPLATE.format(tenant=settings.MICROSOFT_OAUTH_TENANT)


def _token_endpoint() -> str:
    return MICROSOFT_TOKEN_ENDPOINT_TEMPLATE.format(tenant=settings.MICROSOFT_OAUTH_TENANT)


def _require_encryption() -> None:
    if not settings.ENABLE_ENCRYPTION:
        raise EncryptionNotConfiguredError(
            "Microsoft OAuth credentials require AES encryption. Set ENABLE_ENCRYPTION=true and "
            "configure ENCRYPTOR_AES_SECRET_KEY/SALT/IV on the deployment."
        )


def _require_client_credentials() -> tuple[str, str]:
    client_id = settings.MICROSOFT_OAUTH_CLIENT_ID
    client_secret = settings.MICROSOFT_OAUTH_CLIENT_SECRET
    if not client_id or not client_secret:
        raise ValueError("Microsoft OAuth client credentials are not configured")
    return client_id, client_secret


def _coerce_scopes(scopes: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if scopes is None:
        return list(OUTLOOK_MAIL_SCOPES)
    if isinstance(scopes, str):
        parts = [p for p in scopes.replace(",", " ").split() if p]
    else:
        parts = [p for p in (str(s).strip() for s in scopes) if p]
    return parts or list(OUTLOOK_MAIL_SCOPES)


def scopes_for_profile(scope_profile: str | None) -> list[str]:
    if scope_profile is None:
        scope_profile = MICROSOFT_OAUTH_SCOPE_PROFILE_OUTLOOK_MAIL
    if scope_profile not in MICROSOFT_OAUTH_SCOPE_PROFILES:
        raise UnsupportedScopeProfileError(f"Unsupported Microsoft OAuth scope profile: {scope_profile}")
    return list(MICROSOFT_OAUTH_SCOPE_PROFILES[scope_profile])


def _scope_segment(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def has_required_scopes(
    granted_scopes: list[str] | tuple[str, ...] | None,
    required_scopes: list[str] | tuple[str, ...],
) -> bool:
    granted = {_scope_segment(scope) for scope in (granted_scopes or [])}
    return all(_scope_segment(scope) in granted for scope in required_scopes)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _validate_redirect_uri(redirect_uri: str) -> None:
    allowlist = settings.MICROSOFT_OAUTH_REDIRECT_HOSTS
    if not allowlist:
        if settings.MICROSOFT_OAUTH_CLIENT_ID:
            raise InvalidRedirectURIError(
                "MICROSOFT_OAUTH_REDIRECT_HOSTS must be configured when MICROSOFT_OAUTH_CLIENT_ID is set"
            )
        return
    try:
        parsed = urlparse(redirect_uri)
        scheme = parsed.scheme
        host = parsed.hostname or ""
    except Exception as exc:
        raise InvalidRedirectURIError(f"Invalid redirect_uri: {exc}") from exc
    if host not in {h.strip().lower() for h in allowlist if h and h.strip()}:
        raise InvalidRedirectURIError(f"redirect_uri host not allowed: {host}")
    if scheme != "https" and host not in _LOOPBACK_HOSTS:
        raise InvalidRedirectURIError(f"redirect_uri must use https for non-loopback host: {host}")


def _validate_app_origin(app_origin: str) -> None:
    allowlist = settings.MICROSOFT_OAUTH_APP_ORIGINS
    if not allowlist:
        raise InvalidAppOriginError("MICROSOFT_OAUTH_APP_ORIGINS is not configured; app_origin is not accepted")

    try:
        parsed = urlparse(app_origin)
        scheme = parsed.scheme
        netloc = parsed.netloc
        host = parsed.hostname or ""
    except Exception as exc:
        raise InvalidAppOriginError(f"Invalid app_origin URL: {exc}") from exc

    if not scheme or not netloc:
        raise InvalidAppOriginError(f"app_origin must include scheme and hostname: {app_origin!r}")

    canonical = f"{scheme}://{netloc}"

    for entry in allowlist:
        stripped = entry.strip()
        if not stripped:
            continue
        if stripped.startswith("*."):
            if scheme != "https":
                continue
            suffix = stripped[1:].lower()
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return
        elif canonical == stripped:
            return

    raise InvalidAppOriginError(f"app_origin not allowed: {app_origin!r}")


def _code_challenge_for_verifier(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_authorize_url(
    redirect_uri: str,
    state: str,
    scopes: str | list[str] | tuple[str, ...] | None = None,
    code_verifier: str | None = None,
) -> tuple[str, str]:
    client_id, _client_secret = _require_client_credentials()
    _validate_redirect_uri(redirect_uri)

    scope_list = _coerce_scopes(scopes)
    if code_verifier is None:
        code_verifier = secrets.token_urlsafe(64)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scope_list),
        "state": state,
        "code_challenge": _code_challenge_for_verifier(code_verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{_authorize_endpoint()}?{urlencode(params)}", code_verifier


async def start_authorization(
    organization_id: str,
    redirect_uri: str,
    credential_name: str = "Default",
    scopes_requested: str | list[str] | tuple[str, ...] | None = None,
    scope_profile: str | None = None,
    app_origin: str | None = None,
) -> MicrosoftAuthorizationStart:
    _require_encryption()
    _validate_redirect_uri(redirect_uri)
    if app_origin is not None:
        _validate_app_origin(app_origin)

    credential_id = generate_microsoft_oauth_credential_id()
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    requested_scopes = (
        scopes_for_profile(scope_profile) if scopes_requested is None else _coerce_scopes(scopes_requested)
    )

    await app.DATABASE.microsoft_oauth.insert_pending_credential(
        credential_id=credential_id,
        organization_id=organization_id,
        credential_name=credential_name,
        scopes_requested=requested_scopes,
        consent_nonce=nonce,
        consent_redirect_uri=redirect_uri,
        consent_expires_at=now + datetime.timedelta(seconds=CONSENT_TTL_SECONDS),
        consent_app_origin=app_origin,
        consent_code_verifier=code_verifier,
    )

    authorize_url, _ = build_authorize_url(
        redirect_uri=redirect_uri,
        state=nonce,
        scopes=requested_scopes,
        code_verifier=code_verifier,
    )

    return MicrosoftAuthorizationStart(authorize_url=authorize_url, state=nonce)


async def promote_pending_credential(
    organization_id: str,
    nonce: str,
    refresh_token: str,
    scopes_granted: str | list[str] | tuple[str, ...] | None,
) -> MicrosoftOAuthCredentialBase:
    _require_encryption()
    encrypted_token = await encryptor.encrypt(refresh_token, EncryptMethod.AES)
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return await app.DATABASE.microsoft_oauth.promote_pending_to_active(
        organization_id=organization_id,
        nonce=nonce,
        encrypted_refresh_token=encrypted_token,
        encrypted_method=EncryptMethod.AES,
        scopes_granted=_coerce_scopes(scopes_granted),
        now=now,
    )


async def load_pending_consent_context(organization_id: str, nonce: str) -> PendingConsentContext | None:
    return await app.DATABASE.microsoft_oauth.load_pending_by_nonce(
        organization_id=organization_id,
        nonce=nonce,
    )


async def _post_token_form(data: dict[str, str]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                _token_endpoint(),
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise MicrosoftOAuthError(f"Microsoft token endpoint failed: {exc}") from exc

    if response.status_code >= 400:
        error = None
        try:
            payload = response.json()
            error = payload.get("error")
        except ValueError:
            payload = {}
        if response.status_code in (400, 401) and error == "invalid_grant":
            raise MicrosoftOAuthError(
                "Microsoft authorization expired or was revoked. Reconnect the Microsoft account."
            )
        detail = payload.get("error_description") or response.text[:200]
        raise MicrosoftOAuthError(f"Microsoft token endpoint returned HTTP {response.status_code}: {detail}")

    try:
        return response.json()
    except ValueError as exc:
        raise MicrosoftOAuthError("Microsoft token endpoint returned invalid JSON") from exc


async def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
    scopes: str | list[str] | tuple[str, ...] | None = None,
) -> dict:
    client_id, client_secret = _require_client_credentials()
    if not code_verifier:
        raise MicrosoftOAuthError("OAuth consent row is missing the PKCE verifier")

    token_data = await _post_token_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": " ".join(_coerce_scopes(scopes)),
        }
    )
    if not token_data.get("refresh_token"):
        raise MicrosoftOAuthError("Microsoft token response did not include refresh_token")
    if not token_data.get("access_token"):
        raise MissingAccessTokenError("Microsoft token response did not include access_token")
    return token_data


async def refresh_access_token(refresh_token: str, scopes: str | list[str] | tuple[str, ...] | None = None) -> dict:
    client_id, client_secret = _require_client_credentials()
    refresh_scopes = _coerce_scopes(scopes)
    if not has_required_scopes(refresh_scopes, ["offline_access"]):
        refresh_scopes.append("offline_access")
    token_data = await _post_token_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(refresh_scopes),
        }
    )
    if not token_data.get("access_token"):
        raise MissingAccessTokenError("Microsoft token response did not include access_token")
    return token_data


async def load_credential_secrets(
    organization_id: str,
    credential_id: str,
) -> MicrosoftCredentialSecrets:
    payload = await app.DATABASE.microsoft_oauth.load_active_ciphertext(
        organization_id=organization_id,
        credential_id=credential_id,
    )
    if payload is None:
        raise ValueError(f"No active Microsoft OAuth credential found: {credential_id}")
    refresh_token = await encryptor.decrypt(payload.encrypted_refresh_token, payload.encrypted_method)
    return MicrosoftCredentialSecrets(refresh_token=refresh_token, scopes=payload.scopes_granted)


async def access_token_from_secrets(credential_secrets: MicrosoftCredentialSecrets) -> str:
    if not credential_secrets.refresh_token:
        raise ValueError("OAuth credential is missing refresh_token")
    token_data = await refresh_access_token(credential_secrets.refresh_token)
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MissingAccessTokenError("Microsoft token response did not include access_token")
    return access_token


async def refresh_and_rotate(
    *,
    organization_id: str,
    credential_id: str,
    credential_secrets: MicrosoftCredentialSecrets,
) -> str:
    if not credential_secrets.refresh_token:
        raise ValueError("OAuth credential is missing refresh_token")
    token_data = await refresh_access_token(credential_secrets.refresh_token, credential_secrets.scopes)
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise MissingAccessTokenError("Microsoft token response did not include access_token")

    new_refresh_token = token_data.get("refresh_token")
    if (
        isinstance(new_refresh_token, str)
        and new_refresh_token
        and new_refresh_token != credential_secrets.refresh_token
    ):
        encrypted_refresh_token = await encryptor.encrypt(new_refresh_token, EncryptMethod.AES)
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        await app.DATABASE.microsoft_oauth.update_active_refresh_token(
            organization_id=organization_id,
            credential_id=credential_id,
            encrypted_refresh_token=encrypted_refresh_token,
            encrypted_method=EncryptMethod.AES,
            now=now,
        )
    return access_token


async def get_credentials_for_org(organization_id: str) -> list[MicrosoftOAuthCredentialBase]:
    return await app.DATABASE.microsoft_oauth.list_active_for_org(organization_id=organization_id)


async def rename_credential(
    organization_id: str,
    credential_id: str,
    credential_name: str,
) -> MicrosoftOAuthCredentialBase | None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return await app.DATABASE.microsoft_oauth.rename_active(
        organization_id=organization_id,
        credential_id=credential_id,
        credential_name=credential_name,
        now=now,
    )


async def revoke_credential(organization_id: str, credential_id: str) -> bool:
    payload = await app.DATABASE.microsoft_oauth.load_ciphertext_for_revoke(
        organization_id=organization_id,
        credential_id=credential_id,
    )
    if not payload.exists:
        return False

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    revoked_id = await app.DATABASE.microsoft_oauth.mark_revoked_and_scrub(
        organization_id=organization_id,
        credential_id=credential_id,
        now=now,
    )

    if revoked_id is not None:
        try:
            cache_key = microsoft_access_token_cache_key(organization_id, credential_id)
            await app.CACHE.set(cache_key, "", ex=datetime.timedelta(seconds=30))
        except Exception:
            LOG.warning(
                "Failed to invalidate Microsoft access-token cache on revoke",
                credential_id=credential_id,
                exc_info=True,
            )
        LOG.info(
            "Revoked Microsoft OAuth credential",
            credential_id=credential_id,
            organization_id=organization_id,
        )
        return True
    return False
