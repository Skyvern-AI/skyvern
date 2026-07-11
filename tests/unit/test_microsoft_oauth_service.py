import datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.db.repositories.microsoft_oauth import PendingConsentContext
from skyvern.forge.sdk.routes import microsoft_oauth as microsoft_oauth_routes
from skyvern.forge.sdk.schemas.microsoft_oauth import CreateMicrosoftOAuthCallbackRequest
from skyvern.forge.sdk.services import microsoft_oauth_service


def _install_microsoft_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(microsoft_oauth_service.httpx, "AsyncClient", fake_async_client)


def test_build_authorize_url_includes_required_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_TENANT", "common", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_REDIRECT_HOSTS", ["app"], raising=False)

    url, code_verifier = microsoft_oauth_service.build_authorize_url(
        redirect_uri="https://app/settings/microsoft/callback",
        state="abc123",
    )

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert params["client_id"] == "cid"
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "https://app/settings/microsoft/callback"
    assert params["response_mode"] == "query"
    assert params["scope"] == " ".join(microsoft_oauth_service.OUTLOOK_MAIL_SCOPES)
    assert "offline_access" in params["scope"].split()
    assert params["state"] == "abc123"
    assert params["code_challenge_method"] == "S256"
    assert params["prompt"] == "select_account"
    assert params["code_challenge"]
    assert code_verifier and len(code_verifier) >= 43


def test_pkce_challenge_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_REDIRECT_HOSTS", ["app"], raising=False)

    verifier = "fixed-verifier"
    url, returned_verifier = microsoft_oauth_service.build_authorize_url(
        redirect_uri="https://app/cb",
        state="state",
        code_verifier=verifier,
    )

    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert returned_verifier == verifier
    assert params["code_challenge"] == microsoft_oauth_service._code_challenge_for_verifier(verifier)
    assert params["code_challenge"] == "7MosA1dS6hiqNcSny0SqUWJbJo82pR0lNczg5YZ-GLI"


def test_scopes_for_profile() -> None:
    assert microsoft_oauth_service.scopes_for_profile(None) == list(microsoft_oauth_service.OUTLOOK_MAIL_SCOPES)
    assert microsoft_oauth_service.scopes_for_profile("outlook_mail") == list(
        microsoft_oauth_service.OUTLOOK_MAIL_SCOPES
    )
    with pytest.raises(microsoft_oauth_service.UnsupportedScopeProfileError):
        microsoft_oauth_service.scopes_for_profile("calendar")


def test_has_required_scopes_matches_trailing_segments() -> None:
    granted = ["https://graph.microsoft.com/Mail.Read", "https://graph.microsoft.com/User.Read"]
    assert microsoft_oauth_service.has_required_scopes(granted, ["Mail.Read"])
    assert microsoft_oauth_service.has_required_scopes(granted, ["https://graph.microsoft.com/Mail.Read"])
    assert microsoft_oauth_service.has_required_scopes(["Mail.Read"], ["https://graph.microsoft.com/Mail.Read"])
    assert not microsoft_oauth_service.has_required_scopes(["Mail.Read"], ["Mail.Send"])


def test_validate_redirect_uri_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        microsoft_oauth_service.settings,
        "MICROSOFT_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    microsoft_oauth_service._validate_redirect_uri("https://app.skyvern.com/microsoft/callback")

    with pytest.raises(microsoft_oauth_service.InvalidRedirectURIError):
        microsoft_oauth_service._validate_redirect_uri("https://evil.example.com/callback")


def test_validate_redirect_uri_rejects_http_for_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        microsoft_oauth_service.settings,
        "MICROSOFT_OAUTH_REDIRECT_HOSTS",
        ["app.skyvern.com"],
        raising=False,
    )

    with pytest.raises(microsoft_oauth_service.InvalidRedirectURIError, match="https"):
        microsoft_oauth_service._validate_redirect_uri("http://app.skyvern.com/callback")


