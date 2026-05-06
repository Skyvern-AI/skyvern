from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.sql.elements import BindParameter

from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.google_oauth import UpdateGoogleOAuthCredentialRequest
from skyvern.forge.sdk.services import google_oauth_service


def _unwrap_bind(value: Any) -> Any:
    """SQLAlchemy wraps literal values passed to .values(...) in BindParameter; unwrap for equality checks."""
    return value.value if isinstance(value, BindParameter) else value


def _default_scopes_list() -> list[str]:
    return list(google_oauth_service.GOOGLE_SHEETS_SCOPES)


def test_coerce_scopes_accepts_strings_and_iterables() -> None:
    assert google_oauth_service._coerce_scopes("https://a/scope https://b/scope") == [
        "https://a/scope",
        "https://b/scope",
    ]
    assert google_oauth_service._coerce_scopes("https://a/scope, https://b/scope") == [
        "https://a/scope",
        "https://b/scope",
    ]
    assert google_oauth_service._coerce_scopes(["https://a", " https://b "]) == ["https://a", "https://b"]
    assert google_oauth_service._coerce_scopes(None) == _default_scopes_list()
    assert google_oauth_service._coerce_scopes("") == _default_scopes_list()


def test_google_sheets_scopes_includes_drive_file_and_metadata_readonly() -> None:
    scopes = google_oauth_service.GOOGLE_SHEETS_SCOPES
    assert "https://www.googleapis.com/auth/spreadsheets" in scopes
    assert "https://www.googleapis.com/auth/drive.file" in scopes
    assert "https://www.googleapis.com/auth/drive.metadata.readonly" in scopes


def test_sheets_api_runtime_defaults_match_previous_hardcoded_values() -> None:
    """Sheets timeout/retry settings default to known values so unset envs
    produce no behavior change for upgrading deployments."""
    from skyvern.config import Settings

    fresh = Settings()
    assert fresh.GOOGLE_SHEETS_API_TIMEOUT_SECONDS == 30.0
    assert fresh.GOOGLE_SHEETS_API_MAX_RETRIES == 3


def test_build_authorize_url_includes_required_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app"], raising=False)

    url, code_verifier = google_oauth_service.build_authorize_url(
        redirect_uri="https://app/settings/google/callback",
        state="abc123",
    )

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == google_oauth_service.GOOGLE_AUTHORIZE_ENDPOINT
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == "cid"
    assert params["redirect_uri"] == "https://app/settings/google/callback"
    assert params["scope"] == " ".join(google_oauth_service.GOOGLE_SHEETS_SCOPES)
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["state"] == "abc123"
    # PKCE: a code_challenge must be on the URL and the verifier returned for replay.
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"]
    assert code_verifier and len(code_verifier) >= 43


def test_build_authorize_url_passes_autogenerate_code_verifier_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``autogenerate_code_verifier=True`` so a library default flip can't silently drop PKCE."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["x"], raising=False)

    captured: dict = {}

    class _FakeFlow:
        code_verifier = "ver-fake"

        def authorization_url(self, **_kwargs):
            return "https://accounts.google.com/o/oauth2/v2/auth", "state"

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            captured["kwargs"] = kwargs
            return cls()

    monkeypatch.setattr(google_oauth_service, "Flow", _FakeFlow)

    google_oauth_service.build_authorize_url(redirect_uri="https://x/cb", state="s")

    assert captured["kwargs"].get("autogenerate_code_verifier") is True


def test_build_authorize_url_without_client_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        google_oauth_service.build_authorize_url(redirect_uri="https://x", state="s")


def test_build_authorize_url_without_client_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", None, raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        google_oauth_service.build_authorize_url(redirect_uri="https://x", state="s")


def test_build_authorize_url_rejects_redirect_uri_not_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: build_authorize_url must self-validate redirect_uri so direct callers
    (outside start_authorization) cannot bypass the host allowlist."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service.build_authorize_url(
            redirect_uri="https://evil.example.com/callback",
            state="abc",
        )


