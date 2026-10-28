from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, or_, select, update

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


def _has_scope(scopes: list[str] | None, expected: str) -> bool:
    return any(isinstance(scope, str) and scope.rsplit("/", 1)[-1] == expected for scope in scopes or [])


def _is_powerbi_connection(scopes_requested: list[str] | None, scopes_granted: list[str] | None) -> bool:
    return _has_scope(scopes_requested, "Dataset.Read.All") or _has_scope(scopes_granted, "Dataset.Read.All")


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
    encrypted_method: str | None
    scopes_granted: list[str]
    scopes_requested: list[str] = field(default_factory=list)
    initiating_user_id: str | None = None
    connection_id: str | None = None


@dataclass(frozen=True)
class ActiveCredentialAccess:
    scopes_requested: list[str]
    scopes_granted: list[str]
    initiating_user_id: str | None
    connection_id: str | None


@dataclass(frozen=True)
class RevocableCiphertext:
    exists: bool
    encrypted_refresh_token: str | None = None
    encrypted_method: str | None = None


@dataclass(frozen=True)
class ActiveCustodyConnection:
    scopes_granted: list[str]
    tenant_id: str | None
    object_id: str | None
    initiating_user_id: str | None
    connection_id: str | None
    custody_mode: str | None
    modified_at: datetime.datetime


