"""Open source Google OAuth 2.0 connector for Skyvern.

This module implements the OAuth flow Skyvern uses to obtain a Google refresh
token on behalf of an organization, store it encrypted, and mint short-lived
access tokens for downstream API calls (Sheets, Drive, etc.). It is the
backend-agnostic implementation; the cloud deployment layers an in-process
access-token cache on top via ``CloudAgentFunction.get_google_sheets_credentials``.
"""

from __future__ import annotations

import asyncio
import datetime
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import structlog
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.id import generate_google_oauth_credential_id

# Lifecycle constants and InvalidConsentNonceError live on the repository (the DB owns
# them); re-export from this module so the cloud shim and existing call sites keep
# importing through ``google_oauth_service`` unchanged.
from skyvern.forge.sdk.db.repositories.google_oauth import (  # noqa: F401
    STATE_ACTIVE,
    STATE_ERROR,
    STATE_PENDING_CONSENT,
    STATE_REVOKED,
    InvalidConsentNonceError,
    PendingConsentContext,
)
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.google_oauth import (
    GoogleOAuthClientConfig,
    GoogleOAuthClientConfigSafe,
    GoogleOAuthCredentialBase,
)
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# Spreadsheets + Drive scopes for the picker UX:
# - spreadsheets: read/write sheet cells and create new spreadsheets
# - drive.file: list/read spreadsheets this integration created or the user picked
# - drive.metadata.readonly: list file metadata (required for the picker search)
GOOGLE_SHEETS_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)
GOOGLE_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_GMAIL_SCOPES: tuple[str, ...] = (GOOGLE_GMAIL_READONLY_SCOPE,)
# Drive uploads accept pasted folder IDs/URLs. drive.file cannot write to
# arbitrary existing folders unless the app created or Picker-selected them, so
# this profile intentionally uses full Drive scope until a Picker flow exists.
GOOGLE_DRIVE_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive",)
GOOGLE_OAUTH_SCOPE_PROFILE_SHEETS = "google_sheets"
GOOGLE_OAUTH_SCOPE_PROFILE_GMAIL = "gmail"
GOOGLE_OAUTH_SCOPE_PROFILE_DRIVE = "google_drive"
GOOGLE_OAUTH_SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    GOOGLE_OAUTH_SCOPE_PROFILE_SHEETS: GOOGLE_SHEETS_SCOPES,
    GOOGLE_OAUTH_SCOPE_PROFILE_GMAIL: GOOGLE_GMAIL_SCOPES,
    GOOGLE_OAUTH_SCOPE_PROFILE_DRIVE: GOOGLE_DRIVE_SCOPES,
}

CONSENT_TTL_SECONDS = 600


def google_access_token_cache_key(organization_id: str, credential_id: str) -> str:
    return f"google:access_token:{organization_id}:{credential_id}"


class EncryptionNotConfiguredError(RuntimeError):
    """Raised when Google credentials cannot be stored because AES encryption is disabled."""


class MissingAccessTokenError(RuntimeError):
    """Raised when Google returns a 2xx token response without an access_token."""


class InvalidRedirectURIError(ValueError):
    """Raised when the supplied OAuth redirect_uri host is not on the allowlist."""


class InvalidAppOriginError(ValueError):
    """Raised when the supplied app_origin is not on the GOOGLE_OAUTH_APP_ORIGINS allowlist."""


class UnsupportedScopeProfileError(ValueError):
    """Raised when a caller asks for an unsupported Google OAuth scope profile."""


class OrganizationGoogleOAuthConfigDisabledError(RuntimeError):
    """Raised when org-level Google OAuth client config is disabled for this deployment."""


class OrganizationClientConfigUnavailableError(RuntimeError):
    """Raised when a stored org OAuth client config exists (or cannot be ruled out) but cannot be loaded."""