def test_build_authorize_url_rejects_http_for_non_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostname-only allowlist plus an http URI must be rejected even when called directly."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    with pytest.raises(google_oauth_service.InvalidRedirectURIError, match="https"):
        google_oauth_service.build_authorize_url(
            redirect_uri="http://app.skyvern.com/callback",
            state="abc",
        )


@pytest.mark.asyncio
async def test_start_authorization_persists_verifier_and_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["x"], raising=False)
    monkeypatch.setattr(google_oauth_service, "generate_google_oauth_credential_id", lambda: "goac_test")

    insert_mock = AsyncMock(return_value=SimpleNamespace(id="goac_test", organization_id="org_1"))
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.start_authorization(
        organization_id="org_1",
        redirect_uri="https://x/cb",
        credential_name="my-cred",
    )

    assert result.authorize_url.startswith(google_oauth_service.GOOGLE_AUTHORIZE_ENDPOINT)
    assert result.state
    insert_mock.assert_awaited_once()
    insert_kwargs = insert_mock.await_args.kwargs
    assert insert_kwargs["organization_id"] == "org_1"
    assert insert_kwargs["credential_name"] == "my-cred"
    assert insert_kwargs["consent_redirect_uri"] == "https://x/cb"
    assert insert_kwargs["consent_nonce"] == result.state
    # The verifier must be in the same insert as everything else — no second
    # round-trip — so a crash mid-flow can't leave a verifier-less pending row.
    assert insert_kwargs["consent_code_verifier"]
    assert len(insert_kwargs["consent_code_verifier"]) >= 43


@pytest.mark.asyncio
async def test_start_authorization_refuses_without_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", False, raising=False)
    fake_repo = SimpleNamespace(insert_pending_credential=AsyncMock())
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    with pytest.raises(google_oauth_service.EncryptionNotConfiguredError):
        await google_oauth_service.start_authorization(
            organization_id="org_1",
            redirect_uri="https://x/cb",
        )
    fake_repo.insert_pending_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_pending_credential_encrypts_and_calls_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    encrypt_mock = AsyncMock(return_value="ENC::rt")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(encrypt=encrypt_mock))

    promoted_schema = SimpleNamespace(id="goac_1", organization_id="org_1")
    promote_mock = AsyncMock(return_value=promoted_schema)
    fake_repo = SimpleNamespace(promote_pending_to_active=promote_mock)
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.promote_pending_credential(
        organization_id="org_1",
        nonce="nonce-xyz",
        refresh_token="rt-plain",
        scopes_granted="https://a https://b",
    )

    assert result is promoted_schema
    encrypt_mock.assert_awaited_once_with("rt-plain", EncryptMethod.AES)
    promote_mock.assert_awaited_once()
    kwargs = promote_mock.await_args.kwargs
    assert kwargs["organization_id"] == "org_1"
    assert kwargs["nonce"] == "nonce-xyz"
    assert kwargs["encrypted_refresh_token"] == "ENC::rt"
    assert kwargs["encrypted_method"] == EncryptMethod.AES
    assert kwargs["scopes_granted"] == ["https://a", "https://b"]


@pytest.mark.asyncio
async def test_load_pending_consent_context_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import PendingConsentContext

    expected = PendingConsentContext(
        credential_id="goac_1",
        consent_redirect_uri="https://x/cb",
        consent_code_verifier="ver-abc",
    )
    fake_repo = SimpleNamespace(load_pending_by_nonce=AsyncMock(return_value=expected))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.load_pending_consent_context(
        organization_id="org_1",
        nonce="nonce-xyz",
    )
    assert result is expected
    fake_repo.load_pending_by_nonce.assert_awaited_once_with(organization_id="org_1", nonce="nonce-xyz")


class _FakeOAuth2Session:
    def __init__(self, token: dict | None = None) -> None:
        self.token = token or {}


