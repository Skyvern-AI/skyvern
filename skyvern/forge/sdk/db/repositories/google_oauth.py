from __future__ import annotations

import datetime
from dataclasses import dataclass

import structlog
from sqlalchemy import select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import GoogleOAuthCredentialModel
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase

LOG = structlog.get_logger()

# Credential lifecycle states. Kept as plain strings so DB rows survive code rewrites;
# the CHECK constraint in the migration pins the valid set. The repository owns these
# because the DB schema defines them — service re-exports for callers that previously
# imported through the service module.
STATE_PENDING_CONSENT = "pending_consent"
STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"
STATE_ERROR = "error"


class InvalidConsentNonceError(ValueError):
    """Raised when the OAuth callback nonce is unknown, expired, or already consumed.

    Defined here (not in the service) because ``promote_pending_to_active`` is the
    only place it's raised — keeping it next to the raiser avoids the
    service<->repo circular import that an in-method import previously dodged.
    """


@dataclass(frozen=True)
class PendingConsentContext:
    credential_id: str
    consent_redirect_uri: str | None
    consent_code_verifier: str | None
    consent_app_origin: str | None = None
    client_id: str | None = None


@dataclass(frozen=True)
class ActiveCredentialCiphertext:
    encrypted_refresh_token: str
    encrypted_method: EncryptMethod
    scopes_granted: list[str]
    client_id: str | None = None
    credential_version: datetime.datetime | None = None


@dataclass(frozen=True)
class RevocableCiphertext:
    exists: bool
    encrypted_refresh_token: str | None = None
    encrypted_method: EncryptMethod | None = None


