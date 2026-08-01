import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import HttpException
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    NonEmptyPasswordCredential,
)
from skyvern.forge.sdk.services import bitwarden as bitwarden_module
from skyvern.forge.sdk.services.credential import bitwarden_credential_service as service_module
from skyvern.forge.sdk.services.credential.bitwarden_credential_service import BitwardenCredentialVaultService


def _credential() -> Credential:
    return Credential(
        credential_id="cred_test",
        organization_id="org_test",
        name="Login",
        vault_type=CredentialVaultType.BITWARDEN,
        item_id="item_old",
        credential_type=CredentialType.PASSWORD,
        username="user_test",
        totp_type="none",
        totp_identifier=None,
        card_last4=None,
        card_brand=None,
        secret_label=None,
        browser_profile_id=None,
        tested_url=None,
        user_context=None,
        save_browser_session_intent=False,
        folder_id=None,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        deleted_at=None,
    )


def _password_request() -> CreateCredentialRequest:
    return CreateCredentialRequest(
        name="Login Updated",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(
            username="user_test",
            password="secret_test",
            metadata={"key": "value"},
        ),
    )


def _prepare_update(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    *,
    delete_failure: Exception | None = None,
) -> tuple[BitwardenCredentialVaultService, AsyncMock]:
    service = BitwardenCredentialVaultService()
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "create_credential_item",
        AsyncMock(return_value="item_new"),
    )
    delete_item = AsyncMock(side_effect=delete_failure)
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", delete_item)
    monkeypatch.setattr(service, "_update_db_credential", AsyncMock(side_effect=failure))
    return service, delete_item


@pytest.mark.asyncio
async def test_delete_credential_removes_db_row_before_vault_item(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=lambda *_: calls.append("vault_delete")),
    )
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "delete_credential",
        AsyncMock(side_effect=lambda *_args, **_kwargs: calls.append("db_delete")),
    )

    await BitwardenCredentialVaultService().delete_credential(_credential())

    assert calls == ["db_delete", "vault_delete"]


@pytest.mark.asyncio
async def test_delete_credential_enqueues_cleanup_when_vault_delete_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=RuntimeError("vault unavailable")),
    )
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", db_delete)
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    await BitwardenCredentialVaultService().delete_credential(_credential())

    db_delete.assert_awaited_once_with("cred_test", "org_test")
    orphaned.assert_awaited_once_with(
        organization_id="org_test",
        item_id="item_old",
        vault_type=CredentialVaultType.BITWARDEN,
    )