class _FakeFlow:
    """Mirrors the real google-auth-oauthlib Flow surface exchange_code_for_tokens touches."""

    def __init__(self, credentials: Any, session_token: dict | None, captured: dict) -> None:
        self.credentials = credentials
        self.oauth2session = _FakeOAuth2Session(token=session_token)
        self._captured = captured

    def fetch_token(self, code: str, code_verifier: str | None = None) -> None:
        self._captured["code"] = code
        self._captured["fetch_token_code_verifier"] = code_verifier


def _install_fake_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    credentials: Any,
    session_token: dict | None,
) -> dict:
    captured: dict = {}

    class _FlowFactory:
        @classmethod
        def from_client_config(
            cls, client_config: dict, scopes=None, redirect_uri=None, state=None, code_verifier=None
        ) -> _FakeFlow:
            captured["client_config"] = client_config
            captured["scopes"] = scopes
            captured["redirect_uri"] = redirect_uri
            captured["init_code_verifier"] = code_verifier
            return _FakeFlow(credentials=credentials, session_token=session_token, captured=captured)

    monkeypatch.setattr(google_oauth_service, "Flow", _FlowFactory)
    return captured


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_uses_granted_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real google-auth leaves Credentials.scopes=None when Flow is built with scopes=None;
    the granted scope lives on Credentials.granted_scopes (passed through from the token response)."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at-from-flow",
        refresh_token="rt-from-flow",
        scopes=None,
        granted_scopes="https://a/scope https://b/scope",
        expiry=None,
    )
    captured = _install_fake_flow(
        monkeypatch,
        credentials=creds,
        session_token={"scope": "https://a/scope https://b/scope", "access_token": "at-from-flow"},
    )

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="ver-xyz"
    )

    assert result == {
        "access_token": "at-from-flow",
        "refresh_token": "rt-from-flow",
        "scope": "https://a/scope https://b/scope",
        "expiry": None,
    }
    assert captured["code"] == "abc"
    assert captured["redirect_uri"] == "https://x/cb"
    assert captured["client_config"]["web"]["client_id"] == "cid"
    assert captured["client_config"]["web"]["client_secret"] == "secret"
    # ``Flow.from_client_config`` ignores ``code_verifier`` — PKCE actually replays
    # via ``fetch_token``, so only the fetch-time verifier matters.
    assert captured["fetch_token_code_verifier"] == "ver-xyz"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_accepts_granted_scopes_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Future-proof: if upstream starts handing back granted_scopes as a list, we must still serialize it."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at",
        refresh_token="rt",
        scopes=None,
        granted_scopes=["https://a/scope", "https://b/scope"],
        expiry=None,
    )
    _install_fake_flow(monkeypatch, credentials=creds, session_token={"scope": "", "access_token": "at"})

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="v"
    )
    assert result["scope"] == "https://a/scope https://b/scope"


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_falls_back_to_session_token_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: if a library variant leaves granted_scopes empty, read the raw token response."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    creds = SimpleNamespace(
        token="at",
        refresh_token="rt",
        scopes=None,
        granted_scopes=None,
        expiry=None,
    )
    _install_fake_flow(
        monkeypatch,
        credentials=creds,
        session_token={"scope": "https://a/scope", "access_token": "at"},
    )

    result = await google_oauth_service.exchange_code_for_tokens(
        code="abc", redirect_uri="https://x/cb", code_verifier="v"
    )
    assert result["scope"] == "https://a/scope"


@pytest.mark.asyncio
async def test_exchange_code_raises_without_client_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", None, raising=False)

    with pytest.raises(ValueError, match="client credentials"):
        await google_oauth_service.exchange_code_for_tokens("code", "https://x/cb", code_verifier="v")


