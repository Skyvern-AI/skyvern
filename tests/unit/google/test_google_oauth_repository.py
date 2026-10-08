import datetime
from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, GoogleOAuthCredentialModel  # noqa: F401 - registers model on Base
from skyvern.forge.sdk.db.repositories.google_oauth import (
    DISPATCH_FAILED,
    STATE_ACTIVE,
    STATE_ERROR,
    STATE_PENDING_CONSENT,
    STATE_REVOKED,
    GoogleOAuthRepository,
)
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.routes import google_oauth as google_oauth_routes
from skyvern.forge.sdk.schemas.google_oauth import (
    CreateGoogleOAuthAuthorizeRequest,
    CreateGoogleOAuthCallbackRequest,
    GoogleOAuthCredentialBase,
)
from skyvern.forge.sdk.services import google_oauth_service


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

    await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_rename",
        now=datetime.datetime.utcnow(),
    )
    renamed_while_expired = await repo.rename_active(
        organization_id="o_test",
        credential_id="gcred_rename",
        credential_name="Reconnect Me",
        now=datetime.datetime.utcnow(),
    )

    assert renamed_while_expired is not None
    assert renamed_while_expired.credential_name == "Reconnect Me"
    assert renamed_while_expired.state == STATE_ERROR


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


async def _seed_active_credential(
    repo: GoogleOAuthRepository,
    credential_id: str,
    nonce: str,
    *,
    client_id: str | None = None,
    scopes: list[str] | None = None,
) -> None:
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id=credential_id,
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=scopes or ["https://www.googleapis.com/auth/spreadsheets"],
        consent_nonce=nonce,
        consent_redirect_uri="https://app/callback",
        consent_expires_at=expires_at,
        consent_code_verifier=f"ver-{credential_id}",
        client_id=client_id,
    )
    await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce=nonce,
        encrypted_refresh_token=f"cipher-{credential_id}",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=scopes or ["https://www.googleapis.com/auth/spreadsheets"],
        now=datetime.datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_begin_reauthorization_stamps_consent_without_disturbing_live_token(
    repo: GoogleOAuthRepository,
) -> None:
    await _seed_active_credential(repo, "gcred_reauth", "nonce-initial", client_id="client-old")
    reauth_at = datetime.datetime.utcnow()

    result = await repo.begin_reauthorization(
        credential_id="gcred_reauth",
        organization_id="o_test",
        consent_nonce="nonce-reauth",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=reauth_at + datetime.timedelta(minutes=10),
        consent_code_verifier="ver-reauth",
        now=reauth_at,
        consent_app_origin="https://app",
        client_id="client-new",
    )

    assert result is not None
    assert result.id == "gcred_reauth"
    # State is untouched and the live token still resolves, so referencing workflows keep working.
    assert result.state == STATE_ACTIVE
    payload = await repo.load_active_ciphertext(organization_id="o_test", credential_id="gcred_reauth")
    assert payload is not None
    assert payload.encrypted_refresh_token == "cipher-gcred_reauth"
    # The new consent challenge is now loadable by its nonce for the callback.
    ctx = await repo.load_pending_by_nonce(organization_id="o_test", nonce="nonce-reauth")
    assert ctx is not None
    assert ctx.credential_id == "gcred_reauth"
    assert ctx.consent_code_verifier == "ver-reauth"
    assert ctx.client_id == "client-new"


