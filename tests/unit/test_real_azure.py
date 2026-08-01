from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

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


def _http_error(status_code: int) -> HttpResponseError:
    error = HttpResponseError(f"status {status_code}")
    error.status_code = status_code
    return error


class _UnauthorizedVersionIterator:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    def __aiter__(self) -> "_UnauthorizedVersionIterator":
        return self

    async def __anext__(self) -> object:
        raise _http_error(self._status_code)


class _NonAuthErrorVersionIterator:
    def __aiter__(self) -> "_NonAuthErrorVersionIterator":
        return self

    async def __anext__(self) -> object:
        raise _http_error(500)


class _PartialThenErrorVersionIterator:
    def __init__(self, items: list[object]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_PartialThenErrorVersionIterator":
        return self

    async def __anext__(self) -> object:
        if self._items:
            return self._items.pop(0)
        raise _http_error(403)


class _PartialThenNotFoundVersionIterator:
    def __init__(self, items: list[object]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_PartialThenNotFoundVersionIterator":
        return self

    async def __anext__(self) -> object:
        if self._items:
            return self._items.pop(0)
        raise ResourceNotFoundError("versions vanished mid-pagination")


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
async def test_get_secret_returns_none_when_secret_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_client = MagicMock()
    secret_client.get_secret = AsyncMock(side_effect=ResourceNotFoundError("secret not found"))
    secret_client.close = AsyncMock()
    client = _vault_client(monkeypatch, secret_client)

    result = await client.get_secret("my-secret", "vault")

    assert result is None
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_secret_reraises_transient_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_client = MagicMock()
    secret_client.get_secret = AsyncMock(side_effect=RuntimeError("vault unavailable"))
    secret_client.close = AsyncMock()
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(RuntimeError, match="vault unavailable"):
        await client.get_secret("my-secret", "vault")

    secret_client.close.assert_awaited_once()


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
async def test_create_or_update_secret_fails_when_enumerated_version_cannot_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(
        _AsyncVersionIterator([SimpleNamespace(version="v-old", enabled=True)]),
        disable_error=_http_error(403),
    )
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(HttpResponseError, match="status 403"):
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    secret_client.set_secret.assert_awaited_once()
    secret_client.update_secret_properties.assert_awaited_once_with("my-secret", "v-old", enabled=False)
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
@pytest.mark.parametrize("status_code", [401, 403])
async def test_create_or_update_secret_fails_when_version_listing_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    secret_client = _secret_client(_UnauthorizedVersionIterator(status_code))
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(HttpResponseError) as exc_info:
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    assert exc_info.value.status_code == status_code
    secret_client.set_secret.assert_not_awaited()
    secret_client.update_secret_properties.assert_not_awaited()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_fails_when_version_listing_errors_non_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(_NonAuthErrorVersionIterator())
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(HttpResponseError):
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    # A non-authorization enumeration failure must fail the write so it retries, not proceed and leave
    # prior versions enabled.
    secret_client.set_secret.assert_not_awaited()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_fails_when_version_listing_fails_after_partial_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_client = _secret_client(_PartialThenErrorVersionIterator([SimpleNamespace(version="v-old", enabled=True)]))
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(HttpResponseError):
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    # A failure mid-enumeration must fail the write rather than proceed with a half-built version list
    # that would leave the un-enumerated prior versions enabled and readable.
    secret_client.set_secret.assert_not_awaited()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_fails_when_versions_disappear_after_partial_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A not-found *before* any version is enumerated means a brand-new secret (see
    # test_create_or_update_secret_handles_missing_prior_versions). A not-found *after* some versions were
    # enumerated means pagination was interrupted, so the write must fail rather than leave the
    # un-enumerated priors enabled and readable. ResourceNotFoundError subclasses HttpResponseError, so it
    # must be handled by its own gated branch, not swallowed unconditionally.
    secret_client = _secret_client(
        _PartialThenNotFoundVersionIterator([SimpleNamespace(version="v-old", enabled=True)])
    )
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(ResourceNotFoundError):
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    secret_client.set_secret.assert_not_awaited()
    secret_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_or_update_secret_fails_when_auth_error_follows_only_skipped_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first page yields only an already-disabled version (skipped, so `previous_versions` stays empty),
    # then a later page raises 401/403. An empty disable list must not be mistaken for a clean "cannot list
    # at all" auth failure: enumeration started, so the un-enumerated later versions may still be readable.
    secret_client = _secret_client(_PartialThenErrorVersionIterator([SimpleNamespace(version="v-old", enabled=False)]))
    client = _vault_client(monkeypatch, secret_client)

    with pytest.raises(HttpResponseError):
        await client.create_or_update_secret("my-secret", "new-value", "vault")

    secret_client.set_secret.assert_not_awaited()
    secret_client.close.assert_awaited_once()