class GoogleOAuthRepository(BaseRepository):
    """All DB access for Google OAuth credentials. Owns session lifecycle."""

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
        client_id: str | None = None,
    ) -> GoogleOAuthCredentialBase:
        async with self.Session() as session:
            model = GoogleOAuthCredentialModel(
                id=credential_id,
                organization_id=organization_id,
                credential_name=credential_name,
                provider="google",
                state=STATE_PENDING_CONSENT,
                scopes_requested=scopes_requested,
                scopes_granted=[],
                consent_nonce=consent_nonce,
                consent_redirect_uri=consent_redirect_uri,
                consent_expires_at=consent_expires_at,
                consent_app_origin=consent_app_origin,
                consent_code_verifier=consent_code_verifier,
                client_id=client_id,
            )
            session.add(model)
            await session.flush()
            result = GoogleOAuthCredentialBase.model_validate(model, from_attributes=True)
            await session.commit()
            return result

    @db_operation("load_pending_by_nonce")
    async def load_pending_by_nonce(
        self,
        organization_id: str,
        nonce: str,
        now: datetime.datetime | None = None,
    ) -> PendingConsentContext | None:
        # Reject expired rows here so the route layer doesn't burn Google's one-time
        # auth code on a doomed exchange — promote_pending_to_active would catch the
        # stale nonce, but only after the code has already been consumed.
        cutoff = now if now is not None else datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        async with self.Session() as session:
            # A consent nonce lives on a fresh ``pending_consent`` row (first connect) or on an
            # existing ``active``/``error`` row that is being re-authorized in place. The nonce is
            # globally unique while set, so matching nonce + org + unexpired identifies the row in
            # either case; a successful promotion (or revoke) nulls the nonce afterward.
            stmt = select(
                GoogleOAuthCredentialModel.id,
                GoogleOAuthCredentialModel.consent_redirect_uri,
                GoogleOAuthCredentialModel.consent_code_verifier,
                GoogleOAuthCredentialModel.consent_app_origin,
                GoogleOAuthCredentialModel.client_id,
            ).where(
                GoogleOAuthCredentialModel.consent_nonce == nonce,
                GoogleOAuthCredentialModel.organization_id == organization_id,
                GoogleOAuthCredentialModel.state.in_([STATE_PENDING_CONSENT, STATE_ACTIVE, STATE_ERROR]),
                GoogleOAuthCredentialModel.consent_expires_at >= cutoff,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            return PendingConsentContext(
                credential_id=row[0],
                consent_redirect_uri=row[1],
                consent_code_verifier=row[2],
                consent_app_origin=row[3],
                client_id=row[4],
            )

    @db_operation("begin_reauthorization")
    async def begin_reauthorization(
        self,
        credential_id: str,
        organization_id: str,
        consent_nonce: str,
        consent_redirect_uri: str,
        consent_expires_at: datetime.datetime,
        consent_code_verifier: str,
        now: datetime.datetime,
        consent_app_origin: str | None = None,
        client_id: str | None = None,
        requested_scopes: list[str] | None = None,
        fallback_scopes: list[str] | None = None,
    ) -> GoogleOAuthCredentialBase | None:
        """Stamp a fresh consent challenge onto an existing connectable row so re-auth
        preserves the credential id.

        The row's live ``encrypted_refresh_token`` and ``state`` are left untouched, so any
        workflow referencing this credential keeps working until the callback promotes it with
        the new token. Only ``active``/``error`` rows are re-authorizable; returns None otherwise.
        """
        async with self.Session() as session:
            stmt = (
                select(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.id == credential_id,
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state.in_([STATE_ACTIVE, STATE_ERROR]),
                )
                .with_for_update()
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.scopes_requested = list(
                requested_scopes
                if requested_scopes is not None
                else row.scopes_requested or row.scopes_granted or fallback_scopes or []
            )
            row.consent_nonce = consent_nonce
            row.consent_redirect_uri = consent_redirect_uri
            row.consent_expires_at = consent_expires_at
            row.consent_code_verifier = consent_code_verifier
            row.consent_app_origin = consent_app_origin
            row.client_id = client_id
            row.modified_at = now
            await session.flush()
            result = GoogleOAuthCredentialBase.model_validate(row, from_attributes=True)
            await session.commit()
            return result

    @db_operation("mark_needs_reconnect")
    async def mark_needs_reconnect(
        self,
        organization_id: str,
        credential_id: str,
        now: datetime.datetime,
        expected_version: datetime.datetime | None = None,
    ) -> str | None:
        """Flip an active credential to ``error`` after its refresh token is rejected upstream.

        No-op when the row is no longer active or changed after the failing refresh loaded it.
        """
        async with self.Session() as session:
            filters = [
                GoogleOAuthCredentialModel.id == credential_id,
                GoogleOAuthCredentialModel.organization_id == organization_id,
                GoogleOAuthCredentialModel.state == STATE_ACTIVE,
            ]
            if expected_version is not None:
                filters.append(GoogleOAuthCredentialModel.modified_at == expected_version)
            stmt = (
                update(GoogleOAuthCredentialModel)
                .where(*filters)
                .values(state=STATE_ERROR, modified_at=now)
                .returning(GoogleOAuthCredentialModel.id)
            )
            flipped = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
            return flipped

    @db_operation("mark_active_mismatched_client_as_error")
    async def mark_active_mismatched_client_as_error(
        self,
        organization_id: str,
        new_client_id: str | None,
        now: datetime.datetime,
    ) -> int:
        async with self.Session() as session:
            filters = [
                GoogleOAuthCredentialModel.organization_id == organization_id,
                GoogleOAuthCredentialModel.state == STATE_ACTIVE,
                GoogleOAuthCredentialModel.client_id.is_not(None),
            ]
            if new_client_id is not None:
                filters.append(GoogleOAuthCredentialModel.client_id != new_client_id)
            stmt = (
                update(GoogleOAuthCredentialModel)
                .where(*filters)
                .values(
                    state=STATE_ERROR,
                    modified_at=now,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    @db_operation("promote_pending_to_active")
    async def promote_pending_to_active(
        self,
        organization_id: str,
        nonce: str,
        encrypted_refresh_token: str,
        encrypted_method: EncryptMethod,
        scopes_granted: list[str],
        now: datetime.datetime,
    ) -> GoogleOAuthCredentialBase:
        async with self.Session() as session:
            # Promotes both a first-connect row (``pending_consent``) and an in-place re-auth of an
            # existing ``active``/``error`` row: whichever row currently holds this nonce receives the
            # new refresh token and is (re)set to ``active``, preserving the credential id so workflows
            # keep referencing the same connection.
            stmt = (
                update(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.consent_nonce == nonce,
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state.in_([STATE_PENDING_CONSENT, STATE_ACTIVE, STATE_ERROR]),
                    GoogleOAuthCredentialModel.consent_expires_at >= now,
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
                .returning(GoogleOAuthCredentialModel)
            )
            promoted = (await session.execute(stmt)).scalar_one_or_none()
            if promoted is None:
                fallback = (
                    await session.execute(
                        select(GoogleOAuthCredentialModel).where(
                            GoogleOAuthCredentialModel.consent_nonce == nonce,
                            GoogleOAuthCredentialModel.organization_id == organization_id,
                        )
                    )
                ).scalar_one_or_none()
                # A successful promotion (or revoke) nulls the nonce, so a missing row means the
                # nonce is unknown or already consumed. A row that still holds the nonce but failed
                # the UPDATE only failed the expiry guard.
                if fallback is None:
                    raise InvalidConsentNonceError("Unknown or already consumed OAuth consent nonce")
                raise InvalidConsentNonceError("OAuth consent nonce expired")
            result = GoogleOAuthCredentialBase.model_validate(promoted, from_attributes=True)
            await session.commit()
            LOG.info(
                "Promoted pending Google OAuth credential",
                credential_id=result.id,
                organization_id=organization_id,
            )
            return result

    @db_operation("list_active_for_org")
    async def list_active_for_org(self, organization_id: str) -> list[GoogleOAuthCredentialBase]:
        async with self.Session() as session:
            stmt = (
                select(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state == STATE_ACTIVE,
                )
                .order_by(GoogleOAuthCredentialModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [GoogleOAuthCredentialBase.model_validate(r, from_attributes=True) for r in rows]

    @db_operation("list_visible_for_org")
    async def list_visible_for_org(self, organization_id: str) -> list[GoogleOAuthCredentialBase]:
        async with self.Session() as session:
            stmt = (
                select(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state.in_([STATE_ACTIVE, STATE_ERROR]),
                )
                .order_by(GoogleOAuthCredentialModel.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [GoogleOAuthCredentialBase.model_validate(r, from_attributes=True) for r in rows]

    @db_operation("load_active_ciphertext")
    async def load_active_ciphertext(
        self,
        organization_id: str,
        credential_id: str,
    ) -> ActiveCredentialCiphertext | None:
        async with self.Session() as session:
            stmt = select(
                GoogleOAuthCredentialModel.encrypted_refresh_token,
                GoogleOAuthCredentialModel.encrypted_method,
                GoogleOAuthCredentialModel.scopes_granted,
                GoogleOAuthCredentialModel.client_id,
                GoogleOAuthCredentialModel.modified_at,
            ).where(
                GoogleOAuthCredentialModel.id == credential_id,
                GoogleOAuthCredentialModel.organization_id == organization_id,
                GoogleOAuthCredentialModel.state == STATE_ACTIVE,
            )
            row = (await session.execute(stmt)).one_or_none()
            if row is None:
                return None
            ciphertext, method, scopes, client_id, credential_version = row
            if not ciphertext or not method:
                return None
            return ActiveCredentialCiphertext(
                encrypted_refresh_token=ciphertext,
                encrypted_method=EncryptMethod(method),
                scopes_granted=list(scopes or []),
                client_id=client_id,
                credential_version=credential_version,
            )

    @db_operation("load_ciphertext_for_revoke")
    async def load_ciphertext_for_revoke(
        self,
        organization_id: str,
        credential_id: str,
    ) -> RevocableCiphertext:
        async with self.Session() as session:
            stmt = select(
                GoogleOAuthCredentialModel.encrypted_refresh_token,
                GoogleOAuthCredentialModel.encrypted_method,
            ).where(
                GoogleOAuthCredentialModel.id == credential_id,
                GoogleOAuthCredentialModel.organization_id == organization_id,
                GoogleOAuthCredentialModel.state != STATE_REVOKED,
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
    ) -> GoogleOAuthCredentialBase | None:
        async with self.Session() as session:
            stmt = (
                update(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.id == credential_id,
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state.in_([STATE_ACTIVE, STATE_ERROR]),
                )
                .values(credential_name=credential_name, modified_at=now)
                .returning(GoogleOAuthCredentialModel)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                # No-match path: skip commit (nothing to persist) and let the
                # session context manager close cleanly. Postgres has no row
                # lock to release because the WHERE filtered out every row.
                return None
            result = GoogleOAuthCredentialBase.model_validate(row, from_attributes=True)
            await session.commit()
            return result

    @db_operation("mark_revoked_and_scrub")
    async def mark_revoked_and_scrub(
        self,
        organization_id: str,
        credential_id: str,
        now: datetime.datetime,
    ) -> str | None:
        async with self.Session() as session:
            stmt = (
                update(GoogleOAuthCredentialModel)
                .where(
                    GoogleOAuthCredentialModel.id == credential_id,
                    GoogleOAuthCredentialModel.organization_id == organization_id,
                    GoogleOAuthCredentialModel.state != STATE_REVOKED,
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
                .returning(GoogleOAuthCredentialModel.id)
            )
            revoked_id = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
            return revoked_id
