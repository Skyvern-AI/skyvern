from datetime import datetime
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.tools.integrations import _list_integrations, _serialize
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from tests.unit.conftest import render_agent_prompt

ORGANIZATION_ID = "o_test_org"

TOKEN_FIELD_NAMES = (
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "secret",
    "id_token",
    "encrypted_refresh_token",
)


def _google(**overrides: object) -> GoogleOAuthCredentialBase:
    defaults = {
        "id": "goac_1",
        "organization_id": ORGANIZATION_ID,
        "credential_name": "Sheets account",
        "state": "active",
        "scopes_requested": ["https://www.googleapis.com/auth/spreadsheets"],
        "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
        "created_at": datetime(2026, 6, 19),
        "modified_at": datetime(2026, 6, 19),
    }
    return GoogleOAuthCredentialBase(**{**defaults, **overrides})


def _microsoft(**overrides: object) -> MicrosoftOAuthCredentialBase:
    defaults = {
        "id": "msoac_1",
        "organization_id": ORGANIZATION_ID,
        "credential_name": "Outlook account",
        "state": "active",
        "scopes_requested": ["Mail.Send"],
        "scopes_granted": ["Mail.Send"],
        "created_at": datetime(2026, 6, 19),
        "modified_at": datetime(2026, 6, 19),
    }
    return MicrosoftOAuthCredentialBase(**{**defaults, **overrides})


@pytest.fixture
def patched_services(monkeypatch: pytest.MonkeyPatch):
    def _apply(google: list[GoogleOAuthCredentialBase], microsoft: list[MicrosoftOAuthCredentialBase]) -> None:
        async def fake_google(organization_id: str) -> list[GoogleOAuthCredentialBase]:
            assert organization_id == ORGANIZATION_ID
            return google

        async def fake_microsoft(organization_id: str) -> list[MicrosoftOAuthCredentialBase]:
            assert organization_id == ORGANIZATION_ID
            return microsoft

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.integrations.google_oauth_service.get_visible_credentials_for_org",
            fake_google,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.integrations.microsoft_oauth_service.get_credentials_for_org",
            fake_microsoft,
        )

    return _apply


@pytest.mark.asyncio
async def test_lists_both_providers_with_the_fields_the_agent_needs(patched_services) -> None:
    patched_services([_google()], [_microsoft()])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    google_entry, microsoft_entry = result["data"]["integrations"]
    assert google_entry == {
        "connection_id": "goac_1",
        "provider": "google",
        "name": "Sheets account",
        "state": "active",
        "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
    }
    assert microsoft_entry == {
        "connection_id": "msoac_1",
        "provider": "microsoft",
        "name": "Outlook account",
        "state": "active",
        "scopes_granted": ["Mail.Send"],
    }


@pytest.mark.asyncio
async def test_reports_no_integrations_without_erroring(patched_services) -> None:
    patched_services([], [])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["ok"] is True
    assert result["data"] == {"integrations": [], "count": 0}


def test_serializer_is_an_allowlist_so_a_new_token_field_cannot_leak() -> None:
    leaky_google = SimpleNamespace(
        id="goac_1",
        credential_name="Sheets account",
        state="active",
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        **{name: f"SECRET_{name}" for name in TOKEN_FIELD_NAMES},
    )

    entry = _serialize(leaky_google, "google")

    assert set(entry) == {"connection_id", "provider", "name", "state", "scopes_granted"}
    for name in TOKEN_FIELD_NAMES:
        assert name not in entry
    assert not any("SECRET_" in str(value) for value in entry.values())


def test_prompt_routes_oauth_accounts_to_the_right_lookup() -> None:
    rendered = render_agent_prompt()
    assert "appear ONLY in `list_integrations`" in rendered


@pytest.mark.asyncio
async def test_lists_a_google_connection_whose_grant_expired(patched_services) -> None:
    patched_services([_google(state="error")], [])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["data"]["count"] == 1
    assert result["data"]["integrations"][0]["state"] == "error"