@pytest.mark.asyncio
async def test_begin_reauthorization_persists_granted_scopes_for_legacy_credential(
    repo: GoogleOAuthRepository,
) -> None:
    gmail_scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
    await repo.insert_pending_credential(
        credential_id="gcred_legacy_scopes",
        organization_id="o_test",
        credential_name="Default",
        scopes_requested=[],
        consent_nonce="nonce-initial",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-initial",
    )
    await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-initial",
        encrypted_refresh_token="cipher-legacy",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=gmail_scopes,
        now=datetime.datetime.utcnow(),
    )

    result = await repo.begin_reauthorization(
        credential_id="gcred_legacy_scopes",
        organization_id="o_test",
        consent_nonce="nonce-reauth",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=expires_at,
        consent_code_verifier="ver-reauth",
        now=datetime.datetime.utcnow(),
        requested_scopes=None,
        fallback_scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    assert result is not None
    assert result.scopes_requested == gmail_scopes


@pytest.mark.asyncio
async def test_begin_reauthorization_promotes_in_place_preserving_id(
    repo: GoogleOAuthRepository,
) -> None:
    await _seed_active_credential(repo, "gcred_inplace", "nonce-initial")
    await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_inplace",
        now=datetime.datetime.utcnow(),
    )
    reauth_at = datetime.datetime.utcnow()
    await repo.begin_reauthorization(
        credential_id="gcred_inplace",
        organization_id="o_test",
        consent_nonce="nonce-reauth",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=reauth_at + datetime.timedelta(minutes=10),
        consent_code_verifier="ver-reauth",
        now=reauth_at,
    )

    promoted = await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-reauth",
        encrypted_refresh_token="cipher-rotated",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        now=datetime.datetime.utcnow(),
    )

    assert promoted.id == "gcred_inplace"
    assert promoted.state == STATE_ACTIVE
    payload = await repo.load_active_ciphertext(organization_id="o_test", credential_id="gcred_inplace")
    assert payload is not None
    assert payload.encrypted_refresh_token == "cipher-rotated"


@pytest.mark.asyncio
async def test_begin_reauthorization_returns_none_for_non_reauthorizable_rows(
    repo: GoogleOAuthRepository,
) -> None:
    await _seed_active_credential(repo, "gcred_ok", "nonce-ok")
    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="gcred_ok",
        now=datetime.datetime.utcnow(),
    )
    now = datetime.datetime.utcnow()
    common = dict(
        organization_id="o_test",
        consent_nonce="nonce-x",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=now + datetime.timedelta(minutes=10),
        consent_code_verifier="ver-x",
        now=now,
    )

    revoked = await repo.begin_reauthorization(credential_id="gcred_ok", **common)
    missing = await repo.begin_reauthorization(credential_id="gcred_missing", **common)

    assert revoked is None
    assert missing is None


@pytest.mark.asyncio
async def test_mark_needs_reconnect_flips_active_only(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    await _seed_active_credential(repo, "gcred_active", "nonce-active")
    await _seed_active_credential(repo, "gcred_revoked", "nonce-revoked")
    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="gcred_revoked",
        now=datetime.datetime.utcnow(),
    )

    flipped = await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_active",
        now=datetime.datetime.utcnow(),
    )
    # Second call is a no-op: the row is already error, not active.
    flipped_again = await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_active",
        now=datetime.datetime.utcnow(),
    )
    revoked_noop = await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_revoked",
        now=datetime.datetime.utcnow(),
    )

    assert flipped == "gcred_active"
    assert flipped_again is None
    assert revoked_noop is None
    async with engine.connect() as conn:
        states = dict(
            (
                await conn.execute(
                    select(GoogleOAuthCredentialModel.id, GoogleOAuthCredentialModel.state).where(
                        GoogleOAuthCredentialModel.id.in_(["gcred_active", "gcred_revoked"])
                    )
                )
            ).all()
        )
    assert states == {"gcred_active": STATE_ERROR, "gcred_revoked": STATE_REVOKED}


@pytest.mark.asyncio
async def test_stale_refresh_cannot_expire_reauthorized_credential(
    repo: GoogleOAuthRepository,
) -> None:
    await _seed_active_credential(repo, "gcred_race", "nonce-initial")
    stale_payload = await repo.load_active_ciphertext(
        organization_id="o_test",
        credential_id="gcred_race",
    )
    assert stale_payload is not None

    reauth_at = stale_payload.credential_version + datetime.timedelta(seconds=1)
    await repo.begin_reauthorization(
        credential_id="gcred_race",
        organization_id="o_test",
        consent_nonce="nonce-reauth",
        consent_redirect_uri="https://app/callback",
        consent_expires_at=reauth_at + datetime.timedelta(minutes=10),
        consent_code_verifier="ver-reauth",
        now=reauth_at,
    )
    await repo.promote_pending_to_active(
        organization_id="o_test",
        nonce="nonce-reauth",
        encrypted_refresh_token="cipher-new",
        encrypted_method=EncryptMethod.AES,
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        now=reauth_at + datetime.timedelta(seconds=1),
    )

    flipped = await repo.mark_needs_reconnect(
        organization_id="o_test",
        credential_id="gcred_race",
        now=reauth_at + datetime.timedelta(seconds=2),
        expected_version=stale_payload.credential_version,
    )

    assert flipped is None
    visible = await repo.list_visible_for_org("o_test")
    assert visible[0].state == STATE_ACTIVE


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


