import asyncio
import base64
import datetime
import json
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.db.repositories.microsoft_oauth import PendingConsentContext
from skyvern.forge.sdk.routes import microsoft_oauth as microsoft_oauth_routes
from skyvern.forge.sdk.schemas.microsoft_oauth import (
    CreateMicrosoftOAuthCallbackRequest,
    MicrosoftOAuthCredentialBase,
)
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


def _unsigned_id_token(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


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


def test_email_from_id_token_prefers_email_claim() -> None:
    token = _unsigned_id_token(
        {
            "email": "mailbox@example.test",
            "preferred_username": "upn@example.test",
        }
    )

    assert microsoft_oauth_service.email_from_id_token(token) == "mailbox@example.test"


@pytest.mark.parametrize(
    ("preferred_username", "expected"),
    [
        ("upn@example.test", "upn@example.test"),
        ("tenant.onmicrosoft.com", None),
    ],
)
def test_email_from_id_token_requires_email_shaped_preferred_username(
    preferred_username: str,
    expected: str | None,
) -> None:
    token = _unsigned_id_token({"preferred_username": preferred_username})

    assert microsoft_oauth_service.email_from_id_token(token) == expected


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
@pytest.mark.parametrize(
    ("scopes", "expected_scopes"),
    [
        (["Mail.Read"], ["Mail.Read", "offline_access"]),
        (
            ["Mail.Read", "https://graph.microsoft.com/offline_access"],
            ["Mail.Read", "https://graph.microsoft.com/offline_access"],
        ),
    ],
)
async def test_refresh_access_token_posts_form_and_ignores_rotation(
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str],
    expected_scopes: list[str],
) -> None:
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

    result = await microsoft_oauth_service.refresh_access_token("rt-1", scopes=scopes)

    assert result["access_token"] == "at-refreshed"
    assert result["refresh_token"] == "rt-rotated"
    form = {k: v[0] for k, v in parse_qs(captured["body"]).items()}
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-1"
    assert form["scope"].split() == expected_scopes


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
    assert update_active_refresh_token.await_args is not None
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

    message = "Failed to persist rotated Microsoft refresh token; reconnect the Microsoft account"
    with pytest.raises(microsoft_oauth_service.MicrosoftOAuthError, match=message):
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


@pytest.mark.parametrize(
    ("graph_email", "claim_email", "expected_email"),
    [
        ("Mailbox@Example.Test", "upn@example.test", "mailbox@example.test"),
        (None, "Claim@Example.Test", "claim@example.test"),
    ],
)
@pytest.mark.asyncio
async def test_microsoft_oauth_callback_prefers_graph_mail_then_falls_back_to_claim(
    monkeypatch: pytest.MonkeyPatch,
    graph_email: str | None,
    claim_email: str,
    expected_email: str,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = MicrosoftOAuthCredentialBase(
        id="msoac_1",
        organization_id="org_1",
        credential_name="Default",
        state="active",
        scopes_requested=["Mail.Read"],
        scopes_granted=["Mail.Read"],
        email_address="old@example.test",
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_pending_consent_context",
        AsyncMock(
            return_value=PendingConsentContext(
                credential_id=credential.id,
                consent_redirect_uri="https://app/cb",
                consent_code_verifier="verifier",
                scopes_requested=["Mail.Read", "offline_access"],
            )
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "exchange_code_for_tokens",
        AsyncMock(
            return_value={
                "refresh_token": "refresh-token",
                "access_token": "access-token",
                "scope": "Mail.Read offline_access",
                "id_token": "id-token",
            }
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "promote_pending_credential",
        AsyncMock(return_value=credential),
    )
    email_from_id_token = MagicMock(return_value=claim_email)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "email_from_id_token",
        email_from_id_token,
    )
    fetch_primary_account_email = AsyncMock(return_value=graph_email)
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "fetch_primary_account_email",
        fetch_primary_account_email,
    )
    update_email_address = AsyncMock(return_value=True)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "update_email_address",
        update_email_address,
    )

    response = await microsoft_oauth_routes.microsoft_oauth_callback(
        CreateMicrosoftOAuthCallbackRequest(code="code", state="state"),
        SimpleNamespace(organization_id="org_1"),
    )

    assert response.credential.email_address == expected_email
    fetch_primary_account_email.assert_awaited_once_with(access_token="access-token")
    if graph_email is None:
        email_from_id_token.assert_called_once_with("id-token")
    else:
        email_from_id_token.assert_not_called()
    update_email_address.assert_awaited_once_with(
        organization_id="org_1",
        credential_id="msoac_1",
        email_address=expected_email,
        only_if_null=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 408, 429, 503])
