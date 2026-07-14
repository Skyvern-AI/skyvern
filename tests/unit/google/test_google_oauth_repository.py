import datetime
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, GoogleOAuthCredentialModel  # noqa: F401 - registers model on Base
from skyvern.forge.sdk.db.repositories.google_oauth import (
    STATE_ACTIVE,
    STATE_ERROR,
    STATE_PENDING_CONSENT,
    STATE_REVOKED,
    GoogleOAuthRepository,
)
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> GoogleOAuthRepository:
    db = BaseAlchemyDB(engine)
    return GoogleOAuthRepository(db.Session, debug_enabled=False)


async def _seed_credentials_for_list_tests(engine: AsyncEngine) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert(),
            [
                {
                    "id": "gcred_active",
                    "organization_id": "o_test",
                    "credential_name": "Active",
                    "state": STATE_ACTIVE,
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "gcred_error",
                    "organization_id": "o_test",
                    "credential_name": "Error",
                    "state": STATE_ERROR,
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "gcred_pending",
                    "organization_id": "o_test",
                    "credential_name": "Pending",
                    "state": STATE_PENDING_CONSENT,
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "gcred_revoked",
                    "organization_id": "o_test",
                    "credential_name": "Revoked",
                    "state": STATE_REVOKED,
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "gcred_other_org",
                    "organization_id": "o_other",
                    "credential_name": "Other",
                    "state": STATE_ACTIVE,
                    "created_at": now,
                    "modified_at": now,
                },
            ],
        )


@pytest.mark.asyncio
async def test_list_visible_for_org_returns_active_and_error_only(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    await _seed_credentials_for_list_tests(engine)

    credentials = await repo.list_visible_for_org("o_test")

    assert {(credential.id, credential.state) for credential in credentials} == {
        ("gcred_active", STATE_ACTIVE),
        ("gcred_error", STATE_ERROR),
    }


@pytest.mark.asyncio
async def test_list_active_for_org_excludes_error(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    await _seed_credentials_for_list_tests(engine)

    credentials = await repo.list_active_for_org("o_test")

    assert [(credential.id, credential.state) for credential in credentials] == [("gcred_active", STATE_ACTIVE)]


@pytest.mark.asyncio
async def test_insert_pending_credential_returns_schema_without_greenlet_error(
    repo: GoogleOAuthRepository,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    result = await repo.insert_pending_credential(
        credential_id="gcred_abc",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-xyz",
        consent_redirect_uri="http://localhost:8080/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-abc",
    )

    assert isinstance(result, GoogleOAuthCredentialBase)
    assert result.id == "gcred_abc"
    assert result.organization_id == "o_test"
    assert result.credential_name == "Default"
    assert result.provider == "google"
    assert result.state == STATE_PENDING_CONSENT
    assert result.scopes_requested == ["https://www.googleapis.com/auth/spreadsheets"]
    assert result.scopes_granted == []
    assert result.created_at is not None
    assert result.modified_at is not None


@pytest.mark.asyncio
async def test_promote_pending_to_active_returns_schema_without_greenlet_error(
    repo: GoogleOAuthRepository,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_promote",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-promote",
        consent_redirect_uri="http://localhost:8080/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-promote",
    )

    result = await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-promote",
        encrypted_refresh_token="cipher-value",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        now=datetime.datetime.utcnow(),
    )

    assert isinstance(result, GoogleOAuthCredentialBase)
    assert result.id == "gcred_promote"
    assert result.state == "active"
    assert result.scopes_granted == ["https://www.googleapis.com/auth/spreadsheets"]


@pytest.mark.asyncio
async def test_rename_active_returns_schema_without_greenlet_error(
    repo: GoogleOAuthRepository,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_rename",
        organization_id="o_test",
        credential_name="Old Name",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-rename",
        consent_redirect_uri="http://localhost:8080/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-rename",
    )
    await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-rename",
        encrypted_refresh_token="cipher-value",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        now=datetime.datetime.utcnow(),
    )

    renamed = await repo.rename_active(
        organization_id="o_test",
        credential_id="gcred_rename",
        credential_name="New Name",
        now=datetime.datetime.utcnow(),
    )

    assert renamed is not None
    assert isinstance(renamed, GoogleOAuthCredentialBase)
    assert renamed.credential_name == "New Name"
    assert renamed.state == "active"


@pytest.mark.asyncio
async def test_consent_app_origin_round_trips_through_load_pending_by_nonce(
    repo: GoogleOAuthRepository,
) -> None:
    """consent_app_origin written by insert_pending_credential is returned by load_pending_by_nonce."""
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_app_origin",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-app-origin",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-app-origin",
        consent_app_origin="https://skyvern-cloud-git-branch-skyvern.vercel.app",
    )

    from skyvern.forge.sdk.db.repositories.google_oauth import PendingConsentContext

    ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-app-origin")
    assert ctx is not None
    assert isinstance(ctx, PendingConsentContext)
    assert ctx.consent_app_origin == "https://skyvern-cloud-git-branch-skyvern.vercel.app"