@dataclass(frozen=True)
class GoogleCredentialSecrets:
    """Decrypted credential material needed to mint an access token."""

    refresh_token: str
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GoogleOAuthClientConfigResolution:
    config: GoogleOAuthClientConfig | None
    source: str

    def safe(self) -> GoogleOAuthClientConfigSafe:
        return GoogleOAuthClientConfigSafe(
            client_id=self.config.client_id if self.config else None,
            redirect_hosts=list(self.config.redirect_hosts) if self.config else [],
            app_origins=list(self.config.app_origins) if self.config else [],
            client_secret_configured=bool(self.config and self.config.client_secret),
            configured=self.config is not None,
            source=self.source,
            encryption_enabled=settings.ENABLE_ENCRYPTION,
        )


def _require_encryption() -> None:
    """Google credentials must be encrypted at rest; refuse to write them in plaintext."""
    if not settings.ENABLE_ENCRYPTION:
        raise EncryptionNotConfiguredError(
            "Google OAuth credentials require AES encryption. Set ENABLE_ENCRYPTION=true and "
            "configure ENCRYPTOR_AES_SECRET_KEY/SALT/IV on the deployment."
        )


def _settings_client_config() -> GoogleOAuthClientConfig | None:
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        return None
    return GoogleOAuthClientConfig(
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        redirect_hosts=list(settings.GOOGLE_OAUTH_REDIRECT_HOSTS),
        app_origins=list(settings.GOOGLE_OAUTH_APP_ORIGINS),
    )


def _organization_client_config_enabled() -> bool:
    return SettingsManager.get_settings().ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG


def _require_organization_client_config_enabled() -> None:
    if not _organization_client_config_enabled():
        raise OrganizationGoogleOAuthConfigDisabledError("Organization Google OAuth client config is disabled")


async def resolve_client_config(
    organization_id: str | None = None,
    *,
    strict: bool = True,
) -> GoogleOAuthClientConfigResolution:
    if organization_id and _organization_client_config_enabled():
        try:
            token = await app.DATABASE.organizations.get_valid_org_auth_token(
                organization_id=organization_id,
                token_type=OrganizationAuthTokenType.google_oauth_client_config.value,
            )
        except Exception:
            if strict:
                LOG.warning("Failed to load organization Google OAuth client config", organization_id=organization_id)
                raise OrganizationClientConfigUnavailableError(
                    "Failed to load the organization Google OAuth client config"
                ) from None
            LOG.warning(
                "Failed to load organization Google OAuth client config; falling back to environment for token refresh",
                organization_id=organization_id,
            )
            token = None
        if token is not None:
            if not token.token:
                LOG.warning(
                    "Stored organization Google OAuth client config is empty",
                    organization_id=organization_id,
                )
                raise OrganizationClientConfigUnavailableError(
                    "Stored organization Google OAuth client config is invalid"
                )
            try:
                return GoogleOAuthClientConfigResolution(
                    config=GoogleOAuthClientConfig.model_validate_json(token.token),
                    source="organization",
                )
            except Exception:
                LOG.warning(
                    "Stored organization Google OAuth client config is invalid",
                    organization_id=organization_id,
                )
                raise OrganizationClientConfigUnavailableError(
                    "Stored organization Google OAuth client config is invalid"
                ) from None

    env_config = _settings_client_config()
    return GoogleOAuthClientConfigResolution(config=env_config, source="environment" if env_config else "missing")


async def save_client_config(
    organization_id: str,
    config: GoogleOAuthClientConfig,
) -> GoogleOAuthClientConfigResolution:
    _require_organization_client_config_enabled()
    _require_encryption()
    token_payload = config.model_dump_json()
    await app.DATABASE.organizations.replace_org_auth_token(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.google_oauth_client_config,
        token=token_payload,
        encrypted_method=EncryptMethod.AES,
    )
    return GoogleOAuthClientConfigResolution(config=config, source="organization")


async def delete_client_config(organization_id: str) -> None:
    _require_organization_client_config_enabled()
    await app.DATABASE.organizations.invalidate_org_auth_tokens(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.google_oauth_client_config,
    )


def _require_client_credentials(client_config: GoogleOAuthClientConfig | None = None) -> tuple[str, str]:
    config = client_config or _settings_client_config()
    client_id = config.client_id if config else None
    client_secret = config.client_secret if config else None
    if not client_id or not client_secret:
        raise ValueError("Google OAuth client credentials are not configured")
    return client_id, client_secret


