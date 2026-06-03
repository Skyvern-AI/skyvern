"""Regression test: a Bitwarden login item without a ``totp`` field must not crash.

Credentials whose 2FA is delivered as text (``totp_type="text"``) have no
authenticator seed, so the Bitwarden server's ``/object/item/{id}`` response omits
the ``totp`` key from the ``login`` object. ``_get_credential_item_by_id_using_server``
read it with ``login_item["totp"]``, raising ``KeyError: 'totp'`` during workflow
run-context initialization (before any browser opens). It must read the optional
fields defensively instead.
"""

import pytest

import skyvern.forge.sdk.services.bitwarden as bitwarden_module
from skyvern.forge.sdk.schemas.credentials import CredentialType, PasswordCredential
from skyvern.forge.sdk.services.bitwarden import BitwardenService


def _login_response(login: dict) -> dict:
    # Bitwarden item type is an int enum; LOGIN == 1.
    return {"success": True, "data": {"type": 1, "name": "Example", "login": login}}


@pytest.mark.asyncio
async def test_login_item_without_totp_key_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    # `totp` key entirely absent — the exact shape for text/SMS-delivered 2FA.
    async def fake_get_json(*args, **kwargs) -> dict:
        return _login_response({"username": "Gudrun", "password": "pw"})

    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", fake_get_json)

    item = await BitwardenService._get_credential_item_by_id_using_server("item_1")

    assert item.credential_type == CredentialType.PASSWORD
    assert isinstance(item.credential, PasswordCredential)
    assert item.credential.username == "Gudrun"
    assert item.credential.password == "pw"
    assert item.credential.totp is None


@pytest.mark.asyncio
async def test_login_item_with_totp_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(*args, **kwargs) -> dict:
        return _login_response({"username": "u", "password": "p", "totp": "JBSWY3DPEHPK3PXP"})

    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", fake_get_json)

    item = await BitwardenService._get_credential_item_by_id_using_server("item_1")

    assert item.credential.totp == "JBSWY3DPEHPK3PXP"


@pytest.mark.asyncio
async def test_login_item_with_null_fields_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_json(*args, **kwargs) -> dict:
        return _login_response({"username": None, "password": None, "totp": None})

    monkeypatch.setattr(bitwarden_module, "aiohttp_get_json", fake_get_json)

    item = await BitwardenService._get_credential_item_by_id_using_server("item_1")

    assert item.credential.username == ""
    assert item.credential.password == ""
    assert item.credential.totp is None