@pytest.mark.asyncio
async def test_update_email_address_only_if_null_does_not_overwrite_existing_address(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert().values(
                id="gcred_email",
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
        credential_id="gcred_email",
        email_address="stale@example.test",
        only_if_null=True,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.email_address,
                    GoogleOAuthCredentialModel.modified_at,
                ).where(GoogleOAuthCredentialModel.id == "gcred_email")
            )
        ).one()

    assert stored.email_address == "fresh@example.test"
    assert stored.modified_at == modified_at
    assert updated is False


@pytest.mark.asyncio
async def test_update_email_address_authoritative_write_preserves_cas_version(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert().values(
                id="gcred_email",
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
        credential_id="gcred_email",
        email_address="fresh@example.test",
        only_if_null=False,
        expected_version=modified_at,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.email_address,
                    GoogleOAuthCredentialModel.modified_at,
                ).where(GoogleOAuthCredentialModel.id == "gcred_email")
            )
        ).one()

    assert stored.email_address == "fresh@example.test"
    assert stored.modified_at == modified_at
    assert updated is True


@pytest.mark.asyncio
async def test_update_email_address_backfill_stale_version_is_noop(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    current_version = datetime.datetime(2026, 7, 30, 12, 5, 0)
    stale_version = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert().values(
                id="gcred_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address=None,
                created_at=stale_version,
                modified_at=current_version,
            )
        )

    updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="gcred_email",
        email_address="stale@example.test",
        only_if_null=True,
        expected_version=stale_version,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.email_address,
                    GoogleOAuthCredentialModel.modified_at,
                ).where(GoogleOAuthCredentialModel.id == "gcred_email")
            )
        ).one()

    assert stored.email_address is None
    assert stored.modified_at == current_version
    assert updated is False


@pytest.mark.asyncio
async def test_mark_revoked_and_scrub_clears_email_address(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    await _seed_active_credential(repo, "gcred_revoke_email", "nonce-revoke-email")
    await repo.update_email_address(
        organization_id="o_test",
        credential_id="gcred_revoke_email",
        email_address="account@example.test",
        only_if_null=False,
    )

    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="gcred_revoke_email",
        now=datetime.datetime.utcnow(),
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.state,
                    GoogleOAuthCredentialModel.email_address,
                ).where(GoogleOAuthCredentialModel.id == "gcred_revoke_email")
            )
        ).one()

    assert stored.state == STATE_REVOKED
    assert stored.email_address is None


@pytest.mark.asyncio
async def test_update_active_refresh_token_uses_token_identity_guard_across_rename(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    credential_version = datetime.datetime(2026, 7, 30, 12, 0, 0)
    rotated_at = datetime.datetime(2026, 7, 30, 12, 5, 0)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert().values(
                id="gcred_rotation",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                encrypted_refresh_token="encrypted-old",
                encrypted_method=EncryptMethod.AES.value,
                created_at=credential_version,
                modified_at=credential_version,
            )
        )

    renamed = await repo.rename_active(
        organization_id="o_test",
        credential_id="gcred_rotation",
        credential_name="Renamed",
        now=datetime.datetime(2026, 7, 30, 12, 2, 0),
    )
    updated = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="gcred_rotation",
        encrypted_refresh_token="encrypted-rotated",
        encrypted_method=EncryptMethod.AES,
        now=rotated_at,
        expected_encrypted_refresh_token="encrypted-old",
    )
    stale_update = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="gcred_rotation",
        encrypted_refresh_token="encrypted-stale",
        encrypted_method=EncryptMethod.AES,
        now=datetime.datetime(2026, 7, 30, 12, 10, 0),
        expected_encrypted_refresh_token="encrypted-old",
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.encrypted_refresh_token,
                    GoogleOAuthCredentialModel.modified_at,
                ).where(GoogleOAuthCredentialModel.id == "gcred_rotation")
            )
        ).one()

    assert renamed is not None
    assert renamed.credential_name == "Renamed"
    assert updated is True
    assert stale_update is False
    assert stored.encrypted_refresh_token == "encrypted-rotated"
    assert stored.modified_at == rotated_at