class MicrosoftOAuthRepository(BaseRepository):
    @db_operation("get_credential_state")
    async def get_credential_state(self, organization_id: str, credential_id: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(
                select(MicrosoftOAuthCredentialModel.state).where(
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.id == credential_id,
                )
            )

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
        initiating_user_id: str | None,
        tenant_id: str | None,
        object_id: str | None,
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
                    tenant_id=tenant_id or MicrosoftOAuthCredentialModel.tenant_id,
                    object_id=object_id or MicrosoftOAuthCredentialModel.object_id,
                    initiating_user_id=initiating_user_id or MicrosoftOAuthCredentialModel.initiating_user_id,
                    connection_id=MicrosoftOAuthCredentialModel.id,
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
    async def list_active_for_org(
        self, organization_id: str, user_id: str | None = None
    ) -> list[MicrosoftOAuthCredentialBase]:
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
            return [
                MicrosoftOAuthCredentialBase.model_validate(row, from_attributes=True)
                for row in rows
                if not _is_powerbi_connection(row.scopes_requested, row.scopes_granted)
                or (user_id is not None and user_id != f"{organization_id}_user" and row.initiating_user_id == user_id)
            ]

    @db_operation("list_for_org")
    async def list_for_org(self, organization_id: str) -> list[MicrosoftOAuthCredentialBase]:
        """Return every credential row for the organization, regardless of state."""
        async with self.Session() as session:
            stmt = (
                select(MicrosoftOAuthCredentialModel)
                .where(MicrosoftOAuthCredentialModel.organization_id == organization_id)
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
                MicrosoftOAuthCredentialModel.scopes_requested,
                MicrosoftOAuthCredentialModel.initiating_user_id,
                MicrosoftOAuthCredentialModel.connection_id,
            ).where(
                MicrosoftOAuthCredentialModel.id == credential_id,
                MicrosoftOAuthCredentialModel.organization_id == organization_id,
                MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            ciphertext, method, scopes, requested_scopes, initiating_user_id, connection_id = row
            if not ciphertext:
                return None
            return ActiveCredentialCiphertext(
                encrypted_refresh_token=ciphertext,
                encrypted_method=method,
                scopes_granted=list(scopes or []),
                scopes_requested=list(requested_scopes or []),
                initiating_user_id=initiating_user_id,
                connection_id=connection_id,
            )

    @db_operation("load_active_access_metadata")
    async def load_active_access_metadata(
        self,
        organization_id: str,
        credential_id: str,
    ) -> ActiveCredentialAccess | None:
        async with self.Session() as session:
            row = await session.execute(
                select(
                    MicrosoftOAuthCredentialModel.scopes_requested,
                    MicrosoftOAuthCredentialModel.scopes_granted,
                    MicrosoftOAuthCredentialModel.initiating_user_id,
                    MicrosoftOAuthCredentialModel.connection_id,
                ).where(
                    MicrosoftOAuthCredentialModel.id == credential_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
                    MicrosoftOAuthCredentialModel.encrypted_refresh_token.is_not(None),
                    MicrosoftOAuthCredentialModel.encrypted_refresh_token != "",
                )
            )
            values = row.one_or_none()
            if values is None:
                return None
            requested, granted, initiating_user_id, connection_id = values
            return ActiveCredentialAccess(
                scopes_requested=list(requested or []),
                scopes_granted=list(granted or []),
                initiating_user_id=initiating_user_id,
                connection_id=connection_id,
            )

    @db_operation("can_manage_credential")
    async def can_manage_credential(
        self,
        organization_id: str,
        credential_id: str,
        user_id: str | None,
    ) -> bool:
        async with self.Session() as session:
            row = await session.execute(
                select(
                    MicrosoftOAuthCredentialModel.scopes_requested,
                    MicrosoftOAuthCredentialModel.scopes_granted,
                    MicrosoftOAuthCredentialModel.initiating_user_id,
                ).where(
                    MicrosoftOAuthCredentialModel.id == credential_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state != STATE_REVOKED,
                )
            )
            values = row.one_or_none()
            if values is None:
                return False
            requested, granted, owner_user_id = values
            return not _is_powerbi_connection(requested, granted) or (
                user_id is not None and user_id != f"{organization_id}_user" and user_id == owner_user_id
            )

    @db_operation("load_active_custody_connection")
    async def load_active_custody_connection(
        self, organization_id: str, connection_id: str
    ) -> ActiveCustodyConnection | None:
        async with self.Session() as session:
            row = await session.execute(
                select(
                    MicrosoftOAuthCredentialModel.scopes_granted,
                    MicrosoftOAuthCredentialModel.tenant_id,
                    MicrosoftOAuthCredentialModel.object_id,
                    MicrosoftOAuthCredentialModel.initiating_user_id,
                    MicrosoftOAuthCredentialModel.connection_id,
                    MicrosoftOAuthCredentialModel.custody_mode,
                    MicrosoftOAuthCredentialModel.modified_at,
                ).where(
                    MicrosoftOAuthCredentialModel.id == connection_id,
                    MicrosoftOAuthCredentialModel.organization_id == organization_id,
                    MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
                )
            )
            values = row.one_or_none()
            if values is None:
                return None
            scopes, tenant_id, object_id, initiator, bound_connection, custody_mode, modified_at = values
            if bound_connection not in (None, connection_id) or custody_mode not in (None, "cloud_held"):
                return None
            return ActiveCustodyConnection(
                scopes_granted=list(scopes or []),
                tenant_id=tenant_id,
                object_id=object_id,
                initiating_user_id=initiator,
                connection_id=bound_connection,
                custody_mode=custody_mode,
                modified_at=modified_at,
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
            if not ciphertext:
                return RevocableCiphertext(exists=True)
            return RevocableCiphertext(
                exists=True,
                encrypted_refresh_token=ciphertext,
                encrypted_method=method,
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
        encrypted_refresh_token: str | None = None,
        encrypted_method: EncryptMethod | None = None,
        tenant_id: str | None = None,
        object_id: str | None = None,
        initiating_user_id: str | None = None,
        connection_id: str | None = None,
        custody_mode: str | None = None,
        now: datetime.datetime,
        touch_modified_at: bool = True,
        expected_encrypted_refresh_token: str | None = None,
    ) -> bool:
        values: dict[str, object] = {"modified_at": now} if touch_modified_at else {}
        if encrypted_refresh_token is not None:
            if encrypted_method is None:
                raise ValueError("encrypted method is required with a refresh token")
            if expected_encrypted_refresh_token is None:
                raise ValueError("expected encrypted refresh token is required for token updates")
            if not touch_modified_at:
                raise ValueError("token updates must advance modified_at")
            values.update(
                encrypted_refresh_token=encrypted_refresh_token,
                encrypted_method=encrypted_method.value,
            )
        metadata = (
            ("tenant_id", tenant_id),
            ("object_id", object_id),
            ("initiating_user_id", initiating_user_id),
            ("connection_id", connection_id),
            ("custody_mode", custody_mode),
        )
        for name, value in metadata:
            if value is not None:
                values[name] = func.coalesce(getattr(MicrosoftOAuthCredentialModel, name), value)
        if not touch_modified_at and not any(value is not None for _, value in metadata):
            return False
        conditions = [
            MicrosoftOAuthCredentialModel.id == credential_id,
            MicrosoftOAuthCredentialModel.organization_id == organization_id,
            MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
        ]
        if not touch_modified_at:
            conditions.append(
                or_(
                    *(
                        getattr(MicrosoftOAuthCredentialModel, name).is_(None)
                        for name, value in metadata
                        if value is not None
                    )
                )
            )
        if expected_encrypted_refresh_token is not None:
            conditions.append(MicrosoftOAuthCredentialModel.encrypted_refresh_token == expected_encrypted_refresh_token)
        async with self.Session() as session:
            stmt = update(MicrosoftOAuthCredentialModel).where(*conditions).values(**values)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    @db_operation("update_email_address")
    async def update_email_address(
        self,
        *,
        organization_id: str,
        credential_id: str,
        email_address: str,
        only_if_null: bool,
    ) -> bool:
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        async with self.Session() as session:
            filters = [
                MicrosoftOAuthCredentialModel.id == credential_id,
                MicrosoftOAuthCredentialModel.organization_id == organization_id,
                MicrosoftOAuthCredentialModel.state == STATE_ACTIVE,
            ]
            if only_if_null:
                filters.append(MicrosoftOAuthCredentialModel.email_address.is_(None))
            stmt = (
                update(MicrosoftOAuthCredentialModel)
                .where(*filters)
                .values(email_address=email_address, modified_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

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
                    email_address=None,
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