@pytest.mark.asyncio
async def test_revoke_credential_loads_decrypts_revokes_upstream_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import RevocableCiphertext

    load_mock = AsyncMock(
        return_value=RevocableCiphertext(
            exists=True,
            encrypted_refresh_token="ENC::token",
            encrypted_method=EncryptMethod.AES,
        )
    )
    mark_mock = AsyncMock(return_value="goac_1")
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            google_oauth=SimpleNamespace(
                load_ciphertext_for_revoke=load_mock,
                mark_revoked_and_scrub=mark_mock,
            )
        ),
        raising=False,
    )

    decrypt_mock = AsyncMock(return_value="refresh-123")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(decrypt=decrypt_mock))
    upstream_mock = AsyncMock()
    monkeypatch.setattr(google_oauth_service, "_revoke_refresh_token_at_google", upstream_mock)

    fake_cache = SimpleNamespace(set=AsyncMock())
    monkeypatch.setattr(google_oauth_service.app, "CACHE", fake_cache, raising=False)

    revoked = await google_oauth_service.revoke_credential(organization_id="org_1", credential_id="goac_1")
    assert revoked is True
    decrypt_mock.assert_awaited_once_with("ENC::token", EncryptMethod.AES)
    upstream_mock.assert_awaited_once_with("refresh-123")
    mark_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_credential_missing_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import RevocableCiphertext

    load_mock = AsyncMock(return_value=RevocableCiphertext(exists=False))
    upstream_mock = AsyncMock()
    mark_mock = AsyncMock()
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(
            google_oauth=SimpleNamespace(
                load_ciphertext_for_revoke=load_mock,
                mark_revoked_and_scrub=mark_mock,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(google_oauth_service, "_revoke_refresh_token_at_google", upstream_mock)

    revoked = await google_oauth_service.revoke_credential(organization_id="o", credential_id="c")
    assert revoked is False
    upstream_mock.assert_not_awaited()
    mark_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_google_endpoint_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google_oauth_service.httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=transport),
    )

    await google_oauth_service._revoke_refresh_token_at_google("rt")


@pytest.mark.asyncio
async def test_load_credential_secrets_decrypts_repo_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories.google_oauth import ActiveCredentialCiphertext

    payload = ActiveCredentialCiphertext(
        encrypted_refresh_token="ENC::rt",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://a", "https://b"],
    )
    fake_repo = SimpleNamespace(load_active_ciphertext=AsyncMock(return_value=payload))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    decrypt_mock = AsyncMock(return_value="refresh-123")
    monkeypatch.setattr(google_oauth_service, "encryptor", SimpleNamespace(decrypt=decrypt_mock))

    secrets = await google_oauth_service.load_credential_secrets(
        organization_id="org_1",
        credential_id="goac_1",
    )

    assert secrets.refresh_token == "refresh-123"
    assert secrets.scopes == ["https://a", "https://b"]
    decrypt_mock.assert_awaited_once_with("ENC::rt", EncryptMethod.AES)


@pytest.mark.asyncio
async def test_load_credential_secrets_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_repo = SimpleNamespace(load_active_ciphertext=AsyncMock(return_value=None))
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    with pytest.raises(ValueError, match="No active Google OAuth credential"):
        await google_oauth_service.load_credential_secrets(
            organization_id="org_1",
            credential_id="goac_missing",
        )


@pytest.mark.asyncio
async def test_get_credentials_for_org_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = [SimpleNamespace(id="goac_1"), SimpleNamespace(id="goac_2")]
    list_mock = AsyncMock(return_value=creds)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(list_active_for_org=list_mock)),
        raising=False,
    )

    result = await google_oauth_service.get_credentials_for_org(organization_id="org_1")
    assert result is creds
    list_mock.assert_awaited_once_with(organization_id="org_1")


@pytest.mark.asyncio
async def test_access_token_from_secrets_calls_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_mock = AsyncMock(return_value={"access_token": "at-456"})
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-1",
        scopes=["https://a"],
    )

    token = await google_oauth_service.access_token_from_secrets(secrets)

    assert token == "at-456"
    refresh_mock.assert_awaited_once_with("rt-1")


