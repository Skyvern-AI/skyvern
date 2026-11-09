import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import MicrosoftOAuthCredentialModel
from skyvern.forge.sdk.db.repositories.microsoft_oauth import (
    STATE_ACTIVE,
    STATE_REVOKED,
    IncompatibleScopeProfileError,
    MicrosoftOAuthRepository,
    PowerBIReconnectAccessDenied,
    ReconnectIdentityUnavailableError,
)
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.services.microsoft_oauth_service import POWERBI_DATASET_READ_SCOPES, is_powerbi_scope_profile
from tests.unit.conftest import SqliteEngineFactory


@pytest_asyncio.fixture
async def engine(sqlite_engine_factory: SqliteEngineFactory) -> AsyncEngine:
    return sqlite_engine_factory()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> MicrosoftOAuthRepository:
    db = BaseAlchemyDB(engine)
    return MicrosoftOAuthRepository(db.Session, debug_enabled=False)


async def _seed_active_connection(
    repo: MicrosoftOAuthRepository,
    credential_id: str,
    *,
    organization_id: str = "o_test",
    scopes: list[str] | None = None,
    scopes_granted: list[str] | None = None,
    tenant_id: str | None = "tenant-1",
    object_id: str | None = "object-1",
    initiating_user_id: str | None = "user-1",
) -> None:
    scopes = scopes or ["Mail.Read", "User.Read", "offline_access", "openid", "profile"]
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    nonce = f"nonce-{credential_id}"
    await repo.insert_pending_credential(
        credential_id=credential_id,
        organization_id=organization_id,
        credential_name="Default",
        scopes_requested=scopes,
        consent_nonce=nonce,
        consent_redirect_uri="https://app/callback",
        consent_expires_at=now + datetime.timedelta(minutes=10),
        consent_code_verifier=f"verifier-{credential_id}",
    )
    await repo.promote_pending_to_active(
        organization_id=organization_id,
        nonce=nonce,
        encrypted_refresh_token=f"cipher-{credential_id}",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=scopes_granted or scopes,
        initiating_user_id=initiating_user_id,
        tenant_id=tenant_id,
        object_id=object_id,
        now=now,
    )


@pytest.mark.asyncio
async def test_reauthorization_promotes_existing_connection_in_place_and_replaces_nonce(
    repo: MicrosoftOAuthRepository, engine: AsyncEngine
) -> None:
    await _seed_active_connection(repo, "msoac_reconnect")
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    common = {
        "credential_id": "msoac_reconnect",
        "organization_id": "o_test",
        "consent_redirect_uri": "https://app/callback",
        "consent_expires_at": now + datetime.timedelta(minutes=10),
        "now": now,
        "outlook_scopes": ["Mail.Read", "offline_access", "openid", "profile", "User.Read"],
        "powerbi_scopes": [
            "https://analysis.windows.net/powerbi/api/Dataset.Read.All",
            "offline_access",
            "openid",
        ],
        "requesting_user_id": "user-1",
    }
    first = await repo.begin_reauthorization(
        consent_nonce="nonce-first",
        consent_code_verifier="verifier-first",
        **common,
    )
    second = await repo.begin_reauthorization(
        consent_nonce="nonce-second",
        consent_code_verifier="verifier-second",
        **common,
    )

    assert first is not None and second is not None
    assert (first.id, second.id, second.state) == ("msoac_reconnect", "msoac_reconnect", STATE_ACTIVE)
    assert await repo.load_pending_by_nonce("o_test", "nonce-first") is None
    context = await repo.load_pending_by_nonce("o_test", "nonce-second")
    assert context is not None
    assert (context.state, context.tenant_id, context.object_id) == (STATE_ACTIVE, "tenant-1", "object-1")
    assert context.scopes_granted == ["Mail.Read", "User.Read", "offline_access", "openid", "profile"]
    payload = await repo.load_active_ciphertext("o_test", "msoac_reconnect")
    assert payload is not None
    assert payload.encrypted_refresh_token == "cipher-msoac_reconnect"

    promoted = await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-second",
        encrypted_refresh_token="cipher-new",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["Mail.Read", "User.Read", "offline_access"],
        initiating_user_id="user-2",
        tenant_id="tenant-1",
        object_id="object-1",
        now=now,
    )

    assert (promoted.id, promoted.state) == ("msoac_reconnect", STATE_ACTIVE)
    async with engine.connect() as conn:
        initiator = await conn.scalar(
            select(MicrosoftOAuthCredentialModel.initiating_user_id).where(
                MicrosoftOAuthCredentialModel.id == "msoac_reconnect"
            )
        )
    assert initiator == "user-1"
    payload = await repo.load_active_ciphertext("o_test", "msoac_reconnect")
    assert payload is not None
    assert payload.encrypted_refresh_token == "cipher-new"