def _coerce_scopes(scopes: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Accept space- or comma-delimited scope strings, iterables, or None and return a clean list."""
    if scopes is None:
        return list(GOOGLE_SHEETS_SCOPES)
    if isinstance(scopes, str):
        parts = [p for p in scopes.replace(",", " ").split() if p]
    else:
        parts = [p for p in (str(s).strip() for s in scopes) if p]
    return parts or list(GOOGLE_SHEETS_SCOPES)


def scopes_for_profile(scope_profile: str | None) -> list[str]:
    if scope_profile is None:
        scope_profile = GOOGLE_OAUTH_SCOPE_PROFILE_SHEETS
    if scope_profile not in GOOGLE_OAUTH_SCOPE_PROFILES:
        raise UnsupportedScopeProfileError(f"Unsupported Google OAuth scope profile: {scope_profile}")
    return list(GOOGLE_OAUTH_SCOPE_PROFILES[scope_profile])


def has_required_scopes(
    granted_scopes: list[str] | tuple[str, ...] | None,
    required_scopes: list[str] | tuple[str, ...],
) -> bool:
    granted = set(granted_scopes or [])
    return all(scope in granted for scope in required_scopes)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _validate_redirect_uri(redirect_uri: str, client_config: GoogleOAuthClientConfig | None = None) -> None:
    """Validate redirect_uri against the allowlist; defense-in-depth alongside Google's check.

    Beyond the host allowlist, we enforce scheme=https for non-loopback hosts so
    an attacker who controls ``http://allowed-host.com`` can't satisfy a
    hostname-only allowlist intended for ``https://allowed-host.com``.
    """
    allowlist = client_config.redirect_hosts if client_config else settings.GOOGLE_OAUTH_REDIRECT_HOSTS
    if not allowlist:
        # Empty allowlist + no client_id = OAuth not configured at all → silent
        # pass-through (the consent flow itself will fail later in
        # ``_require_client_credentials``). Empty allowlist + client_id = misconfig
        # we must catch up-front so we don't leak a redirect_uri to Google unchecked.
        configured_client_id = client_config.client_id if client_config else settings.GOOGLE_OAUTH_CLIENT_ID
        if configured_client_id:
            raise InvalidRedirectURIError(
                "GOOGLE_OAUTH_REDIRECT_HOSTS must be configured when GOOGLE_OAUTH_CLIENT_ID is set"
            )
        return
    try:
        parsed = urlparse(redirect_uri)
        scheme = parsed.scheme
        # ``parsed.hostname`` is already lowercased; lowercase the allowlist too so an
        # operator who configures ``MyApp.Example.Com`` doesn't reject every redirect.
        host = parsed.hostname or ""
    except Exception as exc:
        raise InvalidRedirectURIError(f"Invalid redirect_uri: {exc}")
    if host not in {h.strip().lower() for h in allowlist if h and h.strip()}:
        raise InvalidRedirectURIError(f"redirect_uri host not allowed: {host}")
    if scheme != "https" and host not in _LOOPBACK_HOSTS:
        raise InvalidRedirectURIError(f"redirect_uri must use https for non-loopback host: {host}")


def _validate_app_origin(app_origin: str, client_config: GoogleOAuthClientConfig | None = None) -> None:
    """Validate app_origin against the GOOGLE_OAUTH_APP_ORIGINS allowlist.

    Entries are either:
    - Exact-match: full origin string like ``https://app.skyvern.com`` or ``http://localhost:5173``.
      The port (if any) must match exactly — ``https://app.skyvern.com`` does NOT match
      ``https://app.skyvern.com:8443``.
    - Suffix wildcard: ``*.foo.com`` — matches any hostname ending with ``.foo.com`` over https only.
      Wildcards intentionally ignore the port (preview deploys often run on non-default ports);
      ``*.vercel.app`` accepts ``app.vercel.app:3000``. Rejects bare-suffix spoofs
      (``attacker-foo.com``, ``foo.com.evil.com``).

    Fails closed: empty allowlist always raises.
    """
    allowlist = client_config.app_origins if client_config else settings.GOOGLE_OAUTH_APP_ORIGINS
    if not allowlist:
        raise InvalidAppOriginError("GOOGLE_OAUTH_APP_ORIGINS is not configured; app_origin is not accepted")

    try:
        parsed = urlparse(app_origin)
        scheme = parsed.scheme
        netloc = parsed.netloc
        host = parsed.hostname or ""
    except Exception as exc:
        raise InvalidAppOriginError(f"Invalid app_origin URL: {exc}")

    if not scheme or not netloc:
        raise InvalidAppOriginError(f"app_origin must include scheme and hostname: {app_origin!r}")

    canonical = f"{scheme}://{netloc}"

    for entry in allowlist:
        stripped = entry.strip()
        if not stripped:
            continue
        if stripped.startswith("*."):
            # Wildcard entry: only match over https
            if scheme != "https":
                continue
            suffix = stripped[1:].lower()  # e.g. ".vercel.app"
            # Match against ``host`` (hostname only, no port) so wildcards like
            # ``*.vercel.app`` accept ``myapp.vercel.app:3000``. Hostname must end
            # with the suffix AND not be the suffix itself (no bare-suffix match).
            if host.endswith(suffix) and host != suffix.lstrip("."):
                return
        else:
            # Exact match
            if canonical == stripped:
                return

    raise InvalidAppOriginError(f"app_origin not allowed: {app_origin!r}")


def build_authorize_url(
    redirect_uri: str,
    state: str,
    scopes: str | list[str] | tuple[str, ...] | None = None,
    code_verifier: str | None = None,
    client_config: GoogleOAuthClientConfig | None = None,
) -> tuple[str, str]:
    """Assemble the Google OAuth 2.0 consent URL the browser should navigate to.

    Self-validates ``redirect_uri`` against the host allowlist (defense-in-depth)
    so direct callers cannot bypass the check. ``start_authorization`` also
    validates up-front to avoid leaking a pending DB row on rejection.

    ``code_verifier`` may be supplied so callers that want the verifier
    persisted to the DB before the URL exists can do so atomically; if None,
    the Flow autogenerates one and we return it.
    """
    # Require both halves up-front; otherwise consent completes but /callback fails every time
    # because exchange_code_for_tokens needs the secret for the code -> token roundtrip.
    client_id, client_secret = _require_client_credentials(client_config)
    _validate_redirect_uri(redirect_uri, client_config)

    scope_list = _coerce_scopes(scopes)
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTHORIZE_ENDPOINT,
                "token_uri": GOOGLE_TOKEN_ENDPOINT,
            }
        },
        scopes=scope_list,
        redirect_uri=redirect_uri,
        state=state,
        autogenerate_code_verifier=code_verifier is None,
    )
    if code_verifier is not None:
        flow.code_verifier = code_verifier
    url, _returned_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    # Flow.code_verifier is None when autogenerate is False AND we didn't set
    # it; that path is unreachable here, but raise (not assert) so a future
    # refactor can't silently leak a null verifier into the DB under -O.
    if not flow.code_verifier:
        raise RuntimeError("Flow did not produce a PKCE code_verifier")
    return url, flow.code_verifier


@dataclass(frozen=True)
class GoogleAuthorizationStart:
    authorize_url: str
    state: str


async def start_authorization(
    organization_id: str,
    redirect_uri: str,
    credential_name: str = "Default",
    scopes_requested: str | list[str] | tuple[str, ...] | None = None,
    scope_profile: str | None = None,
    app_origin: str | None = None,
) -> GoogleAuthorizationStart:
    """Insert a pending consent row, build the authorize URL, persist the PKCE verifier."""
    _require_encryption()
    resolved_config = await resolve_client_config(organization_id)
    if resolved_config.config is None:
        raise ValueError("Google OAuth client credentials are not configured")
    _validate_redirect_uri(redirect_uri, resolved_config.config)
    if app_origin is not None:
        _validate_app_origin(app_origin, resolved_config.config)

    credential_id = generate_google_oauth_credential_id()
    nonce = secrets.token_urlsafe(32)
    # Pre-generate the PKCE verifier so the pending row lands with the verifier
    # populated in a single DB write — a crash mid-flow can no longer leave a
    # pending row that the callback would see as missing the verifier.
    code_verifier = secrets.token_urlsafe(64)
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    requested_scopes = (
        scopes_for_profile(scope_profile) if scopes_requested is None else _coerce_scopes(scopes_requested)
    )

    await app.DATABASE.google_oauth.insert_pending_credential(
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
        client_config=resolved_config.config,
    )

    return GoogleAuthorizationStart(authorize_url=authorize_url, state=nonce)


async def promote_pending_credential(
    organization_id: str,
    nonce: str,
    refresh_token: str,
    scopes_granted: str | list[str] | tuple[str, ...] | None,
) -> GoogleOAuthCredentialBase:
    """Encrypt the refresh token and promote the matching pending row to active."""
    _require_encryption()
    encrypted_token = await encryptor.encrypt(refresh_token, EncryptMethod.AES)
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return await app.DATABASE.google_oauth.promote_pending_to_active(
        organization_id=organization_id,
        nonce=nonce,
        encrypted_refresh_token=encrypted_token,
        encrypted_method=EncryptMethod.AES,
        scopes_granted=_coerce_scopes(scopes_granted),
        now=now,
    )


async def load_pending_consent_context(organization_id: str, nonce: str) -> PendingConsentContext | None:
    """Fetch the redirect_uri + PKCE verifier the callback needs to replay to Google."""
    return await app.DATABASE.google_oauth.load_pending_by_nonce(
        organization_id=organization_id,
        nonce=nonce,
    )


async def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
    organization_id: str | None = None,
) -> dict:
    """Exchange an OAuth authorization code for access and refresh tokens."""
    resolved_config = await resolve_client_config(organization_id)
    client_id, client_secret = _require_client_credentials(resolved_config.config)

    # ``code_verifier`` is passed to ``fetch_token`` (where the PKCE verification
    # actually happens); ``Flow.from_client_config`` ignores the kwarg.
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTHORIZE_ENDPOINT,
                "token_uri": GOOGLE_TOKEN_ENDPOINT,
            }
        },
        scopes=None,
        redirect_uri=redirect_uri,
    )
    await asyncio.to_thread(flow.fetch_token, code=code, code_verifier=code_verifier)
    creds = flow.credentials
    # creds.scopes reflects what we *requested* on this Flow (None here, since we pass scopes=None at callback);
    # Google's actually-granted scopes live on creds.granted_scopes / flow.oauth2session.token["scope"].
    granted = creds.granted_scopes or flow.oauth2session.token.get("scope") or ""
    if isinstance(granted, (list, tuple)):
        granted = " ".join(granted)
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scope": granted,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


async def refresh_access_token(refresh_token: str, organization_id: str | None = None) -> dict:
    """Exchange a refresh token for a new access token via google-auth."""
    resolved_config = await resolve_client_config(organization_id, strict=False)
    client_id, client_secret = _require_client_credentials(resolved_config.config)

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
    )
    try:
        await asyncio.to_thread(creds.refresh, GoogleAuthRequest())
    except GoogleAuthError as exc:
        raise MissingAccessTokenError(f"Google token refresh failed: {exc}") from exc
    return {
        "access_token": creds.token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


async def load_credential_secrets(
    organization_id: str,
    credential_id: str,
) -> GoogleCredentialSecrets:
    """Fetch + decrypt an active credential's refresh token."""
    payload = await app.DATABASE.google_oauth.load_active_ciphertext(
        organization_id=organization_id,
        credential_id=credential_id,
    )
    if payload is None:
        raise ValueError(f"No active Google OAuth credential found: {credential_id}")
    refresh_token = await encryptor.decrypt(payload.encrypted_refresh_token, payload.encrypted_method)
    return GoogleCredentialSecrets(refresh_token=refresh_token, scopes=payload.scopes_granted)


async def access_token_from_secrets(
    credential_secrets: GoogleCredentialSecrets,
    organization_id: str | None = None,
) -> str:
    """Exchange loaded secrets for an access token. Network-only; no DB."""
    if not credential_secrets.refresh_token:
        raise ValueError("OAuth credential is missing refresh_token")
    if organization_id is None:
        token_data = await refresh_access_token(credential_secrets.refresh_token)
    else:
        token_data = await refresh_access_token(credential_secrets.refresh_token, organization_id=organization_id)
    access_token = token_data.get("access_token")
    if not access_token:
        raise MissingAccessTokenError("Google token response did not include access_token")
    return access_token


async def credentials_from_secrets(
    credential_secrets: GoogleCredentialSecrets,
    organization_id: str | None = None,
) -> Credentials:
    """Build a refreshed ``Credentials`` from already-decrypted secrets. Network-only; no DB."""
    resolved_config = await resolve_client_config(organization_id, strict=False)
    client_id, client_secret = _require_client_credentials(resolved_config.config)
    creds = Credentials(
        token=None,
        refresh_token=credential_secrets.refresh_token,
        token_uri=GOOGLE_TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
        scopes=credential_secrets.scopes,
    )
    try:
        await asyncio.to_thread(creds.refresh, GoogleAuthRequest())
    except GoogleAuthError as exc:
        raise MissingAccessTokenError(f"Google token refresh failed: {exc}") from exc
    if not creds.token:
        raise MissingAccessTokenError("Google token response did not include access_token")
    return creds


async def get_credentials_for_org(organization_id: str) -> list[GoogleOAuthCredentialBase]:
    """List all active Google OAuth credentials for an organization (metadata only)."""
    return await app.DATABASE.google_oauth.list_active_for_org(organization_id=organization_id)


# google-auth 2.x has no first-class async revoke helper; keep the 10-line httpx POST.
async def _revoke_refresh_token_at_google(refresh_token: str) -> None:
    """Best-effort call to Google's revoke endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                GOOGLE_REVOKE_ENDPOINT,
                data={"token": refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        if response.status_code >= 400:
            LOG.warning(
                "Google token revocation returned non-success",
                status_code=response.status_code,
                body=response.text[:200],
            )
    except Exception as exc:
        LOG.warning("Failed to revoke Google refresh token upstream", error=str(exc))


async def rename_credential(
    organization_id: str,
    credential_id: str,
    credential_name: str,
) -> GoogleOAuthCredentialBase | None:
    """Rename an active Google OAuth credential. Returns None for not-found / wrong-state / wrong-org."""
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return await app.DATABASE.google_oauth.rename_active(
        organization_id=organization_id,
        credential_id=credential_id,
        credential_name=credential_name,
        now=now,
    )


async def revoke_credential(organization_id: str, credential_id: str) -> bool:
    """Revoke upstream at Google, mark local row revoked, invalidate access-token cache."""
    payload = await app.DATABASE.google_oauth.load_ciphertext_for_revoke(
        organization_id=organization_id,
        credential_id=credential_id,
    )
    if not payload.exists:
        return False

    refresh_token: str | None = None
    if payload.encrypted_refresh_token and payload.encrypted_method:
        try:
            refresh_token = await encryptor.decrypt(payload.encrypted_refresh_token, payload.encrypted_method)
        except Exception as exc:
            LOG.warning(
                "Failed to decrypt refresh token for upstream revocation",
                credential_id=credential_id,
                error=str(exc),
            )
    if refresh_token:
        await _revoke_refresh_token_at_google(refresh_token)

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    revoked_id = await app.DATABASE.google_oauth.mark_revoked_and_scrub(
        organization_id=organization_id,
        credential_id=credential_id,
        now=now,
    )

    if revoked_id is not None:
        try:
            # 30s tombstone outlives any in-flight token refresh that beat the revoke commit.
            cache_key = google_access_token_cache_key(organization_id, credential_id)
            await app.CACHE.set(cache_key, "", ex=datetime.timedelta(seconds=30))
        except Exception:
            LOG.warning(
                "Failed to invalidate access-token cache on revoke",
                credential_id=credential_id,
                exc_info=True,
            )
        LOG.info(
            "Revoked Google OAuth credential",
            credential_id=credential_id,
            organization_id=organization_id,
        )
        return True
    return False