@pytest.mark.asyncio
async def test_delete_credential_surfaces_durable_cleanup_enqueue_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=RuntimeError("vault unavailable")),
    )
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", db_delete)
    orphaned = AsyncMock(side_effect=RuntimeError("cleanup enqueue unavailable"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(RuntimeError, match="cleanup enqueue unavailable"):
        await BitwardenCredentialVaultService().delete_credential(_credential())

    db_delete.assert_awaited_once_with("cred_test", "org_test")
    orphaned.assert_awaited_once_with(
        organization_id="org_test",
        item_id="item_old",
        vault_type=CredentialVaultType.BITWARDEN,
    )


@pytest.mark.asyncio
async def test_delete_credential_enqueues_cleanup_when_vault_delete_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", db_delete)
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await BitwardenCredentialVaultService().delete_credential(_credential())

    db_delete.assert_awaited_once_with("cred_test", "org_test")
    orphaned.assert_awaited_once_with(
        organization_id="org_test",
        item_id="item_old",
        vault_type=CredentialVaultType.BITWARDEN,
    )


@pytest.mark.asyncio
async def test_delete_credential_vault_cancellation_wins_over_cleanup_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", AsyncMock())
    orphaned = AsyncMock(side_effect=RuntimeError("cleanup enqueue unavailable"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await BitwardenCredentialVaultService().delete_credential(_credential())

    orphaned.assert_awaited_once_with(
        organization_id="org_test",
        item_id="item_old",
        vault_type=CredentialVaultType.BITWARDEN,
    )


@pytest.mark.asyncio
async def test_delete_credential_reports_when_durable_cleanup_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        service_module.BitwardenService,
        "delete_credential_item",
        AsyncMock(side_effect=RuntimeError("vault unavailable")),
    )
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", AsyncMock(return_value=False))
    log_error = MagicMock()
    monkeypatch.setattr(service_module.LOG, "error", log_error)

    await BitwardenCredentialVaultService().delete_credential(_credential())

    log_error.assert_called_once_with(
        "Bitwarden vault-item delete failed after DB row deletion; durable cleanup unavailable",
        organization_id="org_test",
        item_id="item_old",
        error_type="RuntimeError",
    )


@pytest.mark.asyncio
async def test_delete_credential_finishes_vault_delete_before_propagating_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()
    delete_finished = asyncio.Event()

    async def delete_item(_item_id: str) -> None:
        delete_started.set()
        await release_delete.wait()
        delete_finished.set()

    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", AsyncMock(side_effect=delete_item))
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", AsyncMock())
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    delete_task = asyncio.create_task(BitwardenCredentialVaultService().delete_credential(_credential()))
    await delete_started.wait()
    delete_task.cancel()
    await asyncio.sleep(0)
    release_delete.set()

    with pytest.raises(asyncio.CancelledError):
        await delete_task

    assert delete_finished.is_set()
    orphaned.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_credential_cancellation_wins_over_cleanup_enqueue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def delete_item(_item_id: str) -> None:
        delete_started.set()
        await release_delete.wait()
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", AsyncMock(side_effect=delete_item))
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", AsyncMock())
    orphaned = AsyncMock(side_effect=RuntimeError("cleanup enqueue unavailable"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    delete_task = asyncio.create_task(BitwardenCredentialVaultService().delete_credential(_credential()))
    await delete_started.wait()
    delete_task.cancel()
    await asyncio.sleep(0)
    release_delete.set()

    # Both the vault delete and the cleanup enqueue fail while the caller is cancelling: the
    # cancellation must still be what propagates, with the cleanup failure only logged.
    with pytest.raises(asyncio.CancelledError):
        await delete_task

    orphaned.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_credential_does_not_touch_vault_when_db_delete_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    delete_item = AsyncMock()
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", delete_item)
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "delete_credential",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await BitwardenCredentialVaultService().delete_credential(_credential())

    delete_item.assert_not_awaited()
    orphaned.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_credential_removes_db_row_when_vault_item_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_url = f"{bitwarden_module.BITWARDEN_SERVER_BASE_URL}/object/item/item_old"
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_organization_bitwarden_collection",
        AsyncMock(return_value=SimpleNamespace(collection_id="collection_test")),
    )
    monkeypatch.setattr(
        bitwarden_module.BitwardenService,
        "_get_skyvern_auth_secrets",
        AsyncMock(return_value=("master-password", "client-id", "client-secret", "admin-password")),
    )
    monkeypatch.setattr(bitwarden_module.BitwardenService, "_unlock_using_server", AsyncMock())
    aiohttp_delete = AsyncMock(side_effect=HttpException(404, item_url))
    monkeypatch.setattr(bitwarden_module, "aiohttp_delete", aiohttp_delete)
    db_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.credentials, "delete_credential", db_delete)
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    await BitwardenCredentialVaultService().delete_credential(_credential())

    db_delete.assert_awaited_once_with("cred_test", "org_test")
    aiohttp_delete.assert_awaited_once_with(item_url, timeout=120)
    orphaned.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_delete_credential_item_returns_provider_failure_status(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_item = AsyncMock(side_effect=RuntimeError("vault unavailable"))
    monkeypatch.setattr(service_module.BitwardenService, "delete_credential_item", delete_item)

    result = await BitwardenCredentialVaultService().post_delete_credential_item("item_old", "org_1")

    assert result is False
    delete_item.assert_awaited_once_with("item_old")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "message"),
    [
        pytest.param(RuntimeError, "database unavailable", id="exception"),
        pytest.param(asyncio.CancelledError, "repoint cancelled", id="cancelled"),
    ],
)
async def test_update_credential_reclaims_new_item_when_repoint_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    message: str,
) -> None:
    service, delete_item = _prepare_update(monkeypatch, failure_type(message))

    with pytest.raises(failure_type):
        await service.update_credential(_credential(), _password_request())

    delete_item.assert_awaited_with("item_new")


@pytest.mark.asyncio
async def test_update_credential_enqueues_orphan_when_cancelled_inline_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, delete_item = _prepare_update(
        monkeypatch,
        asyncio.CancelledError(),
        delete_failure=RuntimeError("vault unavailable"),
    )
    orphaned = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_credential_item_orphaned", orphaned)

    with pytest.raises(asyncio.CancelledError):
        await service.update_credential(_credential(), _password_request())

    delete_item.assert_awaited_with("item_new")
    orphaned.assert_awaited_with(
        organization_id="org_test",
        item_id="item_new",
        vault_type=CredentialVaultType.BITWARDEN,
    )