async def test_microsoft_oauth_callback_preserves_email_on_transient_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = MicrosoftOAuthCredentialBase(
        id="msoac_1",
        organization_id="org_1",
        credential_name="Default",
        state="active",
        scopes_requested=["Mail.Read"],
        scopes_granted=["Mail.Read"],
        email_address="correct@example.test",
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_pending_consent_context",
        AsyncMock(
            return_value=PendingConsentContext(
                credential_id=credential.id,
                consent_redirect_uri="https://app/cb",
                consent_code_verifier="verifier",
                scopes_requested=["Mail.Read", "offline_access"],
            )
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "exchange_code_for_tokens",
        AsyncMock(
            return_value={
                "refresh_token": "refresh-token",
                "access_token": "access-token",
                "scope": "Mail.Read offline_access",
                "id_token": "id-token",
            }
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "promote_pending_credential",
        AsyncMock(return_value=credential),
    )
    email_from_id_token = MagicMock(return_value="upn@example.test")
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "email_from_id_token",
        email_from_id_token,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "fetch_primary_account_email",
        AsyncMock(
            side_effect=microsoft_oauth_routes.outlook.OutlookAPIError(
                status=status,
                code="upstream_unavailable",
                message="temporary failure",
            )
        ),
    )
    update_email_address = AsyncMock(return_value=True)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "update_email_address",
        update_email_address,
    )

    response = await microsoft_oauth_routes.microsoft_oauth_callback(
        CreateMicrosoftOAuthCallbackRequest(code="code", state="state"),
        SimpleNamespace(organization_id="org_1"),
    )

    assert response.credential.email_address == "correct@example.test"
    email_from_id_token.assert_not_called()
    update_email_address.assert_not_awaited()


@pytest.mark.asyncio
async def test_microsoft_oauth_callback_falls_back_to_claim_on_non_transient_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = MicrosoftOAuthCredentialBase(
        id="msoac_1",
        organization_id="org_1",
        credential_name="Default",
        state="active",
        scopes_requested=["Mail.Read"],
        scopes_granted=["Mail.Read"],
        email_address=None,
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_pending_consent_context",
        AsyncMock(
            return_value=PendingConsentContext(
                credential_id=credential.id,
                consent_redirect_uri="https://app/cb",
                consent_code_verifier="verifier",
                scopes_requested=["Mail.Read", "offline_access"],
            )
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "exchange_code_for_tokens",
        AsyncMock(
            return_value={
                "refresh_token": "refresh-token",
                "access_token": "access-token",
                "scope": "Mail.Read offline_access",
                "id_token": "id-token",
            }
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "promote_pending_credential",
        AsyncMock(return_value=credential),
    )
    email_from_id_token = MagicMock(return_value="Claim@Example.Test")
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "email_from_id_token",
        email_from_id_token,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "fetch_primary_account_email",
        AsyncMock(
            side_effect=microsoft_oauth_routes.outlook.OutlookAPIError(
                status=403,
                code="mailbox_not_enabled",
                message="mailbox unavailable",
            )
        ),
    )
    update_email_address = AsyncMock(return_value=True)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "update_email_address",
        update_email_address,
    )

    response = await microsoft_oauth_routes.microsoft_oauth_callback(
        CreateMicrosoftOAuthCallbackRequest(code="code", state="state"),
        SimpleNamespace(organization_id="org_1"),
    )

    assert response.credential.email_address == "claim@example.test"
    email_from_id_token.assert_called_once_with("id-token")
    update_email_address.assert_awaited_once_with(
        organization_id="org_1",
        credential_id="msoac_1",
        email_address="claim@example.test",
        only_if_null=False,
    )


