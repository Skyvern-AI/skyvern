import datetime
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, GoogleOAuthCredentialModel  # noqa: F401 - registers model on Base
from skyvern.forge.sdk.db.repositories.google_oauth import (
    STATE_PENDING_CONSENT,
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