@pytest.mark.asyncio
async def test_post_rotation_version_allows_email_backfill(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
) -> None:
    credential_version = datetime.datetime(2026, 7, 30, 12, 0, 0)
    rotated_at = datetime.datetime(2026, 7, 30, 12, 5, 0)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.insert().values(
                id="gcred_rotation_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                encrypted_refresh_token="encrypted-old",
                encrypted_method=EncryptMethod.AES.value,
                created_at=credential_version,
                modified_at=credential_version,
            )
        )

    rotated = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="gcred_rotation_email",
        encrypted_refresh_token="encrypted-rotated",
        encrypted_method=EncryptMethod.AES,
        now=rotated_at,
        expected_encrypted_refresh_token="encrypted-old",
    )
    email_updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="gcred_rotation_email",
        email_address="account@example.test",
        only_if_null=True,
        expected_version=rotated_at,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    GoogleOAuthCredentialModel.email_address,
                    GoogleOAuthCredentialModel.modified_at,
                ).where(GoogleOAuthCredentialModel.id == "gcred_rotation_email")
            )
        ).one()

    assert rotated is True
    assert email_updated is True
    assert stored.email_address == "account@example.test"
    assert stored.modified_at == rotated_at


GMAIL_SEND_SCOPE = google_oauth_service.GOOGLE_GMAIL_SEND_SCOPE
GMAIL_READONLY_SCOPE = google_oauth_service.GOOGLE_GMAIL_READONLY_SCOPE
IDENTITY_GRANT = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
SEND_GRANT = [GMAIL_SEND_SCOPE, *IDENTITY_GRANT]
READ_SEND_GRANT = [GMAIL_READONLY_SCOPE, *SEND_GRANT]


async def _seed_gmail_connection(
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
    scopes: list[str],
    *,
    email: str | None = "sender@example.test",
    subject: str | None = None,
) -> None:
    await _seed_active_credential(repo, "goac_gmail", "seed-nonce", scopes=scopes)
    async with engine.begin() as conn:
        await conn.execute(
            GoogleOAuthCredentialModel.__table__.update().values(email_address=email, google_subject=subject)
        )


async def _stored_connection(engine: AsyncEngine, credential_id: str = "goac_gmail") -> GoogleOAuthCredentialModel:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(GoogleOAuthCredentialModel).where(GoogleOAuthCredentialModel.id == credential_id))
        ).one()