@pytest.mark.asyncio
async def test_access_token_from_secrets_missing_access_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_mock = AsyncMock(return_value={"scope": "foo"})
    monkeypatch.setattr(google_oauth_service, "refresh_access_token", refresh_mock)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt", scopes=[])

    with pytest.raises(google_oauth_service.MissingAccessTokenError):
        await google_oauth_service.access_token_from_secrets(secrets)


def test_validate_redirect_uri_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    google_oauth_service._validate_redirect_uri("https://app.skyvern.com/google/callback")

    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service._validate_redirect_uri("https://evil.example.com/callback")


def test_validate_redirect_uri_empty_allowlist_dev_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty allowlist + no client_id = dev fallback; keeps local dev ergonomic."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", [], raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", None, raising=False)
    google_oauth_service._validate_redirect_uri("https://anywhere.example.com/cb")


def test_validate_redirect_uri_empty_allowlist_raises_when_client_id_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty allowlist + client_id configured = misconfigured prod; must fail closed."""
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", [], raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    with pytest.raises(google_oauth_service.InvalidRedirectURIError):
        google_oauth_service._validate_redirect_uri("https://app.skyvern.com/cb")


def test_validate_redirect_uri_rejects_http_for_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostname-only allowlist plus an http URI is the bypass claude[bot] flagged:
    an attacker on http://allowed-host.com:9999 should not satisfy a check intended
    for https://allowed-host.com."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidRedirectURIError, match="https"):
        google_oauth_service._validate_redirect_uri("http://app.skyvern.com/cb")


def test_validate_redirect_uri_allows_http_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local dev needs http://localhost; loopback hosts are exempted from the https requirement."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["localhost", "127.0.0.1"],
        raising=False,
    )
    google_oauth_service._validate_redirect_uri("http://localhost:5173/cb")
    google_oauth_service._validate_redirect_uri("http://127.0.0.1:8080/cb")


def test_validate_redirect_uri_normalizes_allowlist_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """``urlparse().hostname`` lowercases the URI host; the allowlist is lowercased
    too so an operator who configures mixed-case entries doesn't reject every redirect."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_REDIRECT_HOSTS",
        ["MyApp.Example.Com"],
        raising=False,
    )
    google_oauth_service._validate_redirect_uri("https://myapp.example.com/cb")
    google_oauth_service._validate_redirect_uri("https://MYAPP.EXAMPLE.COM/cb")


@pytest.mark.asyncio
async def test_refresh_access_token_uses_credentials_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kwargs) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None
            self.expiry = None

        def refresh(self, request) -> None:
            captured["refresh_request_type"] = type(request).__name__
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    result = await google_oauth_service.refresh_access_token("rt-1")

    assert result == {"access_token": "at-refreshed", "expiry": None}
    assert captured["init_kwargs"]["refresh_token"] == "rt-1"
    assert captured["init_kwargs"]["client_id"] == "cid"
    assert captured["init_kwargs"]["token_uri"] == google_oauth_service.GOOGLE_TOKEN_ENDPOINT
    assert captured["refresh_request_type"] == "Request"


@pytest.mark.asyncio
async def test_refresh_access_token_wraps_google_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    class _FakeCreds:
        def __init__(self, **_kwargs) -> None:
            self.token = None
            self.expiry = None

        def refresh(self, _request) -> None:
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    with pytest.raises(google_oauth_service.MissingAccessTokenError, match="refresh failed"):
        await google_oauth_service.refresh_access_token("rt-bad")


@pytest.mark.asyncio
async def test_credentials_from_secrets_wraps_google_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.auth.exceptions import RefreshError

    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    class _FakeCreds:
        def __init__(self, **_kwargs) -> None:
            self.token = None

        def refresh(self, _request) -> None:
            raise RefreshError("token revoked")

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(refresh_token="rt-1", scopes=["https://a"])
    with pytest.raises(google_oauth_service.MissingAccessTokenError, match="refresh failed"):
        await google_oauth_service.credentials_from_secrets(secrets)