@pytest.mark.asyncio
async def test_fetch_primary_account_email_raises_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection failed", request=request)

    monkeypatch.setattr(microsoft_oauth_routes.outlook.asyncio, "sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(microsoft_oauth_routes.outlook.OutlookAPIError) as exc_info:
            await microsoft_oauth_routes.outlook.fetch_primary_account_email(
                access_token="access-token",
                client=client,
            )

    assert exc_info.value.status == 503
    assert exc_info.value.code == "upstream_unavailable"
    assert attempts == 3


@pytest.mark.parametrize("status", [400, 429, 503])
@pytest.mark.asyncio
async def test_fetch_primary_account_email_raises_graph_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "_get_json",
        AsyncMock(
            side_effect=microsoft_oauth_routes.outlook.OutlookAPIError(
                status=status,
                code="temporary",
                message="temporary failure",
            )
        ),
    )

    with pytest.raises(microsoft_oauth_routes.outlook.OutlookAPIError):
        await microsoft_oauth_routes.outlook.fetch_primary_account_email(
            access_token="access-token",
            client=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_fetch_primary_account_email_does_not_fall_back_to_upn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_json = AsyncMock(
        return_value={
            "mail": None,
            "userPrincipalName": "upn@example.test",
        }
    )
    monkeypatch.setattr(microsoft_oauth_routes.outlook, "_get_json", get_json)

    result = await microsoft_oauth_routes.outlook.fetch_primary_account_email(
        access_token="access-token",
        client=AsyncMock(),
    )

    assert result is None
    assert get_json.await_args is not None
    assert get_json.await_args.kwargs["params"] == {"$select": "mail"}


@pytest.mark.asyncio
async def test_microsoft_email_backfill_randomizes_candidates_and_uses_null_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credentials = [
        MicrosoftOAuthCredentialBase(
            id=f"msoac_{index}",
            organization_id="org_1",
            credential_name="Default",
            state="active",
            scopes_requested=["Mail.Read"],
            scopes_granted=["Mail.Read"],
            created_at=now,
            modified_at=now,
        )
        for index in range(5)
    ]
    selected = [credentials[3], credentials[1], credentials[4]]
    sample = MagicMock(return_value=selected)
    monkeypatch.setattr(microsoft_oauth_routes, "_EMAIL_BACKFILL_FAILURES", {})
    monkeypatch.setattr(microsoft_oauth_routes.random, "sample", sample)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_credential_secrets",
        AsyncMock(side_effect=lambda **kwargs: kwargs["credential_id"]),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "refresh_and_rotate",
        AsyncMock(
            side_effect=lambda *, organization_id, credential_id, credential_secrets: f"token-{credential_secrets}"
        ),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "fetch_primary_account_email",
        AsyncMock(side_effect=lambda access_token: f"{access_token}@Example.Test"),
    )
    update_email_address = AsyncMock(side_effect=[True, False, True])
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "update_email_address",
        update_email_address,
    )

    await microsoft_oauth_routes._backfill_microsoft_email_addresses(
        organization_id="org_1",
        credentials=credentials,
    )

    sample.assert_called_once_with(credentials, k=3)
    assert [awaited.kwargs["credential_id"] for awaited in update_email_address.await_args_list] == [
        credential.id for credential in selected
    ]
    assert all(awaited.kwargs["only_if_null"] is True for awaited in update_email_address.await_args_list)
    assert selected[0].email_address == "token-msoac_3@example.test"
    assert selected[1].email_address is None
    assert selected[2].email_address == "token-msoac_4@example.test"
    assert microsoft_oauth_routes._EMAIL_BACKFILL_FAILURES == {}


@pytest.mark.asyncio
async def test_microsoft_email_backfill_does_not_retry_failed_resolution_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = MicrosoftOAuthCredentialBase(
        id="msoac_failed",
        organization_id="org_1",
        credential_name="Default",
        state="active",
        scopes_requested=["Mail.Read"],
        scopes_granted=["Mail.Read"],
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(microsoft_oauth_routes, "_EMAIL_BACKFILL_FAILURES", {})
    monotonic_time = [100.0]
    monkeypatch.setattr(microsoft_oauth_routes.time, "monotonic", lambda: monotonic_time[0])
    load_secrets = AsyncMock(return_value="secrets")
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_credential_secrets",
        load_secrets,
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "refresh_and_rotate",
        AsyncMock(return_value="access-token"),
    )
    monkeypatch.setattr(
        microsoft_oauth_routes.outlook,
        "fetch_primary_account_email",
        AsyncMock(return_value=None),
    )

    await microsoft_oauth_routes._backfill_microsoft_email_addresses(
        organization_id="org_1",
        credentials=[credential],
    )
    await microsoft_oauth_routes._backfill_microsoft_email_addresses(
        organization_id="org_1",
        credentials=[credential],
    )

    load_secrets.assert_awaited_once()

    monotonic_time[0] = 3701.0
    await microsoft_oauth_routes._backfill_microsoft_email_addresses(
        organization_id="org_1",
        credentials=[credential],
    )

    assert load_secrets.await_count == 2


@pytest.mark.asyncio
async def test_microsoft_email_backfill_caches_cancelled_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    credential = MicrosoftOAuthCredentialBase(
        id="msoac_slow",
        organization_id="org_1",
        credential_name="Default",
        state="active",
        scopes_requested=["Mail.Read"],
        scopes_granted=["Mail.Read"],
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(microsoft_oauth_routes, "_EMAIL_BACKFILL_FAILURES", {})
    load_secrets = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(
        microsoft_oauth_routes.microsoft_oauth_service,
        "load_credential_secrets",
        load_secrets,
    )

    with pytest.raises(asyncio.CancelledError):
        await microsoft_oauth_routes._backfill_microsoft_email_addresses(
            organization_id="org_1",
            credentials=[credential],
        )
    await microsoft_oauth_routes._backfill_microsoft_email_addresses(
        organization_id="org_1",
        credentials=[credential],
    )

    load_secrets.assert_awaited_once()