@pytest.mark.asyncio
async def test_powerbi_reauthorization_keeps_the_connection_readable_when_email_was_granted(
    repo: MicrosoftOAuthRepository,
) -> None:
    powerbi_scopes = list(POWERBI_DATASET_READ_SCOPES)
    await _seed_active_connection(
        repo,
        "msoac_powerbi_email",
        scopes=powerbi_scopes,
        scopes_granted=["Dataset.Read.All", "offline_access", "openid", "profile", "email"],
        initiating_user_id="owner",
    )
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

    started = await repo.begin_reauthorization(
        credential_id="msoac_powerbi_email",
        organization_id="o_test",
        consent_nonce="nonce-powerbi",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=now + datetime.timedelta(minutes=10),
        consent_code_verifier="verifier-powerbi",
        now=now,
        outlook_scopes=["Mail.Read"],
        powerbi_scopes=powerbi_scopes,
        requesting_user_id="owner",
    )

    assert started is not None
    assert "email" not in started.scopes_requested
    assert is_powerbi_scope_profile(started.scopes_requested, started.scopes_granted)


@pytest.mark.asyncio
async def test_reauthorization_rejects_incompatible_legacy_and_unowned_connections(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    await _seed_active_connection(repo, "msoac_outlook")
    await _seed_active_connection(repo, "msoac_legacy", tenant_id=None, object_id=None)
    powerbi_scopes = ["https://analysis.windows.net/powerbi/api/Dataset.Read.All", "offline_access", "openid"]
    await _seed_active_connection(repo, "msoac_powerbi", scopes=powerbi_scopes, initiating_user_id="owner")
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    common = {
        "organization_id": "o_test",
        "consent_nonce": "nonce-new",
        "consent_redirect_uri": "https://app/callback",
        "consent_expires_at": now + datetime.timedelta(minutes=10),
        "consent_code_verifier": "verifier-new",
        "now": now,
        "outlook_scopes": ["Mail.Read", "offline_access", "openid", "profile", "User.Read"],
        "powerbi_scopes": powerbi_scopes,
    }

    with pytest.raises(IncompatibleScopeProfileError):
        await repo.begin_reauthorization(
            credential_id="msoac_outlook",
            requested_scopes=powerbi_scopes,
            requesting_user_id="user-1",
            **common,
        )
    with pytest.raises(ReconnectIdentityUnavailableError):
        await repo.begin_reauthorization(
            credential_id="msoac_legacy",
            requested_scopes=None,
            requesting_user_id="user-1",
            **common,
        )
    with pytest.raises(PowerBIReconnectAccessDenied):
        await repo.begin_reauthorization(
            credential_id="msoac_powerbi",
            requested_scopes=None,
            requesting_user_id="other-user",
            **common,
        )
    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="msoac_outlook",
        now=now,
    )
    assert (
        await repo.begin_reauthorization(
            credential_id="msoac_outlook",
            requested_scopes=None,
            requesting_user_id="user-1",
            **common,
        )
        is None
    )
    assert (
        await repo.begin_reauthorization(
            credential_id="msoac_powerbi",
            organization_id="o_other",
            requested_scopes=None,
            requesting_user_id="owner",
            **{key: value for key, value in common.items() if key != "organization_id"},
        )
        is None
    )

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    MicrosoftOAuthCredentialModel.id,
                    MicrosoftOAuthCredentialModel.consent_nonce,
                    MicrosoftOAuthCredentialModel.encrypted_refresh_token,
                ).where(MicrosoftOAuthCredentialModel.id.in_(["msoac_outlook", "msoac_legacy", "msoac_powerbi"]))
            )
        ).all()
    assert {row.id: (row.consent_nonce, row.encrypted_refresh_token) for row in rows} == {
        "msoac_outlook": (None, None),
        "msoac_legacy": (None, "cipher-msoac_legacy"),
        "msoac_powerbi": (None, "cipher-msoac_powerbi"),
    }


@pytest.mark.asyncio
async def test_update_email_address_only_if_null_does_not_overwrite_existing_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="fresh@example.test",
                created_at=modified_at,
                modified_at=modified_at,
            )
        )

    updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="msoac_email",
        email_address="stale@example.test",
        only_if_null=True,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    MicrosoftOAuthCredentialModel.email_address,
                    MicrosoftOAuthCredentialModel.modified_at,
                ).where(MicrosoftOAuthCredentialModel.id == "msoac_email")
            )
        ).one()

    assert stored.email_address == "fresh@example.test"
    assert stored.modified_at == modified_at
    assert updated is False