def test_validate_app_origin_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        microsoft_oauth_service.settings,
        "MICROSOFT_OAUTH_APP_ORIGINS",
        ["https://app.skyvern.com", "*.vercel.app"],
        raising=False,
    )

    microsoft_oauth_service._validate_app_origin("https://app.skyvern.com")
    microsoft_oauth_service._validate_app_origin("https://preview.vercel.app:3000")

    with pytest.raises(microsoft_oauth_service.InvalidAppOriginError):
        microsoft_oauth_service._validate_app_origin("https://vercel.app")
    with pytest.raises(microsoft_oauth_service.InvalidAppOriginError):
        microsoft_oauth_service._validate_app_origin("https://evil.example.com")


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_posts_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_TENANT", "tenant-1", raising=False)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": "Mail.Read User.Read",
                "id_token": "id",
            },
        )

    _install_microsoft_transport(monkeypatch, handler)

    result = await microsoft_oauth_service.exchange_code_for_tokens(
        code="code-1",
        redirect_uri="https://app/cb",
        code_verifier="verifier-1",
    )

    assert result["access_token"] == "at"
    assert result["refresh_token"] == "rt"
    assert captured["url"] == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    assert captured["content_type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(captured["body"]).items()}
    assert form["client_id"] == "cid"
    assert form["client_secret"] == "secret"
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-1"
    assert form["redirect_uri"] == "https://app/cb"
    assert form["code_verifier"] == "verifier-1"
    assert form["scope"] == " ".join(microsoft_oauth_service.OUTLOOK_MAIL_SCOPES)


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_requires_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "scope": "Mail.Read"})

    _install_microsoft_transport(monkeypatch, handler)

    with pytest.raises(microsoft_oauth_service.MicrosoftOAuthError, match="refresh_token"):
        await microsoft_oauth_service.exchange_code_for_tokens(
            code="code-1",
            redirect_uri="https://app/cb",
            code_verifier="verifier-1",
        )


@pytest.mark.asyncio
async def test_refresh_access_token_posts_form_and_ignores_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at-refreshed",
                "refresh_token": "rt-rotated",
                "expires_in": 3600,
                "scope": "Mail.Read",
            },
        )

    _install_microsoft_transport(monkeypatch, handler)

    result = await microsoft_oauth_service.refresh_access_token("rt-1", scopes=["Mail.Read"])

    assert result["access_token"] == "at-refreshed"
    assert result["refresh_token"] == "rt-rotated"
    form = {k: v[0] for k, v in parse_qs(captured["body"]).items()}
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-1"
    assert form["scope"].split() == ["Mail.Read", "offline_access"]


@pytest.mark.asyncio
async def test_refresh_and_rotate_persists_rotated_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "at-refreshed",
                "refresh_token": "rt-rotated",
                "expires_in": 3600,
                "scope": "Mail.Read",
            },
        )

    _install_microsoft_transport(monkeypatch, handler)
    update_active_refresh_token = AsyncMock()
    repository = SimpleNamespace(update_active_refresh_token=update_active_refresh_token)
    monkeypatch.setattr(microsoft_oauth_service.app, "DATABASE", SimpleNamespace(microsoft_oauth=repository))
    encrypt = AsyncMock(return_value="encrypted-rt-rotated")
    monkeypatch.setattr(microsoft_oauth_service, "encryptor", SimpleNamespace(encrypt=encrypt))

    access_token = await microsoft_oauth_service.refresh_and_rotate(
        organization_id="org-1",
        credential_id="cred-1",
        credential_secrets=microsoft_oauth_service.MicrosoftCredentialSecrets(
            refresh_token="rt-original",
            scopes=["Mail.Read"],
        ),
    )

    assert access_token == "at-refreshed"
    encrypt.assert_awaited_once_with("rt-rotated", microsoft_oauth_service.EncryptMethod.AES)
    update_active_refresh_token.assert_awaited_once()
    kwargs = update_active_refresh_token.await_args.kwargs
    assert kwargs["organization_id"] == "org-1"
    assert kwargs["credential_id"] == "cred-1"
    assert kwargs["encrypted_refresh_token"] == "encrypted-rt-rotated"
    assert kwargs["encrypted_method"] == microsoft_oauth_service.EncryptMethod.AES
    assert isinstance(kwargs["now"], datetime.datetime)