@pytest.fixture
def gmail_send_oauth(monkeypatch: pytest.MonkeyPatch, repo: GoogleOAuthRepository) -> SimpleNamespace:
    organizations = SimpleNamespace(get_valid_org_auth_token=AsyncMock(return_value=None))
    monkeypatch.setattr(app, "DATABASE", SimpleNamespace(google_oauth=repo, organizations=organizations))
    monkeypatch.setattr(google_oauth_service.settings, "ENABLE_ENCRYPTION", True, raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_CLIENT_SECRET", "csecret", raising=False)
    monkeypatch.setattr(google_oauth_service.settings, "GOOGLE_OAUTH_REDIRECT_HOSTS", ["x"], raising=False)
    monkeypatch.setattr(
        google_oauth_service.SettingsManager,
        "get_settings",
        lambda: SimpleNamespace(ENABLE_ORGANIZATION_GOOGLE_OAUTH_CLIENT_CONFIG=False),
    )
    env = SimpleNamespace(
        claims={"sub": "subject-1", "email": "Sender@Example.Test", "email_verified": True},
        exchange=AsyncMock(),
        refresh=AsyncMock(return_value=google_oauth_service.GoogleRefreshResult("access-token", None)),
        fetch_profile_email=AsyncMock(return_value="profile@example.test"),
    )

    def verify_oauth2_token(id_token: str, request: object, audience: str, **_: object) -> dict[str, object]:
        assert audience == "cid"
        return env.claims

    monkeypatch.setattr(google_oauth_service.google_id_token, "verify_oauth2_token", verify_oauth2_token)
    encryptor = SimpleNamespace(encrypt=AsyncMock(return_value="enc-new"), decrypt=AsyncMock(return_value="old"))
    monkeypatch.setattr(google_oauth_service, "encryptor", encryptor)
    monkeypatch.setattr(google_oauth_service, "invalidate_google_access_token_cache", AsyncMock())
    monkeypatch.setattr(google_oauth_service, "exchange_code_for_tokens", env.exchange)
    monkeypatch.setattr(google_oauth_service, "refresh_and_rotate", env.refresh)
    monkeypatch.setattr(google_oauth_routes, "record_request_audit_event", AsyncMock())
    monkeypatch.setattr(google_oauth_routes.google_gmail_service, "fetch_profile_email", env.fetch_profile_email)
    monkeypatch.setattr(AgentFunction, "on_integration_connected", AsyncMock())
    return env


async def _start_consent(
    *, credential_id: str | None = None, scope_profile: str | None = "gmail_send"
) -> tuple[str, list[str]]:
    start = await google_oauth_service.start_authorization(
        organization_id="o_test",
        redirect_uri="https://x/cb",
        credential_id=credential_id,
        scope_profile=scope_profile,
        initiator_id="user_1",
    )
    return start.state, parse_qs(urlparse(start.authorize_url).query)["scope"][0].split()


async def _finish_consent(
    env: SimpleNamespace, state: str, granted: list[str], *, with_id_token: bool = True
) -> GoogleOAuthCredentialBase:
    env.exchange.return_value = {
        "refresh_token": "refresh-new",
        "access_token": "access-new",
        "scope": " ".join(granted),
        **({"id_token": "id-token"} if with_id_token else {}),
    }
    response = await google_oauth_routes.google_oauth_callback(
        CreateGoogleOAuthCallbackRequest(code="code", state=state),
        current_org=SimpleNamespace(organization_id="o_test"),
        current_user_id="user_1",
    )
    return response.credential


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_profile", "asked", "granted"),
    [
        ("gmail_send", [GMAIL_SEND_SCOPE, "openid", "email"], SEND_GRANT),
        ("gmail_read_send", [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE, "openid", "email"], READ_SEND_GRANT),
    ],
)
async def test_gmail_send_new_connection_asks_for_its_profile_and_records_the_verified_account(
    gmail_send_oauth: SimpleNamespace, engine: AsyncEngine, scope_profile: str, asked: list[str], granted: list[str]
) -> None:
    state, requested = await _start_consent(scope_profile=scope_profile)
    credential = await _finish_consent(gmail_send_oauth, state, granted)

    stored = await _stored_connection(engine, credential.id)
    assert requested == asked
    assert credential.gmail_send_ready is True
    assert (GMAIL_READONLY_SCOPE in credential.scopes_granted) == (scope_profile == "gmail_read_send")
    assert (stored.email_address, stored.google_subject) == ("sender@example.test", "subject-1")
    gmail_send_oauth.fetch_profile_email.assert_not_awaited()
    assert "google_subject" not in credential.model_dump()