@pytest.mark.asyncio
async def test_consent_app_origin_defaults_to_none_for_backward_compat(
    repo: GoogleOAuthRepository,
) -> None:
    """Omitting consent_app_origin (pre-existing callers) stores and returns None."""
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_no_origin",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-no-origin",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-no-origin",
        # consent_app_origin intentionally omitted
    )

    ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-no-origin")
    assert ctx is not None
    assert ctx.consent_app_origin is None


@pytest.mark.asyncio
async def test_pending_client_id_round_trips_through_load_pending_by_nonce(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_client_id",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-client-id",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-client-id",
        client_id="client-old",
    )
    await repo.insert_pending_credential(
        credential_id="gcred_legacy_client_id",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-legacy-client-id",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-legacy-client-id",
    )

    async with engine.connect() as conn:
        stored_client_id = (
            await conn.execute(
                select(GoogleOAuthCredentialModel.client_id).where(
                    GoogleOAuthCredentialModel.id == "gcred_client_id",
                )
            )
        ).scalar_one()

    bound_ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-client-id")
    legacy_ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-legacy-client-id")

    assert stored_client_id == "client-old"
    assert bound_ctx is not None
    assert bound_ctx.client_id == "client-old"
    assert legacy_ctx is not None
    assert legacy_ctx.client_id is None


@pytest.mark.asyncio
async def test_load_active_ciphertext_returns_stored_client_id_and_legacy_none(
    repo: GoogleOAuthRepository,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_active_client_id",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-active-client-id",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-active-client-id",
        client_id="client-active",
    )
    await repo.insert_pending_credential(
        credential_id="gcred_active_legacy",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-active-legacy",
        consent_redirect_uri="https://app-staging.skyvern.com/integrations/google/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-active-legacy",
    )
    for nonce in ("nonce-active-client-id", "nonce-active-legacy"):
        await repo.promote_pending_to_active(
            organization_id="o_test",
            nonce=nonce,
            encrypted_refresh_token=f"cipher-{nonce}",
            encrypted_method=EncryptMethod.AES,
            scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
            now=datetime.datetime.utcnow(),
        )

    bound_payload = await repo.load_active_ciphertext(
        organization_id="o_test",
        credential_id="gcred_active_client_id",
    )
    legacy_payload = await repo.load_active_ciphertext(
        organization_id="o_test",
        credential_id="gcred_active_legacy",
    )

    assert bound_payload is not None
    assert bound_payload.client_id == "client-active"
    assert legacy_payload is not None
    assert legacy_payload.client_id is None


@pytest.mark.asyncio
async def test_load_pending_by_nonce_filters_expired_rows(
    repo: GoogleOAuthRepository,
) -> None:
    """Expired consent rows must not load — otherwise the callback exchanges Google's
    one-time auth code before the nonce is rejected, forcing the user to restart."""
    expired_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    await repo.insert_pending_credential(
        credential_id="gcred_expired",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce="nonce-expired",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=expired_at,
        consent_code_verifier="ver-expired",
    )

    ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-expired")
    assert ctx is None


