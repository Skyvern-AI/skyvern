"""Tests for SKY-8847: Bitwarden CLI path must coerce null username/password to empty strings.

The Bitwarden CLI sometimes returns login items whose `username`, `password`, or `totp`
fields are present but have a JSON `null` value. `dict.get(key, default)` returns the
actual value (i.e. `None`) when the key exists, not the default — so callers must
explicitly coerce `None` to `""` before handing the value to the form-filler.

The server-side path (`_get_login_item_by_id_using_server`) already does this with
`login["username"] or ""`; the CLI path used to use the bare `dict.get(..., "")`
pattern, which silently propagated `None` into the secret store. That manifested as
spurious "invalid credentials" errors during runs because the real value never got
typed into the login form.
"""

import json

import pytest

from skyvern.forge.sdk.services.bitwarden import (
    BitwardenConstants,
    BitwardenService,
    RunCommandResult,
)


@pytest.fixture(autouse=True)
def _stub_bitwarden_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_login(*args: object, **kwargs: object) -> None:
        return None

    async def _noop_sync() -> None:
        return None

    async def _fake_unlock(_master_password: str) -> str:
        return "session-key"

    async def _noop_logout() -> None:
        return None

    async def _noop_jitter() -> None:
        return None

    monkeypatch.setattr(BitwardenService, "login", _noop_login)
    monkeypatch.setattr(BitwardenService, "sync", _noop_sync)
    monkeypatch.setattr(BitwardenService, "unlock", _fake_unlock)
    monkeypatch.setattr(BitwardenService, "logout", _noop_logout)
    monkeypatch.setattr(BitwardenService, "_apply_jitter", _noop_jitter)


@pytest.mark.asyncio
async def test_get_secret_value_by_item_id_coerces_null_fields_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "login": {
            "username": None,
            "password": None,
            "totp": None,
        },
    }

    async def fake_run_command(command: list[str], **_: object) -> RunCommandResult:
        return RunCommandResult(stdout=json.dumps(item_payload), stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    result = await BitwardenService.get_secret_value_from_url(
        client_id="client-id",
        client_secret="client-secret",
        master_password="master-password",
        bw_organization_id="org-id",
        bw_collection_ids=None,
        item_id="11111111-1111-1111-1111-111111111111",
    )

    assert result[BitwardenConstants.USERNAME] == ""
    assert result[BitwardenConstants.PASSWORD] == ""
    assert result[BitwardenConstants.TOTP] == ""


@pytest.mark.asyncio
async def test_get_secret_value_by_url_coerces_null_fields_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_payload = [
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "login": {
                "username": None,
                "password": "real-password",
                "totp": None,
                "uris": [{"uri": "https://example.com"}],
            },
        }
    ]

    async def fake_run_command(command: list[str], **_: object) -> RunCommandResult:
        return RunCommandResult(stdout=json.dumps(list_payload), stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    result = await BitwardenService.get_secret_value_from_url(
        client_id="client-id",
        client_secret="client-secret",
        master_password="master-password",
        bw_organization_id="org-id",
        bw_collection_ids=None,
        url="https://example.com/login",
    )

    assert result[BitwardenConstants.USERNAME] == ""
    assert result[BitwardenConstants.PASSWORD] == "real-password"
    assert result[BitwardenConstants.TOTP] == ""


@pytest.mark.asyncio
async def test_get_secret_value_by_item_id_preserves_real_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_payload = {
        "id": "33333333-3333-3333-3333-333333333333",
        "login": {
            "username": "alice@example.com",
            "password": "hunter2",
            "totp": "",
        },
    }

    async def fake_run_command(command: list[str], **_: object) -> RunCommandResult:
        return RunCommandResult(stdout=json.dumps(item_payload), stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    result = await BitwardenService.get_secret_value_from_url(
        client_id="client-id",
        client_secret="client-secret",
        master_password="master-password",
        bw_organization_id="org-id",
        bw_collection_ids=None,
        item_id="33333333-3333-3333-3333-333333333333",
    )

    assert result[BitwardenConstants.USERNAME] == "alice@example.com"
    assert result[BitwardenConstants.PASSWORD] == "hunter2"
    assert result[BitwardenConstants.TOTP] == ""


@pytest.mark.asyncio
async def test_get_credit_card_data_includes_billing_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_payload = {
        "id": "44444444-4444-4444-4444-444444444444",
        "type": 3,
        "organizationId": "org-id",
        "collectionIds": ["collection-id"],
        "card": {
            "cardholderName": "Jane Doe",
            "number": "4111111111111111",
            "expMonth": "12",
            "expYear": "2030",
            "code": "123",
            "brand": "visa",
        },
        "fields": [
            {"name": "billing_address_line1", "value": "123 Main St"},
            {"name": "billing_address_country_code", "value": "US"},
            {"name": "billing_email", "value": "billing@example.com"},
            {"name": "metadata_customer_id", "value": "cus_123"},
        ],
    }

    async def fake_run_command(command: list[str], **_: object) -> RunCommandResult:
        return RunCommandResult(stdout=json.dumps(item_payload), stderr="", returncode=0)

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    result = await BitwardenService.get_credit_card_data(
        client_id="client-id",
        client_secret="client-secret",
        master_password="master-password",
        bw_organization_id="org-id",
        bw_collection_ids=["collection-id"],
        collection_id="collection-id",
        item_id="44444444-4444-4444-4444-444444444444",
    )

    assert result[BitwardenConstants.CREDIT_CARD_NUMBER] == "4111111111111111"
    assert result["billing_address_line1"] == "123 Main St"
    assert result["billing_address_country_code"] == "US"
    assert result["billing_email"] == "billing@example.com"
    assert result["metadata_customer_id"] == "cus_123"