@pytest.mark.asyncio
async def test_gmail_send_upgrade_keeps_the_connection_id_and_prior_grants(
    gmail_send_oauth: SimpleNamespace, repo: GoogleOAuthRepository, engine: AsyncEngine
) -> None:
    await _seed_gmail_connection(repo, engine, [GMAIL_READONLY_SCOPE])

    state, requested = await _start_consent(credential_id="goac_gmail")
    credential = await _finish_consent(gmail_send_oauth, state, READ_SEND_GRANT)

    stored = await _stored_connection(engine)
    assert requested == [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE, "openid", "email"]
    assert (credential.id, credential.gmail_send_ready) == ("goac_gmail", True)
    assert {GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE} <= set(stored.scopes_granted)
    assert (stored.encrypted_refresh_token, stored.google_subject) == ("enc-new", "subject-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted", "claims", "with_id_token", "stored_subject"),
    [
        pytest.param([GMAIL_READONLY_SCOPE, *IDENTITY_GRANT], {}, True, None, id="send-denied"),
        pytest.param(READ_SEND_GRANT, {"email": "other@example.test"}, True, None, id="account-switch"),
        pytest.param(READ_SEND_GRANT, {"sub": "subject-2"}, True, "subject-1", id="same-address-other-account"),
        pytest.param(READ_SEND_GRANT, {"email_verified": False}, True, None, id="unverified"),
        pytest.param(READ_SEND_GRANT, {}, False, None, id="no-id-token"),
    ],
)
async def test_gmail_send_rejected_upgrade_leaves_the_connection_as_it_was(
    gmail_send_oauth: SimpleNamespace,
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
    granted: list[str],
    claims: dict[str, object],
    with_id_token: bool,
    stored_subject: str | None,
) -> None:
    await _seed_gmail_connection(repo, engine, [GMAIL_READONLY_SCOPE], subject=stored_subject)
    before = await _stored_connection(engine)
    gmail_send_oauth.claims.update(claims)

    state, _ = await _start_consent(credential_id="goac_gmail")
    with pytest.raises(HTTPException) as rejected:
        await _finish_consent(gmail_send_oauth, state, granted, with_id_token=with_id_token)

    after = await _stored_connection(engine)
    assert rejected.value.status_code == 409
    assert (after.state, after.scopes_granted, after.email_address, after.google_subject) == (
        before.state,
        before.scopes_granted,
        before.email_address,
        before.google_subject,
    )
    assert after.encrypted_refresh_token == before.encrypted_refresh_token
    authorization = await google_oauth_service.resolve_gmail_send_authorization("o_test", "goac_gmail")
    assert authorization.status == google_oauth_service.GmailSendAuthorizationStatus.MISSING_SCOPE
    gmail_send_oauth.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_gmail_read_send_new_connection_saves_nothing_when_send_is_not_approved(
    gmail_send_oauth: SimpleNamespace, repo: GoogleOAuthRepository
) -> None:
    state, _ = await _start_consent(scope_profile="gmail_read_send")

    with pytest.raises(HTTPException) as rejected:
        await _finish_consent(gmail_send_oauth, state, [GMAIL_READONLY_SCOPE, *IDENTITY_GRANT])

    assert rejected.value.status_code == 409
    assert await repo.list_visible_for_org("o_test") == []


@pytest.mark.asyncio
async def test_gmail_send_cancelled_upgrade_is_not_requested_again_by_a_plain_reconnect(
    gmail_send_oauth: SimpleNamespace, repo: GoogleOAuthRepository, engine: AsyncEngine
) -> None:
    await _seed_gmail_connection(repo, engine, [GMAIL_READONLY_SCOPE])
    await _start_consent(credential_id="goac_gmail")
    assert GMAIL_SEND_SCOPE in (await _stored_connection(engine)).scopes_requested

    _, requested = await _start_consent(credential_id="goac_gmail", scope_profile=None)

    assert requested == [GMAIL_READONLY_SCOPE]
    assert (await _stored_connection(engine)).scopes_granted == [GMAIL_READONLY_SCOPE]


@pytest.mark.asyncio
async def test_gmail_send_connection_keeps_send_when_reconnected_with_the_read_profile(
    gmail_send_oauth: SimpleNamespace, repo: GoogleOAuthRepository, engine: AsyncEngine
) -> None:
    await _seed_gmail_connection(repo, engine, SEND_GRANT, subject="subject-1")

    _, requested = await _start_consent(credential_id="goac_gmail", scope_profile="gmail")

    assert requested == [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE, "openid", "email"]


@pytest.mark.asyncio
async def test_gmail_send_upgrade_is_refused_for_a_connection_with_no_stored_identity(
    gmail_send_oauth: SimpleNamespace, repo: GoogleOAuthRepository, engine: AsyncEngine
) -> None:
    sheets_scopes = list(google_oauth_service.GOOGLE_SHEETS_SCOPES)
    await _seed_gmail_connection(repo, engine, sheets_scopes, email=None)

    with pytest.raises(HTTPException) as refused:
        await google_oauth_routes.google_oauth_authorize(
            CreateGoogleOAuthAuthorizeRequest(
                redirect_uri="https://x/cb", credential_id="goac_gmail", scope_profile="gmail_send"
            ),
            current_org=SimpleNamespace(organization_id="o_test"),
            current_user_id="user_1",
        )

    stored = await _stored_connection(engine)
    assert refused.value.status_code == 409
    assert (stored.consent_nonce, stored.scopes_requested) == (None, sheets_scopes)