@pytest.mark.asyncio
async def test_refresh_and_rotate_propagates_rotated_token_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        microsoft_oauth_service,
        "refresh_access_token",
        AsyncMock(return_value={"access_token": "at-refreshed", "refresh_token": "rt-rotated"}),
    )
    repository = SimpleNamespace(
        update_active_refresh_token=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(microsoft_oauth_service.app, "DATABASE", SimpleNamespace(microsoft_oauth=repository))
    monkeypatch.setattr(
        microsoft_oauth_service,
        "encryptor",
        SimpleNamespace(encrypt=AsyncMock(return_value="encrypted-rt-rotated")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await microsoft_oauth_service.refresh_and_rotate(
            organization_id="org-1",
            credential_id="cred-1",
            credential_secrets=microsoft_oauth_service.MicrosoftCredentialSecrets(
                refresh_token="rt-original",
                scopes=["Mail.Read"],
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_token", ["rt-original", None])
async def test_refresh_and_rotate_skips_persist_without_new_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
    refresh_token: str | None,
) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)
    response_json: dict[str, str | int] = {
        "access_token": "at-refreshed",
        "expires_in": 3600,
        "scope": "Mail.Read",
    }
    if refresh_token is not None:
        response_json["refresh_token"] = refresh_token

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_json)

    _install_microsoft_transport(monkeypatch, handler)
    update_active_refresh_token = AsyncMock()
    repository = SimpleNamespace(update_active_refresh_token=update_active_refresh_token)
    monkeypatch.setattr(microsoft_oauth_service.app, "DATABASE", SimpleNamespace(microsoft_oauth=repository))
    encrypt = AsyncMock(return_value="encrypted-rt-rotated")
    monkeypatch.setattr(microsoft_oauth_service, "encryptor", SimpleNamespace(encrypt=encrypt))

    access_token = await microsoft_oauth_service.refresh_and_rotate(
        organization_id="org-1",
        credential_id="cred-1",
        credential_secrets=microsoft_oauth_service.MicrosoftCredentialSecrets(
            refresh_token="rt-original",
            scopes=["Mail.Read"],
        ),
    )

    assert access_token == "at-refreshed"
    encrypt.assert_not_awaited()
    update_active_refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_grant_raises_reconnect_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(microsoft_oauth_service.settings, "MICROSOFT_OAUTH_CLIENT_SECRET", "secret", raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})

    _install_microsoft_transport(monkeypatch, handler)

    with pytest.raises(microsoft_oauth_service.MicrosoftOAuthError, match="Reconnect"):
        await microsoft_oauth_service.refresh_access_token("rt-1")


@pytest.mark.asyncio
async def test_callback_rejects_missing_mail_read_before_promoting(monkeypatch: pytest.MonkeyPatch) -> None:
    promote = AsyncMock()
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_pending_consent_context",
        AsyncMock(
            return_value=PendingConsentContext(
                credential_id="cred_1",
                consent_redirect_uri="https://app/cb",
                consent_code_verifier="verifier",
                consent_app_origin="https://app",
                scopes_requested=["Mail.Read", "offline_access"],
            )
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "exchange_code_for_tokens",
        AsyncMock(return_value={"refresh_token": "rt", "scope": "User.Read offline_access"}),
    )
    monkeypatch.setattr(microsoft_oauth_routes.microsoft_oauth_service, "promote_pending_credential", promote)

    with pytest.raises(HTTPException) as exc_info:
        await microsoft_oauth_routes.microsoft_oauth_callback(
            CreateMicrosoftOAuthCallbackRequest(code="code", state="state"),
            SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "Microsoft did not grant Mail.Read. Please re-connect and accept all requested permissions."
    )
    promote.assert_not_awaited()
