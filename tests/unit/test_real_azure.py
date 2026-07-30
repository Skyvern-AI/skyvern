from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

from skyvern.forge.sdk.api.real_azure import RealAsyncAzureVaultClient


class _AsyncVersionIterator:
    def __init__(self, items: list[object]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncVersionIterator":
        return self

    async def __anext__(self) -> object:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _RaisingVersionIterator:
    def __aiter__(self) -> "_RaisingVersionIterator":
        return self

    async def __anext__(self) -> object:
        raise ResourceNotFoundError("no prior versions")


class _UnauthorizedVersionIterator:
    def __aiter__(self) -> "_UnauthorizedVersionIterator":
        return self

    async def __anext__(self) -> object:
        raise ClientAuthenticationError("caller lacks secrets/list permission")


def _secret_client(versions_iterator: object, *, disable_error: Exception | None = None) -> MagicMock:
    secret_client = MagicMock()
    secret_client.list_properties_of_secret_versions = MagicMock(return_value=versions_iterator)
    secret_client.set_secret = AsyncMock(return_value=SimpleNamespace(name="my-secret"))
    secret_client.update_secret_properties = AsyncMock(side_effect=disable_error)
    secret_client.close = AsyncMock()
    return secret_client


def _vault_client(monkeypatch: pytest.MonkeyPatch, secret_client: MagicMock) -> RealAsyncAzureVaultClient:
    client = RealAsyncAzureVaultClient(credential=AsyncMock())
    monkeypatch.setattr(client, "_get_secret_client", AsyncMock(return_value=secret_client))
    return client


@pytest.mark.asyncio
async def test_create_or_update_secret_disables_only_preexisting_enabled_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(
        _AsyncVersionIterator(
            [
                SimpleNamespace(version="v-enabled", enabled=True),
                SimpleNamespace(version="v-unset", enabled=None),  # enabled is not False -> still readable
                SimpleNamespace(version="v-disabled", enabled=False),  # already disabled -> skip
                SimpleNamespace(version=None, enabled=True),  # no version id -> skip
            ]
        )
    )
    client = _vault_client(monkeypatch, secret_client)

    result = await client.create_or_update_secret("my-secret", "new-value", "vault")

    assert result == "my-secret"
    secret_client.set_secret.assert_awaited_once_with("my-secret", "new-value")
    disabled = sorted(sent.args[1] for sent in secret_client.update_secret_properties.await_args_list)
    assert disabled == ["v-enabled", "v-unset"]
    for sent in secret_client.update_secret_properties.await_args_list:
        assert sent.kwargs == {"enabled": False}
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_snapshots_versions_before_writing_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order: list[str] = []
    secret_client = _secret_client(_AsyncVersionIterator([SimpleNamespace(version="v-old", enabled=True)]))
    secret_client.set_secret = AsyncMock(
        side_effect=lambda *_: call_order.append("set") or SimpleNamespace(name="my-secret")
    )
    secret_client.update_secret_properties = AsyncMock(side_effect=lambda *a, **k: call_order.append("disable"))
    secret_client.list_properties_of_secret_versions = MagicMock(
        side_effect=lambda *_: (
            call_order.append("list") or _AsyncVersionIterator([SimpleNamespace(version="v-old", enabled=True)])
        )
    )
    client = _vault_client(monkeypatch, secret_client)

    await client.create_or_update_secret("my-secret", "new-value", "vault")

    # The new value must be written before any prior version is disabled, so a mid-write crash never
    # leaves the secret with every version disabled and no readable value.
    assert call_order == ["list", "set", "disable"]


@pytest.mark.asyncio
async def test_create_or_update_secret_survives_version_disable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(
        _AsyncVersionIterator([SimpleNamespace(version="v-old", enabled=True)]),
        disable_error=RuntimeError("disable failed"),
    )
    client = _vault_client(monkeypatch, secret_client)

    result = await client.create_or_update_secret("my-secret", "new-value", "vault")

    assert result == "my-secret"
    secret_client.set_secret.assert_awaited_once()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_handles_missing_prior_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(_RaisingVersionIterator())
    client = _vault_client(monkeypatch, secret_client)

    result = await client.create_or_update_secret("my-secret", "new-value", "vault")

    assert result == "my-secret"
    secret_client.update_secret_properties.assert_not_awaited()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_writes_when_version_listing_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(_UnauthorizedVersionIterator())
    client = _vault_client(monkeypatch, secret_client)

    result = await client.create_or_update_secret("my-secret", "new-value", "vault")

    assert result == "my-secret"
    secret_client.set_secret.assert_awaited_once_with("my-secret", "new-value")
    secret_client.update_secret_properties.assert_not_awaited()
    secret_client.close.assert_awaited_once()