@pytest.mark.asyncio
async def test_gmail_send_authorization_reconnects_when_the_connection_changes_between_its_two_reads(
    gmail_send_oauth: SimpleNamespace,
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_gmail_connection(repo, engine, SEND_GRANT, subject="subject-1")
    load_active_ciphertext = repo.load_active_ciphertext

    async def reconnect_then_load(organization_id: str, credential_id: str) -> object:
        await repo.update_active_refresh_token(
            organization_id=organization_id,
            credential_id=credential_id,
            encrypted_refresh_token="enc-other-account",
            encrypted_method=EncryptMethod.AES,
            now=datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(seconds=5),
            expected_encrypted_refresh_token="cipher-goac_gmail",
        )
        return await load_active_ciphertext(organization_id=organization_id, credential_id=credential_id)

    monkeypatch.setattr(repo, "load_active_ciphertext", reconnect_then_load)

    authorization = await google_oauth_service.resolve_gmail_send_authorization("o_test", "goac_gmail")

    assert authorization.status == google_oauth_service.GmailSendAuthorizationStatus.RECONNECT
    gmail_send_oauth.refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh_error", "expected", "state_after"),
    [
        (google_oauth_service.ExpiredRefreshTokenError("revoked"), "reconnect", STATE_ERROR),
        (google_oauth_service.MissingAccessTokenError("token endpoint timed out"), "unavailable", STATE_ACTIVE),
        (google_oauth_service.ClientConfigMismatchError("configuration changed"), "reconnect", STATE_ACTIVE),
    ],
    ids=["rejected-token", "temporary-failure", "oauth-client-changed"],
)
async def test_gmail_send_authorization_asks_for_a_reconnect_only_when_reconnecting_fixes_it(
    gmail_send_oauth: SimpleNamespace,
    repo: GoogleOAuthRepository,
    engine: AsyncEngine,
    refresh_error: Exception,
    expected: str,
    state_after: str,
) -> None:
    await _seed_gmail_connection(repo, engine, SEND_GRANT, subject="subject-1")
    gmail_send_oauth.refresh.side_effect = refresh_error

    authorization = await google_oauth_service.resolve_gmail_send_authorization("o_test", "goac_gmail")

    assert (authorization.status.value, authorization.access_token) == (expected, None)
    assert (await _stored_connection(engine)).state == state_after


@pytest.mark.asyncio
async def test_gmail_send_dispatch_rejected_attempt_is_reclaimed_once_per_reading(repo: GoogleOAuthRepository) -> None:
    claim = await repo.claim_gmail_send_dispatch(
        organization_id="o_test",
        workflow_run_id="wr_1",
        execution_key="key",
        block_label="send",
        credential_id="goac_gmail",
    )
    assert claim is not None

    async def reclaim(observed_modified_at: datetime.datetime) -> bool:
        return await repo.reclaim_failed_gmail_send_dispatch(
            gmail_send_dispatch_id=claim.gmail_send_dispatch_id,
            observed_modified_at=observed_modified_at,
            credential_id="goac_gmail",
        )

    async def reject() -> None:
        await repo.finalize_gmail_send_dispatch(
            gmail_send_dispatch_id=claim.gmail_send_dispatch_id, status=DISPATCH_FAILED
        )

    while_the_send_is_in_flight = await reclaim(claim.modified_at)
    await reject()
    read_by_two_executions = await repo.get_gmail_send_dispatch("wr_1", "key")
    assert read_by_two_executions is not None
    first_reader = await reclaim(read_by_two_executions.modified_at)
    await reject()
    stale_reader = await reclaim(read_by_two_executions.modified_at)

    assert (while_the_send_is_in_flight, first_reader, stale_reader) == (False, True, False)
