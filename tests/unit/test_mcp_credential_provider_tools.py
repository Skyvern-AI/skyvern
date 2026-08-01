from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import skyvern.cli.mcp_tools.credential as credential_tools
from skyvern.cli.core.client import reset_api_key_override, set_api_key_override
from skyvern.cli.mcp_tools import mcp
from skyvern.client.errors import NotFoundError


@pytest.mark.asyncio
async def test_onepassword_items_lists_metadata_with_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_http_get = AsyncMock(
        return_value={
            "configured": True,
            "items": [
                {
                    "item_id": "item_keep",
                    "title": "Production Login",
                    "vault_id": "vault_prod",
                    "vault_name": "Production",
                    "category": "LOGIN",
                    "url": "https://app.example.com",
                },
                {
                    "item_id": "item_skip",
                    "title": "Staging Login",
                    "vault_id": "vault_stage",
                    "vault_name": "Staging",
                    "category": "LOGIN",
                    "url": "https://staging.example.com",
                },
            ],
        }
    )
    monkeypatch.setattr(credential_tools, "raw_http_get", raw_http_get)

    result = await credential_tools.skyvern_onepassword_items(search="production", vault_id="vault_prod", limit=10)

    assert result["ok"] is True
    raw_http_get.assert_awaited_once_with("v1/credentials/onepassword/items")
    assert result["data"]["configured"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["matched_count"] == 1
    assert result["data"]["items"][0]["item_id"] == "item_keep"


@pytest.mark.asyncio
async def test_bitwarden_items_lists_metadata_with_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_http_get = AsyncMock(
        return_value={
            "configured": True,
            "items": [
                {
                    "item_id": "item_keep",
                    "title": "Production Login",
                    "collection_id": "collection_prod",
                    "credential_type": "password",
                    "url": "https://app.example.com",
                },
                {
                    "item_id": "item_skip",
                    "title": "Production Card",
                    "collection_id": "collection_prod",
                    "credential_type": "credit_card",
                    "url": "https://app.example.com",
                },
            ],
        }
    )
    monkeypatch.setattr(credential_tools, "raw_http_get", raw_http_get)

    result = await credential_tools.skyvern_bitwarden_items(
        search="production",
        collection_id="collection_prod",
        credential_type="password",
        limit=10,
    )

    assert result["ok"] is True
    raw_http_get.assert_awaited_once_with("v1/credentials/bitwarden/items")
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["item_id"] == "item_keep"


@pytest.mark.asyncio
async def test_bitwarden_items_rejects_unknown_credential_type() -> None:
    result = await credential_tools.skyvern_bitwarden_items(credential_type="bank_account")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_onepassword_config_get_returns_unconfigured_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_http_get = AsyncMock(side_effect=NotFoundError(body={"detail": "missing"}))
    monkeypatch.setattr(credential_tools, "raw_http_get", raw_http_get)

    result = await credential_tools.skyvern_onepassword_config_get()

    assert result["ok"] is True
    raw_http_get.assert_awaited_once_with("v1/credentials/onepassword/get")
    assert result["data"] == {"configured": False, "provider": "onepassword"}


@pytest.mark.asyncio
async def test_onepassword_config_set_redacts_service_account_token(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_http_post = AsyncMock(
        return_value={
            "token": {
                "id": "oat_123",
                "organization_id": "org_123",
                "token_type": "onepassword_service_account",
                "token": "op_secret",
                "valid": True,
            }
        }
    )
    monkeypatch.setattr(credential_tools, "raw_http_post", raw_http_post)
    monkeypatch.setattr(credential_tools.settings, "OP_SERVICE_ACCOUNT_TOKEN", "op_secret")

    result = await credential_tools.skyvern_onepassword_config_set()

    assert result["ok"] is True
    raw_http_post.assert_awaited_once_with("v1/credentials/onepassword/create", json_body={"token": "op_secret"})
    assert result["data"]["configured"] is True
    assert result["data"]["token"]["id"] == "oat_123"
    assert "token" not in result["data"]["token"]


@pytest.mark.asyncio
async def test_bitwarden_config_set_uses_environment_secret_and_accepts_safe_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_http_post = AsyncMock(
        return_value={
            "token": {
                "id": "oat_123",
                "organization_id": "org_123",
                "token_type": "bitwarden_credential",
                "valid": True,
                "credential": {
                    "email": "user@example.com",
                },
            }
        }
    )
    monkeypatch.setattr(credential_tools, "raw_http_post", raw_http_post)
    monkeypatch.setattr(credential_tools.settings, "BITWARDEN_MASTER_PASSWORD", "master-secret")

    result = await credential_tools.skyvern_bitwarden_config_set(email="user@example.com")

    assert result["ok"] is True
    raw_http_post.assert_awaited_once_with(
        "v1/credentials/bitwarden/create",
        json_body={"credential": {"email": "user@example.com", "master_password": "master-secret"}},
    )
    assert result["data"]["token"]["credential"]["email"] == "user@example.com"
    assert "master_password" not in result["data"]["token"]["credential"]


def test_provider_config_redaction_is_a_defense_in_depth_backstop() -> None:
    response = {
        "token": {
            "credential": {
                "email": "user@example.com",
                "master_password": "master-secret",
            }
        }
    }

    assert credential_tools._safe_config_payload(response) == {
        "configured": True,
        "token": {
            "credential": {
                "email": "user@example.com",
                "master_password": "***redacted***",
            }
        },
    }


@pytest.mark.asyncio
async def test_provider_config_set_tool_schemas_do_not_accept_secrets() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    secret_fields = {"token", "master_password", "password", "secret"}

    onepassword_properties = tools["skyvern_onepassword_config_set"].parameters.get("properties", {})
    bitwarden_properties = tools["skyvern_bitwarden_config_set"].parameters.get("properties", {})

    assert not secret_fields.intersection(onepassword_properties)
    assert not secret_fields.intersection(bitwarden_properties)
    assert not onepassword_properties
    assert set(bitwarden_properties) == {"email"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "setting_name", "env_var", "kwargs"),
    [
        ("skyvern_onepassword_config_set", "OP_SERVICE_ACCOUNT_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN", {}),
        (
            "skyvern_bitwarden_config_set",
            "BITWARDEN_MASTER_PASSWORD",
            "BITWARDEN_MASTER_PASSWORD",
            {"email": "user@example.com"},
        ),
    ],
)
async def test_provider_config_set_requires_secret_in_server_environment(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    setting_name: str,
    env_var: str,
    kwargs: dict[str, str],
) -> None:
    raw_http_post = AsyncMock()
    monkeypatch.setattr(credential_tools, "raw_http_post", raw_http_post)
    monkeypatch.setattr(credential_tools.settings, setting_name, None)

    result = await getattr(credential_tools, tool_name)(**kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert env_var in result["error"]["hint"]
    raw_http_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "setting_name", "secret", "kwargs"),
    [
        ("skyvern_onepassword_config_set", "OP_SERVICE_ACCOUNT_TOKEN", "host-op-secret", {}),
        (
            "skyvern_bitwarden_config_set",
            "BITWARDEN_MASTER_PASSWORD",
            "host-bitwarden-secret",
            {"email": "request@example.com"},
        ),
    ],
)
async def test_provider_config_set_refuses_request_scoped_api_key_override(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    setting_name: str,
    secret: str,
    kwargs: dict[str, str],
) -> None:
    raw_http_post = AsyncMock()
    monkeypatch.setattr(credential_tools, "raw_http_post", raw_http_post)
    monkeypatch.setattr(credential_tools.settings, setting_name, secret)
    override = set_api_key_override("request-api-key")

    try:
        result = await getattr(credential_tools, tool_name)(**kwargs)
    finally:
        reset_api_key_override(override)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["message"] == (
        "Provider configuration setters are only available in single-tenant (stdio/local) mode."
    )
    assert secret not in repr(result)
    assert "request-api-key" not in repr(result)
    assert "request@example.com" not in repr(result)
    raw_http_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "setting_name", "secret", "kwargs"),
    [
        ("skyvern_onepassword_config_set", "OP_SERVICE_ACCOUNT_TOKEN", "op_secret", {}),
        (
            "skyvern_bitwarden_config_set",
            "BITWARDEN_MASTER_PASSWORD",
            "master-secret",
            {"email": "user@example.com"},
        ),
    ],
)
async def test_provider_config_set_errors_do_not_echo_request_input(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    setting_name: str,
    secret: str,
    kwargs: dict[str, str],
) -> None:
    request_input = (
        {"token": secret}
        if setting_name == "OP_SERVICE_ACCOUNT_TOKEN"
        else {"credential": {"email": "user@example.com", "master_password": secret}}
    )
    raw_http_post = AsyncMock(side_effect=RuntimeError(f"HTTP 422: {request_input!r}"))
    monkeypatch.setattr(credential_tools, "raw_http_post", raw_http_post)
    monkeypatch.setattr(credential_tools.settings, setting_name, secret)

    result = await getattr(credential_tools, tool_name)(**kwargs)

    assert result["ok"] is False
    assert secret not in repr(result)
    assert repr(request_input) not in repr(result)


@pytest.mark.asyncio
async def test_provider_config_clear_calls_provider_delete_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_http_delete = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(credential_tools, "raw_http_delete", raw_http_delete)

    result = await credential_tools.skyvern_bitwarden_config_clear()

    assert result["ok"] is True
    raw_http_delete.assert_awaited_once_with("v1/credentials/bitwarden")
    assert result["data"] == {"provider": "bitwarden", "cleared": True}
