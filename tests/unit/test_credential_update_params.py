"""Tests that update_credential() accepts user_context and save_browser_session_intent
on CredentialRepository."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from skyvern.forge import app as forge_app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
from skyvern.forge.sdk.routes import credentials as credentials_routes
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialType,
    CredentialVaultType,
    TotpType,
    UpdateCredentialRequest,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.proxy_pinning import apply_proxy_pin_update as _apply_proxy_pin_update
from skyvern.schemas.proxy_pinning import (
    generate_proxy_session_id,
    is_proxy_session_id,
)
from skyvern.schemas.runs import ProxyLocation
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session


def _make_credential_repo(mock_credential: MagicMock) -> CredentialRepository:
    mock_session = make_mock_session(mock_credential)
    return CredentialRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))


def _make_browser_sessions_repo(mock_browser_profile: MagicMock) -> BrowserSessionsRepository:
    mock_session = make_mock_session(mock_browser_profile)
    return BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))


def _make_password_credential(**overrides: object) -> Credential:
    defaults: dict[str, object] = {
        "credential_id": "cred_123",
        "organization_id": "org_123",
        "name": "test",
        "vault_type": CredentialVaultType.AZURE_VAULT,
        "item_id": "item_123",
        "credential_type": CredentialType.PASSWORD,
        "username": "user@example.com",
        "totp_type": TotpType.NONE,
        "totp_identifier": None,
        "card_last4": None,
        "card_brand": None,
        "secret_label": None,
        "browser_profile_id": None,
        "tested_url": None,
        "user_context": None,
        "save_browser_session_intent": False,
        "folder_id": None,
        "proxy_location": None,
        "proxy_session_id": None,
        "created_at": datetime(2026, 1, 1),
        "modified_at": datetime(2026, 1, 1),
        "deleted_at": None,
    }
    defaults.update(overrides)
    return Credential(**defaults)


# --- CredentialRepository tests ---


@pytest.mark.asyncio
async def test_credential_vault_service_create_db_credential_passes_proxy_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_credential = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(forge_app.DATABASE.credentials, "create_credential", create_credential)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        tested_url="https://example.com/login",
    )

    await CredentialVaultService._create_db_credential(
        organization_id="org_123",
        data=data,
        item_id="item_123",
        vault_type=CredentialVaultType.AZURE_VAULT,
    )

    create_credential.assert_awaited_once()
    assert create_credential.await_args.kwargs["proxy_location"] == ProxyLocation.RESIDENTIAL_ISP
    assert create_credential.await_args.kwargs["proxy_session_id"] is None
    assert create_credential.await_args.kwargs["tested_url"] == "https://example.com/login"


@pytest.mark.asyncio
async def test_create_credential_response_includes_generated_proxy_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy_session_id = generate_proxy_session_id("cred_123")
    stored_credential = _make_password_credential(
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id=proxy_session_id,
        tested_url="https://example.com/login",
        totp_type=TotpType.AUTHENTICATOR,
    )
    vault_service = SimpleNamespace(create_credential=AsyncMock(return_value=stored_credential))
    monkeypatch.setattr(credentials_routes, "_get_credential_vault_service", AsyncMock(return_value=vault_service))

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )

    response = await credentials_routes.create_credential(
        background_tasks=BackgroundTasks(),
        data=data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    assert response.proxy_location == ProxyLocation.RESIDENTIAL_ISP
    assert response.proxy_session_id == proxy_session_id
    assert response.tested_url == "https://example.com/login"
    assert response.credential.totp_type == TotpType.AUTHENTICATOR


@pytest.mark.asyncio
async def test_credential_vault_service_update_db_credential_persists_tested_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_credential = AsyncMock(return_value=MagicMock())
    update_credential_vault_data = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential_vault_data", update_credential_vault_data)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        tested_url="https://example.com/login",
    )
    credential = SimpleNamespace(credential_id="cred_123", organization_id="org_123")
    await CredentialVaultService._update_db_credential(credential=credential, data=data, item_id="item_123")

    update_credential.assert_not_awaited()
    update_credential_vault_data.assert_awaited_once()
    assert update_credential_vault_data.await_args.kwargs["tested_url"] == "https://example.com/login"


@pytest.mark.asyncio
async def test_credential_vault_service_update_db_credential_skips_tested_url_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_credential = AsyncMock(return_value=MagicMock())
    update_credential_vault_data = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential_vault_data", update_credential_vault_data)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        tested_url=None,
    )
    credential = SimpleNamespace(credential_id="cred_123", organization_id="org_123")
    await CredentialVaultService._update_db_credential(credential=credential, data=data, item_id="item_123")

    update_credential.assert_not_awaited()
    update_credential_vault_data.assert_awaited_once()
    assert update_credential_vault_data.await_args.kwargs["tested_url"] is None


@pytest.mark.asyncio
async def test_create_credential_then_get_credential_returns_tested_url(monkeypatch: pytest.MonkeyPatch) -> None:
    stored_credential = _make_password_credential(tested_url="https://example.com/login")
    vault_service = SimpleNamespace(create_credential=AsyncMock(return_value=stored_credential))
    monkeypatch.setattr(credentials_routes, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=stored_credential),
    )

    create_data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        tested_url="https://example.com/login",
    )
    create_response = await credentials_routes.create_credential(
        background_tasks=BackgroundTasks(),
        data=create_data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )
    get_response = await credentials_routes.get_credential(
        credential_id="cred_123",
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    assert create_response.tested_url == "https://example.com/login"
    assert get_response.tested_url == "https://example.com/login"


@pytest.mark.asyncio
async def test_repo_create_credential_clears_incompatible_proxy_pin() -> None:
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    repo = CredentialRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.create_credential(
            organization_id="org_123",
            name="test",
            vault_type=CredentialVaultType.AZURE_VAULT,
            item_id="item_123",
            credential_type=CredentialType.PASSWORD,
            username="user@example.com",
            totp_type="none",
            card_last4=None,
            card_brand=None,
            proxy_location=ProxyLocation.NONE,
            proxy_session_id="abc1234567",
        )

    stored_credential = mock_session.add.call_args.args[0]
    assert stored_credential.proxy_location == ProxyLocation.NONE.value
    assert stored_credential.proxy_session_id is None


@pytest.mark.asyncio
async def test_repo_create_credential_persists_tested_url() -> None:
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    repo = CredentialRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.create_credential(
            organization_id="org_123",
            name="test",
            vault_type=CredentialVaultType.AZURE_VAULT,
            item_id="item_123",
            credential_type=CredentialType.PASSWORD,
            username="user@example.com",
            totp_type="none",
            card_last4=None,
            card_brand=None,
            tested_url="https://example.com/login",
        )

    stored_credential = mock_session.add.call_args.args[0]
    assert stored_credential.tested_url == "https://example.com/login"


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_user_context() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.user_context = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            user_context="Click SSO button first",
        )

    assert mock_credential.user_context == "Click SSO button first"


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_save_browser_session_intent() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.save_browser_session_intent = False
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            save_browser_session_intent=True,
        )

    assert mock_credential.save_browser_session_intent is True


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_tested_url() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.tested_url = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            tested_url="https://example.com/login",
        )

    assert mock_credential.tested_url == "https://example.com/login"


@pytest.mark.asyncio
async def test_repo_update_credential_unset_params_not_applied() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.user_context = "existing"
    mock_credential.save_browser_session_intent = True
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
        )

    assert mock_credential.user_context == "existing"
    assert mock_credential.save_browser_session_intent is True


@pytest.mark.asyncio
async def test_repo_update_credential_explicit_null_browser_profile_id_unlinks() -> None:
    # SKY-12724 item 1: choosing Auto sends browser_profile_id=null, which must unlink the profile.
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.browser_profile_id = "bp_existing"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            browser_profile_id=None,
        )

    assert mock_credential.browser_profile_id is None


@pytest.mark.asyncio
async def test_repo_update_credential_omitted_browser_profile_id_untouched() -> None:
    # Omitting browser_profile_id (a rename, a pin change) must leave the existing link intact.
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.browser_profile_id = "bp_existing"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            name="renamed",
        )

    assert mock_credential.browser_profile_id == "bp_existing"


@pytest.mark.asyncio
async def test_repo_update_credential_tested_url_none_preserves_existing_value() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.tested_url = "https://example.com/existing"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            tested_url=None,
        )

    assert mock_credential.tested_url == "https://example.com/existing"


@pytest.mark.asyncio
async def test_route_pin_only_update_does_not_pass_browser_profile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # SKY-12724 regression (Codex): a pin-only POST /credentials/{id}/update must NOT pass
    # browser_profile_id (its omitted default None) into the repo, which would unlink the saved profile.
    existing = SimpleNamespace(
        vault_type=CredentialVaultType.BITWARDEN,
        item_id="item_1",
        organization_id="o_test",
        browser_profile_id="bp_existing",
        credential_id="cred_1",
    )
    conflict = AsyncMock(return_value=existing)
    vault_service = SimpleNamespace(update_credential=AsyncMock(return_value=existing))
    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(credentials=SimpleNamespace(get_credential=AsyncMock(return_value=existing))),
        CREDENTIAL_VAULT_SERVICES={CredentialVaultType.BITWARDEN: vault_service},
        AGENT_FUNCTION=AgentFunction(),
    )
    monkeypatch.setattr(credentials_routes, "app", fake_app)
    monkeypatch.setattr(credentials_routes, "_update_credential_or_profile_conflict", conflict)
    monkeypatch.setattr(credentials_routes, "_clear_cached_totp_code_preview", lambda **kwargs: None)
    monkeypatch.setattr(credentials_routes, "_normalize_authenticator_totp_for_organization_or_raise", AsyncMock())
    monkeypatch.setattr(credentials_routes, "_convert_to_response", lambda c: c)

    req = CreateCredentialRequest(
        name="c",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "u@e.com", "password": "pw"},
        pin_saved_session_ip=True,
    )
    await credentials_routes.update_credential(
        background_tasks=BackgroundTasks(),
        credential_id="cred_1",
        data=req,
        current_org=SimpleNamespace(organization_id="o_test"),  # type: ignore[arg-type]
    )

    conflict.assert_awaited_once()
    assert "browser_profile_id" not in conflict.await_args.kwargs
    assert conflict.await_args.kwargs["pin_saved_session_ip"] is True


@pytest.mark.asyncio
async def test_repo_update_credential_generates_proxy_session_id_for_proxy_location() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = None
    mock_credential.proxy_session_id = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        )

    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_credential.proxy_session_id is not None
    assert is_proxy_session_id(mock_credential.proxy_session_id)


@pytest.mark.asyncio
async def test_repo_update_credential_rotates_proxy_session_id_when_requested() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "existing-pin"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
            rotate_proxy_session_id=True,
        )

    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_credential.proxy_session_id is not None
    assert mock_credential.proxy_session_id != "existing-pin"
    assert is_proxy_session_id(mock_credential.proxy_session_id)


@pytest.mark.asyncio
async def test_repo_update_credential_vault_data_persists_proxy_pin() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = None
    mock_credential.proxy_session_id = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential_vault_data(
            credential_id="cred_123",
            organization_id="org_123",
            item_id="item_123",
            name="test",
            credential_type="password",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        )

    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_credential.proxy_session_id is not None
    assert is_proxy_session_id(mock_credential.proxy_session_id)


@pytest.mark.asyncio
async def test_repo_update_credential_vault_data_writes_tested_url_only_when_provided() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.tested_url = "https://old.example.com/login"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential_vault_data(
            credential_id="cred_123",
            organization_id="org_123",
            item_id="item_123",
            name="test",
            credential_type="password",
        )
        assert mock_credential.tested_url == "https://old.example.com/login"

        await repo.update_credential_vault_data(
            credential_id="cred_123",
            organization_id="org_123",
            item_id="item_123",
            name="test",
            credential_type="password",
            tested_url="https://new.example.com/login",
        )
        assert mock_credential.tested_url == "https://new.example.com/login"


@pytest.mark.asyncio
async def test_repo_update_credential_preserves_existing_proxy_pin_on_resave() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "support-shared-login@example.com"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        )

    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_credential.proxy_session_id == "support-shared-login@example.com"


@pytest.mark.asyncio
async def test_repo_update_credential_explicit_null_session_id_clears_location() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "abc1234567"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_session_id=None,
        )

    assert mock_credential.proxy_location is None
    assert mock_credential.proxy_session_id is None


@pytest.mark.asyncio
async def test_repo_update_credential_non_isp_location_clears_stale_proxy_pin() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "abc1234567"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL,
        )

    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL.value
    assert mock_credential.proxy_session_id is None


@pytest.mark.asyncio
async def test_repo_update_browser_profile_preserves_existing_proxy_pin_on_resave() -> None:
    mock_profile = MagicMock()
    mock_profile.name = "Profile"
    mock_profile.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_profile.proxy_session_id = "support-shared-login@example.com"
    repo = _make_browser_sessions_repo(mock_profile)

    with patch("skyvern.forge.sdk.schemas.browser_profiles.BrowserProfile.model_validate", return_value=MagicMock()):
        await repo.update_browser_profile(
            profile_id="bp_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        )

    assert mock_profile.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_profile.proxy_session_id == "support-shared-login@example.com"


@pytest.mark.asyncio
async def test_repo_update_browser_profile_rotates_proxy_session_id_when_requested() -> None:
    mock_profile = MagicMock()
    mock_profile.name = "Profile"
    mock_profile.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_profile.proxy_session_id = "existing-pin"
    repo = _make_browser_sessions_repo(mock_profile)

    with patch("skyvern.forge.sdk.schemas.browser_profiles.BrowserProfile.model_validate", return_value=MagicMock()):
        await repo.update_browser_profile(
            profile_id="bp_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
            rotate_proxy_session_id=True,
        )

    assert mock_profile.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_profile.proxy_session_id is not None
    assert mock_profile.proxy_session_id != "existing-pin"
    assert is_proxy_session_id(mock_profile.proxy_session_id)


@pytest.mark.asyncio
async def test_repo_update_browser_profile_explicit_null_session_id_clears_location() -> None:
    mock_profile = MagicMock()
    mock_profile.name = "Profile"
    mock_profile.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_profile.proxy_session_id = "abc1234567"
    repo = _make_browser_sessions_repo(mock_profile)

    with patch("skyvern.forge.sdk.schemas.browser_profiles.BrowserProfile.model_validate", return_value=MagicMock()):
        await repo.update_browser_profile(
            profile_id="bp_123",
            organization_id="org_123",
            proxy_session_id=None,
        )

    assert mock_profile.proxy_location is None
    assert mock_profile.proxy_session_id is None


@pytest.mark.asyncio
async def test_repo_update_browser_profile_non_isp_location_clears_stale_proxy_pin() -> None:
    mock_profile = MagicMock()
    mock_profile.name = "Profile"
    mock_profile.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_profile.proxy_session_id = "abc1234567"
    repo = _make_browser_sessions_repo(mock_profile)

    with patch("skyvern.forge.sdk.schemas.browser_profiles.BrowserProfile.model_validate", return_value=MagicMock()):
        await repo.update_browser_profile(
            profile_id="bp_123",
            organization_id="org_123",
            proxy_location=ProxyLocation.RESIDENTIAL,
        )

    assert mock_profile.proxy_location == ProxyLocation.RESIDENTIAL.value
    assert mock_profile.proxy_session_id is None


@pytest.mark.asyncio
async def test_repo_create_browser_profile_clears_incompatible_proxy_pin() -> None:
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))

    with patch("skyvern.forge.sdk.schemas.browser_profiles.BrowserProfile.model_validate", return_value=MagicMock()):
        await repo.create_browser_profile(
            organization_id="org_123",
            name="Profile",
            proxy_location=ProxyLocation.NONE,
            proxy_session_id="abc1234567",
        )

    stored_profile = mock_session.add.call_args.args[0]
    assert stored_profile.proxy_location == ProxyLocation.NONE.value
    assert stored_profile.proxy_session_id is None


@pytest.mark.asyncio
async def test_browser_profile_resave_updates_proxy_pin_after_storage_write(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def store_browser_profile(**_kwargs: object) -> None:
        events.append("store")

    async def update_browser_profile(**_kwargs: object) -> SimpleNamespace:
        events.append("update_profile")
        return SimpleNamespace()

    async def mark_verified_login(**_kwargs: object) -> None:
        events.append("mark_verified")

    async def update_credential(**_kwargs: object) -> SimpleNamespace:
        events.append("update_credential")
        return SimpleNamespace()

    monkeypatch.setattr(
        forge_app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowRunStatus.completed, browser_profile_id=None, browser_sink_profile_id=None
            )
        ),
    )
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/session"))
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", store_browser_profile)
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(
            return_value=SimpleNamespace(
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                proxy_session_id="abc1234567",
            )
        ),
    )
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_existing")),
    )
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "update_browser_profile", update_browser_profile)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "mark_verified_login", mark_verified_login)

    await credentials_routes._create_browser_profile_after_workflow(
        credential_id="cred_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wp_123",
        organization_id="org_123",
        credential_name="test",
        test_url="https://example.com/login",
        existing_browser_profile_id="bp_existing",
    )

    assert events == ["store", "update_profile", "mark_verified", "update_credential"]


@pytest.mark.asyncio
async def test_browser_profile_resave_leaves_existing_profile_pin_when_credential_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def store_browser_profile(**_kwargs: object) -> None:
        events.append("store")

    async def update_browser_profile(**_kwargs: object) -> SimpleNamespace:
        events.append("update_profile")
        return SimpleNamespace()

    async def mark_verified_login(**_kwargs: object) -> None:
        events.append("mark_verified")

    async def update_credential(**_kwargs: object) -> SimpleNamespace:
        events.append("update_credential")
        return SimpleNamespace()

    monkeypatch.setattr(
        forge_app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowRunStatus.completed, browser_profile_id=None, browser_sink_profile_id=None
            )
        ),
    )
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/session"))
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", store_browser_profile)
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(proxy_location=None, proxy_session_id=None)),
    )
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_existing")),
    )
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "update_browser_profile", update_browser_profile)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "mark_verified_login", mark_verified_login)

    await credentials_routes._create_browser_profile_after_workflow(
        credential_id="cred_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wp_123",
        organization_id="org_123",
        credential_name="test",
        test_url="https://example.com/login",
        existing_browser_profile_id="bp_existing",
    )

    assert events == ["store", "mark_verified", "update_credential"]


@pytest.mark.asyncio
async def test_browser_profile_resave_preserves_different_existing_profile_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def store_browser_profile(**_kwargs: object) -> None:
        events.append("store")

    async def update_browser_profile(**_kwargs: object) -> SimpleNamespace:
        events.append("update_profile")
        return SimpleNamespace()

    async def mark_verified_login(**_kwargs: object) -> None:
        events.append("mark_verified")

    async def update_credential(**_kwargs: object) -> SimpleNamespace:
        events.append("update_credential")
        return SimpleNamespace()

    monkeypatch.setattr(
        forge_app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowRunStatus.completed, browser_profile_id=None, browser_sink_profile_id=None
            )
        ),
    )
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/session"))
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", store_browser_profile)
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(
            return_value=SimpleNamespace(
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                proxy_session_id="credential-pin",
            )
        ),
    )
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_existing", proxy_session_id="profile-pin")),
    )
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "update_browser_profile", update_browser_profile)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "mark_verified_login", mark_verified_login)

    await credentials_routes._create_browser_profile_after_workflow(
        credential_id="cred_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wp_123",
        organization_id="org_123",
        credential_name="test",
        test_url="https://example.com/login",
        existing_browser_profile_id="bp_existing",
    )

    assert events == ["store", "mark_verified", "update_credential"]


@pytest.mark.asyncio
async def test_credential_browser_profile_save_reads_managed_profile_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_browser_profile = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_created"))
    update_credential = AsyncMock(return_value=SimpleNamespace())
    store_browser_profile = AsyncMock()
    retrieve_profile = AsyncMock(return_value="/tmp/managed_profile")
    retrieve_session = AsyncMock()

    monkeypatch.setattr(
        forge_app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowRunStatus.completed,
                browser_profile_id="bp_managed",
                workflow_permanent_id="wpid_123",
                browser_sink_profile_id=None,
            )
        ),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.workflows,
        "get_workflow",
        AsyncMock(return_value=SimpleNamespace(workflow_permanent_id="wpid_123")),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=True, workflow_permanent_id="wpid_123")),
    )
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_profile", retrieve_profile)
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_session", retrieve_session)
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", store_browser_profile)
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(proxy_location=None, proxy_session_id=None)),
    )
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "create_browser_profile", create_browser_profile)

    await credentials_routes._create_browser_profile_after_workflow(
        credential_id="cred_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        organization_id="org_123",
        credential_name="test",
        test_url="https://example.com/login",
    )

    retrieve_profile.assert_awaited_once_with(organization_id="org_123", profile_id="bp_managed")
    retrieve_session.assert_not_awaited()
    store_browser_profile.assert_awaited_once_with(
        organization_id="org_123",
        profile_id="bp_created",
        directory="/tmp/managed_profile",
    )
    update_credential.assert_awaited_once()


@pytest.mark.asyncio
async def test_credential_browser_profile_save_falls_back_to_legacy_archive_for_managed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_browser_profile = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_created"))
    update_credential = AsyncMock(return_value=SimpleNamespace())
    store_browser_profile = AsyncMock()
    retrieve_profile = AsyncMock(return_value=None)
    retrieve_session = AsyncMock(return_value="/tmp/legacy_session")
    get_storage_key = AsyncMock(return_value="wpid_123")

    monkeypatch.setattr(
        forge_app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(
            return_value=SimpleNamespace(
                status=WorkflowRunStatus.completed,
                browser_profile_id="bp_managed",
                workflow_permanent_id="wpid_123",
                browser_sink_profile_id=None,
            )
        ),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.workflows,
        "get_workflow",
        AsyncMock(return_value=SimpleNamespace(workflow_permanent_id="wpid_123")),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=True, workflow_permanent_id="wpid_123")),
    )
    monkeypatch.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_browser_session_storage_key", get_storage_key)
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_profile", retrieve_profile)
    monkeypatch.setattr(forge_app.STORAGE, "retrieve_browser_session", retrieve_session)
    monkeypatch.setattr(forge_app.STORAGE, "store_browser_profile", store_browser_profile)
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(proxy_location=None, proxy_session_id=None)),
    )
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "create_browser_profile", create_browser_profile)

    await credentials_routes._create_browser_profile_after_workflow(
        credential_id="cred_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        organization_id="org_123",
        credential_name="test",
        test_url="https://example.com/login",
    )

    retrieve_profile.assert_awaited_once_with(organization_id="org_123", profile_id="bp_managed")
    retrieve_session.assert_awaited_once_with(organization_id="org_123", workflow_permanent_id="wpid_123")
    store_browser_profile.assert_awaited_once_with(
        organization_id="org_123",
        profile_id="bp_created",
        directory="/tmp/legacy_session",
    )


# --- rider: credential browser_profile_id (plain/unowned validation) + pin_saved_session_ip ---


@pytest.mark.asyncio
async def test_repo_update_credential_accepts_pin_saved_session_ip() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.pin_saved_session_ip = False
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            pin_saved_session_ip=True,
        )

    assert mock_credential.pin_saved_session_ip is True


@pytest.mark.asyncio
async def test_validate_browser_profile_id_rejects_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_app.DATABASE.browser_sessions, "get_browser_profile", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_browser_profile_id_rejects_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=True, workflow_permanent_id="wpid_x")),
    )
    with pytest.raises(HTTPException) as exc:
        await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_browser_profile_id_rejects_other_workflow_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain-looking profile still owned by a workflow (workflow_permanent_id set) is rejected.
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id="wpid_other")),
    )
    with pytest.raises(HTTPException) as exc:
        await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_browser_profile_id_rejects_other_credential_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[SimpleNamespace(credential_id="cred_other")]),
    )
    with pytest.raises(HTTPException) as exc:
        await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_browser_profile_id_accepts_plain_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),
    )
    # No raise.
    await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")


@pytest.mark.asyncio
async def test_validate_browser_profile_id_allows_relink_to_same_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[SimpleNamespace(credential_id="cred_self")]),
    )
    # Re-linking the profile a credential already owns is a no-op, not a conflict.
    await credentials_routes._validate_credential_browser_profile_id(
        "bp_x", "org_123", current_credential_id="cred_self"
    )


def _stored_credential(browser_profile_id: str | None = None, pin: bool = False) -> Credential:
    return Credential(
        credential_id="cred_123",
        organization_id="org_123",
        name="test",
        vault_type=CredentialVaultType.AZURE_VAULT,
        item_id="item_123",
        credential_type=CredentialType.PASSWORD,
        username="user@example.com",
        card_last4=None,
        card_brand=None,
        secret_label=None,
        browser_profile_id=browser_profile_id,
        pin_saved_session_ip=pin,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )


@pytest.mark.asyncio
async def test_create_credential_links_validated_browser_profile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    vault_service = SimpleNamespace(create_credential=AsyncMock(return_value=_stored_credential()))
    monkeypatch.setattr(credentials_routes, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),
    )
    update_credential = AsyncMock(return_value=_stored_credential(browser_profile_id="bp_plain", pin=True))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        browser_profile_id="bp_plain",
        pin_saved_session_ip=True,
    )

    response = await credentials_routes.create_credential(
        background_tasks=BackgroundTasks(),
        data=data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    update_credential.assert_awaited_once()
    kwargs = update_credential.await_args.kwargs
    assert kwargs["browser_profile_id"] == "bp_plain"
    assert kwargs["pin_saved_session_ip"] is True
    assert response.browser_profile_id == "bp_plain"
    assert response.pin_saved_session_ip is True


@pytest.mark.asyncio
async def test_create_credential_rejects_managed_browser_profile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    vault_create = AsyncMock(return_value=_stored_credential())
    vault_service = SimpleNamespace(create_credential=vault_create)
    monkeypatch.setattr(credentials_routes, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=True, workflow_permanent_id="wpid_x")),
    )
    update_credential = AsyncMock()
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        browser_profile_id="bp_managed",
    )

    with pytest.raises(HTTPException) as exc:
        await credentials_routes.create_credential(
            background_tasks=BackgroundTasks(),
            data=data,
            current_org=SimpleNamespace(organization_id="org_123"),
        )
    assert exc.value.status_code == 400
    # Validation rejects BEFORE provisioning — no orphan credential/vault item is created.
    vault_create.assert_not_awaited()
    update_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_credential_sets_pin_and_validated_browser_profile_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.schemas.credentials import UpdateCredentialRequest

    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=_stored_credential()),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),
    )
    update_credential = AsyncMock(return_value=_stored_credential(browser_profile_id="bp_plain", pin=True))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)

    data = UpdateCredentialRequest(browser_profile_id="bp_plain", pin_saved_session_ip=True)

    await credentials_routes.rename_credential(
        credential_id="cred_123",
        data=data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    kwargs = update_credential.await_args.kwargs
    assert kwargs["browser_profile_id"] == "bp_plain"
    assert kwargs["pin_saved_session_ip"] is True


def test_credential_route_treats_null_advanced_key_as_no_explicit_identity() -> None:
    update_kwargs: dict[str, object] = {}

    _apply_proxy_pin_update(
        update_kwargs,
        proxy_location_was_set=True,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id_was_set=True,
        proxy_session_id=None,
    )

    assert update_kwargs == {"proxy_location": ProxyLocation.RESIDENTIAL_ISP}


def test_credential_route_passes_rotate_proxy_session_id_intent() -> None:
    update_kwargs: dict[str, object] = {}

    _apply_proxy_pin_update(
        update_kwargs,
        proxy_location_was_set=True,
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        proxy_session_id_was_set=False,
        proxy_session_id=None,
        rotate_proxy_session_id=True,
    )

    assert update_kwargs == {
        "proxy_location": ProxyLocation.RESIDENTIAL_ISP,
        "rotate_proxy_session_id": True,
    }


@pytest.mark.asyncio
async def test_rename_credential_route_passes_tested_url(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _make_password_credential(tested_url="https://example.com/original")
    updated = _make_password_credential(name="renamed", tested_url="https://example.com/new")
    update_credential = AsyncMock(return_value=updated)
    monkeypatch.setattr(forge_app.DATABASE.credentials, "get_credential", AsyncMock(return_value=existing))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)

    response = await credentials_routes.rename_credential(
        credential_id="cred_123",
        data=credentials_routes.UpdateCredentialRequest(
            name="renamed",
            tested_url="https://example.com/new",
        ),
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    update_credential.assert_awaited_once()
    assert update_credential.await_args.kwargs["tested_url"] == "https://example.com/new"
    assert response.tested_url == "https://example.com/new"


def test_generate_proxy_session_id_rejects_empty_entity_id() -> None:
    with pytest.raises(ValueError, match="empty entity id"):
        generate_proxy_session_id("   ")


@pytest.mark.asyncio
async def test_repo_update_credential_clears_proxy_session_id_with_proxy_location() -> None:
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "abc1234567"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            proxy_location=None,
        )

    assert mock_credential.proxy_location is None
    assert mock_credential.proxy_session_id is None


@pytest.mark.asyncio
async def test_update_credential_route_applies_validated_browser_profile_and_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.schemas.credentials import NonEmptyPasswordCredential

    existing = _stored_credential()
    monkeypatch.setattr(forge_app.DATABASE.credentials, "get_credential", AsyncMock(return_value=existing))
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),
    )
    from skyvern.forge.forge_app import ForgeApp

    vault_service = SimpleNamespace(
        update_credential=AsyncMock(return_value=_stored_credential()),
        post_delete_credential_item=AsyncMock(),
    )
    monkeypatch.setattr(ForgeApp, "CREDENTIAL_VAULT_SERVICES", {existing.vault_type: vault_service}, raising=False)
    update_credential = AsyncMock(return_value=_stored_credential(browser_profile_id="bp_plain", pin=True))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(credentials_routes, "_clear_cached_totp_code_preview", lambda **_kwargs: None)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential=NonEmptyPasswordCredential(username="user@example.com", password="pw"),
        browser_profile_id="bp_plain",
        pin_saved_session_ip=True,
    )

    response = await credentials_routes.update_credential(
        background_tasks=BackgroundTasks(),
        credential_id="cred_123",
        data=data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    kwargs = update_credential.await_args.kwargs
    assert kwargs["browser_profile_id"] == "bp_plain"
    assert kwargs["pin_saved_session_ip"] is True
    assert response.browser_profile_id == "bp_plain"
    assert response.pin_saved_session_ip is True


@pytest.mark.asyncio
async def test_repo_update_credential_mints_proxy_session_when_pin_turned_on() -> None:
    # F4: turning the pin on with no proxy session provisions a residential-ISP sticky session, so the
    # pin is not a silent no-op. The create route routes pin_saved_session_ip through this same call.
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.pin_saved_session_ip = False
    mock_credential.proxy_location = None
    mock_credential.proxy_session_id = None
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            pin_saved_session_ip=True,
        )

    assert mock_credential.pin_saved_session_ip is True
    assert mock_credential.proxy_location == ProxyLocation.RESIDENTIAL_ISP.value
    assert mock_credential.proxy_session_id is not None
    assert is_proxy_session_id(mock_credential.proxy_session_id)


@pytest.mark.asyncio
async def test_repo_update_credential_pin_keeps_existing_proxy_session() -> None:
    # Turning the pin on when a proxy session already exists mints nothing new (idempotent).
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.pin_saved_session_ip = False
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "existing-pin"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            pin_saved_session_ip=True,
        )

    assert mock_credential.proxy_session_id == "existing-pin"


@pytest.mark.asyncio
async def test_repo_update_credential_disables_pin_without_reminting() -> None:
    # H: an explicit pin_saved_session_ip=False disables the pin (and never mints a proxy session).
    mock_credential = MagicMock()
    mock_credential.name = "test"
    mock_credential.pin_saved_session_ip = True
    mock_credential.proxy_location = ProxyLocation.RESIDENTIAL_ISP.value
    mock_credential.proxy_session_id = "existing-pin"
    repo = _make_credential_repo(mock_credential)

    with patch("skyvern.forge.sdk.schemas.credentials.Credential.model_validate", return_value=MagicMock()):
        await repo.update_credential(
            credential_id="cred_123",
            organization_id="org_123",
            pin_saved_session_ip=False,
        )

    assert mock_credential.pin_saved_session_ip is False
    assert mock_credential.proxy_session_id == "existing-pin"


@pytest.mark.asyncio
async def test_update_credential_conflict_translates_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # G: the atomic partial-unique backstop. A concurrent link that slips past the read-check hits the
    # DB unique constraint; the route translates it into the same 400 as the friendly read-check.
    from sqlalchemy.exc import IntegrityError

    orig = Exception('duplicate key value violates unique constraint "uq_credentials_browser_profile_id"')
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "update_credential",
        AsyncMock(side_effect=IntegrityError("UPDATE credentials", {}, orig)),
    )
    with pytest.raises(HTTPException) as exc:
        await credentials_routes._update_credential_or_profile_conflict(
            credential_id="cred_123", organization_id="org_123", browser_profile_id="bp_x"
        )
    assert exc.value.status_code == 400
    assert "already linked to another credential" in exc.value.detail


@pytest.mark.asyncio
async def test_update_credential_conflict_reraises_unrelated_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import IntegrityError

    orig = Exception('null value in column "name" violates not-null constraint')
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "update_credential",
        AsyncMock(side_effect=IntegrityError("UPDATE credentials", {}, orig)),
    )
    with pytest.raises(IntegrityError):
        await credentials_routes._update_credential_or_profile_conflict(
            credential_id="cred_123", organization_id="org_123", browser_profile_id="bp_x"
        )


@pytest.mark.asyncio
async def test_validate_browser_profile_id_relinks_when_owner_soft_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    # G amendment 1: a soft-deleted owner must NOT reserve the profile. The read-check queries live
    # credentials only (get_credentials_by_browser_profile_id filters deleted_at), and the partial-unique
    # index predicate matches (deleted_at IS NULL), so relinking is allowed.
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),  # the soft-deleted owner is filtered out by the repo
    )
    await credentials_routes._validate_credential_browser_profile_id("bp_x", "org_123")


@pytest.mark.asyncio
async def test_create_credential_profile_alone_does_not_reset_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    # K: supplying a browser_profile_id without a pin field must NOT reset the pin — pass None (leave
    # unchanged), not the request model's default False.
    vault_service = SimpleNamespace(create_credential=AsyncMock(return_value=_stored_credential()))
    monkeypatch.setattr(credentials_routes, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(
        forge_app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(is_managed=False, workflow_permanent_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(return_value=[]),
    )
    update_credential = AsyncMock(return_value=_stored_credential(browser_profile_id="bp_plain"))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)

    data = CreateCredentialRequest(
        name="test",
        credential_type=CredentialType.PASSWORD,
        credential={"username": "user@example.com", "password": "pw"},
        browser_profile_id="bp_plain",  # pin_saved_session_ip omitted
    )

    await credentials_routes.create_credential(
        background_tasks=BackgroundTasks(),
        data=data,
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    assert update_credential.await_args.kwargs["pin_saved_session_ip"] is None


def test_update_run_sequentially_documents_drain_before_enable_window() -> None:
    # The false->true toggle does not retroactively serialize runs that were already active when the
    # flag flipped on (they were never stamped with the credential). That adoption-window limitation is
    # documented on the field rather than enforced with a broad active-run scan; pin the warning so the
    # contract is not silently dropped.
    description = UpdateCredentialRequest.model_fields["run_sequentially"].description or ""
    assert "after this flag is enabled" in description
    assert "Drain" in description


def test_oss_agent_function_does_not_support_sequential_credentials() -> None:
    assert AgentFunction().supports_sequential_credentials() is False


@pytest.mark.asyncio
async def test_oss_route_rejects_enabling_sequential_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _make_password_credential(run_sequentially=False)
    update_credential = AsyncMock(return_value=_make_password_credential(run_sequentially=True))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "get_credential", AsyncMock(return_value=existing))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(supports_sequential_credentials=lambda: False),
    )

    with pytest.raises(HTTPException) as exc:
        await credentials_routes.rename_credential(
            credential_id="cred_123",
            data=UpdateCredentialRequest(run_sequentially=True),
            current_org=SimpleNamespace(organization_id="org_123"),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Sequential credential execution is not supported by this deployment"
    update_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_oss_route_allows_disabling_sequential_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _make_password_credential(run_sequentially=True)
    updated = _make_password_credential(run_sequentially=False)
    update_credential = AsyncMock(return_value=updated)
    monkeypatch.setattr(forge_app.DATABASE.credentials, "get_credential", AsyncMock(return_value=existing))
    monkeypatch.setattr(forge_app.DATABASE.credentials, "update_credential", update_credential)
    monkeypatch.setattr(
        forge_app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            supports_sequential_credentials=lambda: False,
            should_lock_credential_write=AsyncMock(return_value=False),
        ),
    )

    response = await credentials_routes.rename_credential(
        credential_id="cred_123",
        data=UpdateCredentialRequest(run_sequentially=False),
        current_org=SimpleNamespace(organization_id="org_123"),
    )

    assert response.run_sequentially is False
    assert update_credential.await_args.kwargs["run_sequentially"] is False