@pytest.mark.asyncio
async def test_credentials_from_secrets_returns_refreshed_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "secret", raising=False)

    captured: dict = {}

    class _FakeCreds:
        def __init__(self, **kwargs) -> None:
            captured["init_kwargs"] = kwargs
            self.token: str | None = None

        def refresh(self, request) -> None:
            self.token = "at-refreshed"

    monkeypatch.setattr(google_oauth_service, "Credentials", _FakeCreds)

    secrets = google_oauth_service.GoogleCredentialSecrets(
        refresh_token="rt-decoded",
        scopes=["https://a", "https://b"],
    )
    creds = await google_oauth_service.credentials_from_secrets(secrets)

    assert creds.token == "at-refreshed"
    assert captured["init_kwargs"]["refresh_token"] == "rt-decoded"
    assert captured["init_kwargs"]["scopes"] == ["https://a", "https://b"]


def test_update_google_oauth_credential_request_strips_whitespace() -> None:
    request = UpdateGoogleOAuthCredentialRequest(credential_name="  Personal Gmail  ")
    assert request.credential_name == "Personal Gmail"


def test_update_google_oauth_credential_request_rejects_blank() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="   ")


def test_update_google_oauth_credential_request_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="")


def test_update_google_oauth_credential_request_enforces_max_length() -> None:
    with pytest.raises(ValidationError):
        UpdateGoogleOAuthCredentialRequest(credential_name="x" * 129)


@pytest.mark.asyncio
async def test_rename_credential_delegates_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    renamed = SimpleNamespace(id="goac_1", credential_name="Personal Gmail")
    rename_mock = AsyncMock(return_value=renamed)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(rename_active=rename_mock)),
        raising=False,
    )

    result = await google_oauth_service.rename_credential(
        organization_id="org_1",
        credential_id="goac_1",
        credential_name="Personal Gmail",
    )
    assert result is renamed
    rename_mock.assert_awaited_once()
    kwargs = rename_mock.await_args.kwargs
    assert kwargs["organization_id"] == "org_1"
    assert kwargs["credential_id"] == "goac_1"
    assert kwargs["credential_name"] == "Personal Gmail"


@pytest.mark.asyncio
async def test_rename_credential_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    rename_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        google_oauth_service.app,
        "DATABASE",
        SimpleNamespace(google_oauth=SimpleNamespace(rename_active=rename_mock)),
        raising=False,
    )
    result = await google_oauth_service.rename_credential(
        organization_id="org_1",
        credential_id="goac_missing",
        credential_name="Anything",
    )
    assert result is None


def test_require_scopes_from_token_returns_scope_when_present() -> None:
    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    assert oauth_route._require_scopes_from_token({"scope": "https://a https://b"}) == ["https://a", "https://b"]


def test_require_scopes_from_token_raises_on_missing_scope() -> None:
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException) as excinfo:
        oauth_route._require_scopes_from_token({})
    assert excinfo.value.status_code == 400
    assert "scope" in excinfo.value.detail.lower()


def test_require_scopes_from_token_raises_on_empty_scope_string() -> None:
    """Google returns an empty scope string on partial consent; must fail closed, not default."""
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException) as excinfo:
        oauth_route._require_scopes_from_token({"scope": ""})
    assert excinfo.value.status_code == 400
    assert "scope" in excinfo.value.detail.lower()


def test_require_scopes_from_token_raises_on_whitespace_only_scope() -> None:
    from fastapi import HTTPException

    from skyvern.forge.sdk.routes import google_oauth as oauth_route

    with pytest.raises(HTTPException):
        oauth_route._require_scopes_from_token({"scope": "   "})


# ---------------------------------------------------------------------------
# _validate_app_origin tests
# ---------------------------------------------------------------------------


