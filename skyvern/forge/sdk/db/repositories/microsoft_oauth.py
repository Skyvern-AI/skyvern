from __future__ import annotations

import datetime
from dataclasses import dataclass

import structlog
from sqlalchemy import select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import MicrosoftOAuthCredentialModel
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase

LOG = structlog.get_logger()

STATE_PENDING_CONSENT = "pending_consent"
STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"
STATE_ERROR = "error"


class InvalidConsentNonceError(ValueError):
    pass


@dataclass(frozen=True)
class PendingConsentContext:
    credential_id: str
    consent_redirect_uri: str | None
    consent_code_verifier: str | None
    consent_app_origin: str | None = None
    scopes_requested: list[str] | None = None


@dataclass(frozen=True)
class ActiveCredentialCiphertext:
    encrypted_refresh_token: str
    encrypted_method: EncryptMethod
    scopes_granted: list[str]


@dataclass(frozen=True)
class RevocableCiphertext:
    exists: bool
    encrypted_refresh_token: str | None = None
    encrypted_method: EncryptMethod | None = None


class MicrosoftOAuthRepository(BaseRepository):
    @db_operation("insert_pending_credential")
    async def insert_pending_credential(
        self,
        credential_id: str,
        organization_id: str,
        credential_name: str,
        scopes_requested: list[str],
        consent_nonce: str,
        consent_redirect_uri: str,
        consent_expires_at: datetime.datetime,
        consent_code_verifier: str,
        consent_app_origin: str | None = None,
    ) -> MicrosoftOAuthCredentialBase:
        async with self.Session() as session:
            model = MicrosoftOAuthCredentialModel(
                id=credential_id,
                organization_id=organization_id,
                credential_name=credential_name,
                state=STATE_PENDING_CONSENT,
                scopes_requested=scopes_requested,
                scopes_granted=[],
                consent_nonce=consent_nonce,
                consent_redirect_uri=consent_redirect_uri,
                consent_expires_at=consent_expires_at,
                consent_app_origin=consent_app_origin,
                consent_code_verifier=consent_code_verifier,
            )
            session.add(model)
            await session.flush()
            result = MicrosoftOAuthCredentialBase.model_validate(model, from_attributes=True)
            await session.commit()
            return result

    @db_operation("load_pending_by_nonce")
    async def load_pending_by_nonce(
        self,
        organization_id: str,
        nonce: str,
        now: datetime.datetime | None = None,
    ) -> PendingConsentContext | None:
        cutoff = now if now is not None else datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        async with self.Session() as session:
            stmt = select(
                MicrosoftOAuthCredentialModel.id,
                MicrosoftOAuthCredentialModel.consent_redirect_uri,
                MicrosoftOAuthCredentialModel.consent_code_verifier,
                MicrosoftOAuthCredentialModel.consent_app_origin,
                MicrosoftOAuthCredentialModel.scopes_requested,
            ).where(
                MicrosoftOAuthCredentialModel.consent_nonce == nonce,
                MicrosoftOAuthCredentialModel.organization_id == organization_id,
                MicrosoftOAuthCredentialModel.state == STATE_PENDING_CONSENT,
                MicrosoftOAuthCredentialModel.consent_expires_at >= cutoff,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            return PendingConsentContext(
                credential_id=row[0],
                consent_redirect_uri=row[1],
                consent_code_verifier=row[2],
                consent_app_origin=row[3],
                scopes_requested=list(row[4] or []),
            )

    @db_operation("promote_pending_to_active")
    async def promote_pending_to_active(
        self,
        organization_id: str,
        nonce: str,
        encrypted_refresh_token: str,
        encrypted_method: EncryptMethod,
        scopes_granted: list[str],
        now: datetime.datetime,
    ) -> MicrosoftOAuthCredentialBase:
        async with self.Session() as session:
            stmt = (
                update(MicrosoftOAuthCredentialModel)
                .where(
                    MicrosoftOAuthCredentialModel.consent_nonce == nonce,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_PENDING_CONSENT,
                    MicrosoftOAuthCredentialModel.consent_expires_at >= now,
                )
                .values(
                    state=STATE_ACTIVE,
                    encrypted_refresh_token=encrypted_refresh_token,
                    encrypted_method=encrypted_method.value,
                    scopes_granted=scopes_granted,
                    consent_nonce=None,
                    consent_redirect_uri=None,
                    consent_expires_at=None,
                    consent_code_verifier=None,
                    consent_app_origin=None,
                    modified_at=now,
                )
                .returning(MicrosoftOAuthCredentialModel)
            )
            promoted = (await session.execute(stmt)).scalar_one_or_none()
            if promoted is None:
                fallback = (
                    await session.execute(
                        select(MicrosoftOAuthCredentialModel).where(
                            MicrosoftOAuthCredentialModel.consent_nonce == nonce,
                            MicrosoftOAuthCredentialModel.organization_id == organization_id,
                        )
                    )
                ).scalar_one_or_none()
                if fallback is None:
                    raise InvalidConsentNonceError("Unknown OAuth consent nonce")
                if fallback.state == STATE_ERROR:
                    raise InvalidConsentNonceError("OAuth consent row is in error state")
                if fallback.state != STATE_PENDING_CONSENT:
                    raise InvalidConsentNonceError("OAuth consent nonce already consumed")
                raise InvalidConsentNonceError("OAuth consent nonce expired")
            result = MicrosoftOAuthCredentialBase.model_validate(promoted, from_attributes=True)
            await session.commit()
            LOG.info(
                "Promoted pending Microsoft OAuth credential",
                credential_id=result.id,
                organization_id=organization_id,
            )
            return result

    @db_operation("list_active_for_org")
    async def list_active_for_org(self, organization_id: str) -> list[MicrosoftOAuthCredentialBase]:
        async with self.Session() as session:
            stmt = (
                select(MicrosoftOAuthCredentialModel)
                .where(
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
                )
                .order_by(MicrosoftOAuthCredentialModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [MicrosoftOAuthCredentialBase.model_validate(r, from_attributes=True) for r in rows]

    @db_operation("load_active_ciphertext")
    async def load_active_ciphertext(
        self,
        organization_id: str,
        credential_id: str,
    ) -> ActiveCredentialCiphertext | None:
        async with self.Session() as session:
            stmt = select(
                MicrosoftOAuthCredentialModel.encrypted_refresh_token,
                MicrosoftOAuthCredentialModel.encrypted_method,
                MicrosoftOAuthCredentialModel.scopes_granted,
            ).where(
                MicrosoftOAuthCredentialModel.id == credential_id,
                MicrosoftOAuthCredentialModel.organization_id == organization_id,
                MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            ciphertext, method, scopes = row
            if not ciphertext or not method:
                return None
            return ActiveCredentialCiphertext(
                encrypted_refresh_token=ciphertext,
                encrypted_method=EncryptMethod(method),
                scopes_granted=list(scopes or []),
            )

    @db_operation("load_ciphertext_for_revoke")
    async def load_ciphertext_for_revoke(
        self,
        organization_id: str,
        credential_id: str,
    ) -> RevocableCiphertext:
        async with self.Session() as session:
            stmt = select(
                MicrosoftOAuthCredentialModel.encrypted_refresh_token,
                MicrosoftOAuthCredentialModel.encrypted_method,
            ).where(
                MicrosoftOAuthCredentialModel.id == credential_id,
                MicrosoftOAuthCredentialModel.organization_id == organization_id,
                MicrosoftOAuthCredentialModel.state != STATE_REVOKED,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return RevocableCiphertext(exists=False)
            ciphertext, method = row
            if not ciphertext or not method:
                return RevocableCiphertext(exists=True)
            return RevocableCiphertext(
                exists=True,
                encrypted_refresh_token=ciphertext,
                encrypted_method=EncryptMethod(method),
            )

    @db_operation("rename_active")
    async def rename_active(
        self,
        organization_id: str,
        credential_id: str,
        credential_name: str,
        now: datetime.datetime,
    ) -> MicrosoftOAuthCredentialBase | None:
        async with self.Session() as session:
            stmt = (
                update(MicrosoftOAuthCredentialModel)
                .where(
                    MicrosoftOAuthCredentialModel.id == credential_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
                )
                .values(credential_name=credential_name, modified_at=now)
                .returning(MicrosoftOAuthCredentialModel)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            result = MicrosoftOAuthCredentialBase.model_validate(row, from_attributes=True)
            await session.commit()
            return result

    @db_operation("update_active_refresh_token")
    async def update_active_refresh_token(
        self,
        *,
        organization_id: str,
        credential_id: str,
        encrypted_refresh_token: str,
        encrypted_method: EncryptMethod,
        now: datetime.datetime,
    ) -> None:
        async with self.Session() as session:
            stmt = (
                update(MicrosoftOAuthCredentialModel)
                .where(
                    MicrosoftOAuthCredentialModel.id == credential_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
                )
                .values(
                    encrypted_refresh_token=encrypted_refresh_token,
                    encrypted_method=encrypted_method.value,
                    modified_at=now,
                )
            )
            await session.execute(stmt)
            await session.commit()

    @db_operation("mark_revoked_and_scrub")
    async def mark_revoked_and_scrub(
        self,
        organization_id: str,
        credential_id: str,
        now: datetime.datetime,
    ) -> str | None:
        async with self.Session() as session:
            stmt = (
                update(MicrosoftOAuthCredentialModel)
                .where(
                    MicrosoftOAuthCredentialModel.id == credential_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state != STATE_REVOKED,
                )
                .values(
                    state=STATE_REVOKED,
                    encrypted_refresh_token=None,
                    encrypted_method=None,
                    consent_nonce=None,
                    consent_redirect_uri=None,
                    consent_expires_at=None,
                    consent_code_verifier=None,
                    consent_app_origin=None,
                    modified_at=now,
                )
                .returning(MicrosoftOAuthCredentialModel.id)
            )
            revoked_id = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
            return revoked_id