@pytest.mark.asyncio
async def test_update_email_address_authoritative_write_updates_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="old@example.test",
                created_at=modified_at,
                modified_at=modified_at,
            )
        )

    updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="msoac_email",
        email_address="fresh@example.test",
        only_if_null=False,
    )

    async with engine.connect() as conn:
        stored_address = (
            await conn.execute(
                select(MicrosoftOAuthCredentialModel.email_address).where(
                    MicrosoftOAuthCredentialModel.id == "msoac_email"
                )
            )
        ).scalar_one()

    assert stored_address == "fresh@example.test"
    assert updated is True


@pytest.mark.asyncio
async def test_powerbi_rows_are_listed_and_managed_only_by_their_owner(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.fromisoformat("2026-10-28T12:00:00")
    powerbi_scopes = ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"]
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert(),
            [
                {
                    "id": "powerbi_owner",
                    "organization_id": "o_test",
                    "credential_name": "Owner Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": "pbi-encrypted-token",
                    "initiating_user_id": "user-a",
                    "connection_id": "powerbi_owner",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "powerbi_ownerless",
                    "organization_id": "o_test",
                    "credential_name": "Ownerless Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": None,
                    "connection_id": "powerbi_ownerless",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "powerbi_synthetic_owner",
                    "organization_id": "o_test",
                    "credential_name": "Synthetic Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": "o_test_user",
                    "connection_id": "powerbi_synthetic_owner",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "outlook_legacy",
                    "organization_id": "o_test",
                    "credential_name": "Legacy Outlook",
                    "state": STATE_ACTIVE,
                    "scopes_requested": ["Mail.Read"],
                    "scopes_granted": ["Mail.Read"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": "user-a",
                    "connection_id": None,
                    "created_at": now,
                    "modified_at": now,
                },
            ],
        )

    owner_rows = await repo.list_active_for_org("o_test", user_id="user-a")
    other_rows = await repo.list_active_for_org("o_test", user_id="user-b")
    ownerless_rows = await repo.list_active_for_org("o_test")
    synthetic_rows = await repo.list_active_for_org("o_test", user_id="o_test_user")
    owner_metadata = await repo.load_active_access_metadata("o_test", "powerbi_owner")

    assert {row.id for row in owner_rows} == {"powerbi_owner", "outlook_legacy"}
    assert {row.id for row in other_rows} == {"outlook_legacy"}
    assert {row.id for row in ownerless_rows} == {"outlook_legacy"}
    assert {row.id for row in synthetic_rows} == {"outlook_legacy"}
    assert await repo.can_manage_credential("o_test", "powerbi_owner", "user-a") is True
    assert await repo.can_manage_credential("o_test", "powerbi_owner", "user-b") is False
    assert await repo.can_manage_credential("o_test", "powerbi_ownerless", "user-a") is False
    assert await repo.can_manage_credential("o_test", "powerbi_synthetic_owner", "o_test_user") is False
    assert await repo.can_manage_credential("o_test", "outlook_legacy", "user-b") is True
    assert await repo.can_manage_credential("o_other", "powerbi_owner", "user-a") is False
    assert owner_metadata is not None
    assert owner_metadata.initiating_user_id == "user-a"
    assert owner_metadata.connection_id == "powerbi_owner"
    assert await repo.load_active_access_metadata("o_other", "powerbi_owner") is None


@pytest.mark.asyncio
async def test_update_active_refresh_token_only_rewrites_expected_ciphertext(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_rotation",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                encrypted_refresh_token="encrypted-old",
                encrypted_method="aes",
                created_at=now,
                modified_at=now,
            )
        )

    rotated = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="msoac_rotation",
        encrypted_refresh_token="encrypted-rotated",
        encrypted_method=EncryptMethod.AES,
        expected_encrypted_refresh_token="encrypted-old",
        now=now + datetime.timedelta(minutes=1),
    )
    stale = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="msoac_rotation",
        encrypted_refresh_token="encrypted-stale",
        encrypted_method=EncryptMethod.AES,
        expected_encrypted_refresh_token="encrypted-old",
        now=now + datetime.timedelta(minutes=2),
    )

    async with engine.connect() as conn:
        stored = await conn.scalar(
            select(MicrosoftOAuthCredentialModel.encrypted_refresh_token).where(
                MicrosoftOAuthCredentialModel.id == "msoac_rotation"
            )
        )

    assert rotated is True
    assert stale is False
    assert stored == "encrypted-rotated"


@pytest.mark.asyncio
async def test_mark_revoked_and_scrub_clears_email_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_revoke_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="account@example.test",
                created_at=now,
                modified_at=now,
            )
        )

    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="msoac_revoke_email",
        now=now + datetime.timedelta(minutes=1),
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    MicrosoftOAuthCredentialModel.state,
                    MicrosoftOAuthCredentialModel.email_address,
                ).where(MicrosoftOAuthCredentialModel.id == "msoac_revoke_email")
            )
        ).one()

    assert stored.state == STATE_REVOKED
    assert stored.email_address is None