def test_validate_app_origin_exact_match_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["https://app.skyvern.com"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://app.skyvern.com")


def test_validate_app_origin_exact_match_http_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["http://localhost:5173"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("http://localhost:5173")


def test_validate_app_origin_rejects_non_matching_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["https://app.skyvern.com"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError, match="not allowed"):
        google_oauth_service._validate_app_origin("https://evil.example.com")


def test_validate_app_origin_suffix_wildcard_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://skyvern-cloud-git-main-skyvern.vercel.app")


def test_validate_app_origin_suffix_wildcard_matches_with_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suffix matching strips the port — ``*.vercel.app`` accepts ``myapp.vercel.app:3000``."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    google_oauth_service._validate_app_origin("https://myapp.vercel.app:3000")


def test_validate_app_origin_suffix_wildcard_rejects_bare_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'vercel.app' itself must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://vercel.app")


def test_validate_app_origin_suffix_wildcard_rejects_spoof_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'attacker-vercel.app' must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://attacker-vercel.app")


def test_validate_app_origin_suffix_wildcard_rejects_spoof_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """'vercel.app.evil.com' must not match '*.vercel.app'."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("https://vercel.app.evil.com")


def test_validate_app_origin_wildcard_requires_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wildcard entries only match https, not http."""
    monkeypatch.setattr(
        google_oauth_service.settings,
        "GOOGLE_OAUTH_APP_ORIGINS",
        ["*.vercel.app"],
        raising=False,
    )
    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        google_oauth_service._validate_app_origin("http://skyvern-cloud-git-main-skyvern.vercel.app")


def test_validate_app_origin_empty_allowlist_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", [], raising=False)
    with pytest.raises(google_oauth_service.InvalidAppOriginError, match="not configured"):
        google_oauth_service._validate_app_origin("https://app.skyvern.com")


@pytest.mark.asyncio
async def test_start_authorization_persists_app_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app-staging.skyvern.com"], raising=False
    )
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", ["*.vercel.app"], raising=False)
    monkeypatch.setattr(google_oauth_service, "generate_google_oauth_credential_id", lambda: "goac_test2")

    insert_mock = AsyncMock(return_value=SimpleNamespace(id="goac_test2", organization_id="org_1"))
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    result = await google_oauth_service.start_authorization(
        organization_id="org_1",
        redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        credential_name="Test",
        app_origin="https://skyvern-cloud-git-branch-skyvern.vercel.app",
    )

    assert result.state
    insert_mock.assert_awaited_once()
    insert_kwargs = insert_mock.await_args.kwargs
    assert insert_kwargs["consent_app_origin"] == "https://skyvern-cloud-git-branch-skyvern.vercel.app"


@pytest.mark.asyncio
async def test_start_authorization_rejects_bad_app_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["app-staging.skyvern.com"], raising=False
    )
    monkeypatch.setattr(
        google_oauth_service.settings, "GOOGLE_OAUTH_APP_ORIGINS", ["https://app.skyvern.com"], raising=False
    )

    insert_mock = AsyncMock()
    fake_repo = SimpleNamespace(insert_pending_credential=insert_mock)
    monkeypatch.setattr(google_oauth_service.app, "DATABASE", SimpleNamespace(google_oauth=fake_repo), raising=False)

    with pytest.raises(google_oauth_service.InvalidAppOriginError):
        await google_oauth_service.start_authorization(
            organization_id="org_1",
            redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
            app_origin="https://evil.example.com",
        )
    insert_mock.assert_not_awaited()


def test_google_oauth_credential_response_exposes_app_origin() -> None:
    import datetime

    from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase, GoogleOAuthCredentialResponse

    cred = GoogleOAuthCredentialBase(
        id="goac_1",
        organization_id="o_1",
        credential_name="Default",
        provider="google",
        state="active",
        scopes_requested=[],
        scopes_granted=[],
        created_at=datetime.datetime.utcnow(),
        modified_at=datetime.datetime.utcnow(),
    )
    resp = GoogleOAuthCredentialResponse(credential=cred, app_origin="https://foo.vercel.app")
    assert resp.app_origin == "https://foo.vercel.app"

    resp_no_origin = GoogleOAuthCredentialResponse(credential=cred)
    assert resp_no_origin.app_origin is None
