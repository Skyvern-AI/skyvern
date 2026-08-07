from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.services import bitwarden as bitwarden_module
from skyvern.forge.sdk.services.bitwarden import (
    BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
    BitwardenItemType,
    BitwardenService,
    RunCommandResult,
    get_list_response_item_from_bitwarden_item,
)

# The vault server omits `totp` entirely for a login saved without a two-factor secret,
# and returns it as JSON null for some hand-made items.
MISSING_TOTP_LOGINS = [
    pytest.param({"username": "user@example.com", "password": "pw"}, id="totp-absent"),
    pytest.param({"username": "user@example.com", "password": "pw", "totp": None}, id="totp-null"),
]


@pytest.mark.asyncio
async def test_login_ignores_data_file_creation_notice_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_command(*args, **kwargs) -> RunCommandResult:
        return RunCommandResult(
            stdout="You are logged in!\n\nTo unlock your vault, use the `unlock` command.",
            stderr='Could not find data file, "/tmp/bitwarden/data.json"; creating it instead.\n',
            returncode=0,
        )

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    await BitwardenService.login("client-id", "client-secret", master_password="master-password")


@pytest.mark.asyncio
async def test_server_login_item_round_trips_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"tenant": "north", "account_id": "acct_123"}
    stored_item: dict = {}
    get_json = AsyncMock(
        side_effect=[
            {"data": {"template": {}}},
            {"data": {"template": {}}},
            {"success": True, "data": stored_item},
        ]
    )
    post = AsyncMock(return_value={"success": True, "data": {"id": "item-1"}})
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)
    monkeypatch.setattr(bitwarden_module, "aiohttp_post", post)

    item_id = await BitwardenService._create_login_item_using_server(
        bw_organization_id="bw-org",
        collection_id="collection-1",
        name="Login",
        credential=PasswordCredential(username="user@example.com", password="pw", totp="", metadata=metadata),
    )
    stored_item.update(post.await_args.kwargs["data"], id=item_id)
    listed_item = get_list_response_item_from_bitwarden_item(stored_item)
    fetched_item = await BitwardenService._get_credential_item_by_id_using_server(item_id)

    assert stored_item["fields"] == [
        {
            "name": "metadata_tenant",
            "value": "north",
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        },
        {
            "name": "metadata_account_id",
            "value": "acct_123",
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        },
    ]
    assert listed_item.credential.metadata == metadata
    assert fetched_item.credential.metadata == metadata


@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
def test_list_response_item_reads_login_without_totp(login: dict) -> None:
    item = {"id": "item-1", "name": "Login", "type": BitwardenItemType.LOGIN, "login": login}

    listed_item = get_list_response_item_from_bitwarden_item(item)

    assert listed_item.credential.totp == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
async def test_get_login_item_by_id_reads_login_without_totp(monkeypatch: pytest.MonkeyPatch, login: dict) -> None:
    get_json = AsyncMock(return_value={"success": True, "data": {"login": login}})
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)

    credential = await BitwardenService._get_login_item_by_id_using_server("item-1")

    assert credential.totp == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("login", MISSING_TOTP_LOGINS)
async def test_get_credential_item_by_id_reads_login_without_totp(monkeypatch: pytest.MonkeyPatch, login: dict) -> None:
    get_json = AsyncMock(
        return_value={
            "success": True,
            "data": {"id": "item-1", "name": "Login", "type": BitwardenItemType.LOGIN, "login": login},
        }
    )
    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", get_json)

    fetched_item = await BitwardenService._get_credential_item_by_id_using_server("item-1")

    assert fetched_item.credential.totp == ""