@pytest.mark.asyncio
async def test_mark_active_mismatched_client_as_error_flips_only_mismatched_bound_active_rows(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    rows = [
        ("gcred_flip", "nonce-flip", "old"),
        ("gcred_match", "nonce-match", "new"),
        ("gcred_legacy", "nonce-legacy", None),
        ("gcred_pending", "nonce-pending", "old"),
        ("gcred_revoked", "nonce-revoked", "old"),
    ]
    for credential_id, nonce, client_id in rows:
        await repo.insert_pending_credential(
            credential_id=credential_id,
            organization_id="o_test",
            credential_name="Default",
            scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
            consent_nonce=nonce,
            consent_redirect_uri="https://app/callback",
            consent_expires_at=expires_at,
            consent_code_verifier=f"ver-{credential_id}",
            client_id=client_id,
        )
    for nonce in ("nonce-flip", "nonce-match", "nonce-legacy", "nonce-revoked"):
        await repo.promote_pending_to_active(
            organization_id="o_test",
            nonce=nonce,
            encrypted_refresh_token="cipher-value",
            encrypted_method=EncryptMethod.AES,
            scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
            now=datetime.datetime.utcnow(),
        )
    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="gcred_revoked",
        now=datetime.datetime.utcnow(),
    )

    changed = await repo.mark_active_mismatched_client_as_error(
        organization_id="o_test",
        new_client_id="new",
        now=datetime.datetime.utcnow(),
    )

    async with engine.connect() as conn:
        states = dict(
            (
                await conn.execute(
                    select(GoogleOAuthCredentialModel.id, GoogleOAuthCredentialModel.state).where(
                        GoogleOAuthCredentialModel.id.in_(
                            ["gcred_flip", "gcred_match", "gcred_legacy", "gcred_pending", "gcred_revoked"]
                        )
                    )
                )
            ).all()
        )

    assert changed == 1
    assert states == {
        "gcred_flip": STATE_ERROR,
        "gcred_match": STATE_ACTIVE,
        "gcred_legacy": STATE_ACTIVE,
        "gcred_pending": STATE_PENDING_CONSENT,
        "gcred_revoked": STATE_REVOKED,
    }


@pytest.mark.asyncio
async def test_mark_active_mismatched_client_as_error_with_no_new_client_flips_all_bound_active_rows(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    rows = [
        ("gcred_bound_1", "nonce-bound-1", "old-1"),
        ("gcred_bound_2", "nonce-bound-2", "old-2"),
        ("gcred_unbound", "nonce-unbound", None),
    ]
    for credential_id, nonce, client_id in rows:
        await repo.insert_pending_credential(
            credential_id=credential_id,
            organization_id="o_test",
            credential_name="Default",
            scopes_requested=["https://www.googleapis.com/auth/spreadsheets"],
            consent_nonce=nonce,
            consent_redirect_uri="https://app/callback",
            consent_expires_at=expires_at,
            consent_code_verifier=f"ver-{credential_id}",
            client_id=client_id,
        )
        await repo.promote_pending_to_active(
            organization_id="o_test",
            nonce=nonce,
            encrypted_refresh_token="cipher-value",
            encrypted_method=EncryptMethod.AES,
            scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
            now=datetime.datetime.utcnow(),
        )

    changed = await repo.mark_active_mismatched_client_as_error(
        organization_id="o_test",
        new_client_id=None,
        now=datetime.datetime.utcnow(),
    )

    async with engine.connect() as conn:
        states = dict(
            (
                await conn.execute(
                    select(GoogleOAuthCredentialModel.id, GoogleOAuthCredentialModel.state).where(
                        GoogleOAuthCredentialModel.id.in_(["gcred_bound_1", "gcred_bound_2", "gcred_unbound"])
                    )
                )
            ).all()
        )

    assert changed == 2
    assert states == {
        "gcred_bound_1": STATE_ERROR,
        "gcred_bound_2": STATE_ERROR,
        "gcred_unbound": STATE_ACTIVE,
    }
