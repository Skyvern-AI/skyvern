from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.pool import QueuePool

from skyvern.forge.sdk.db._error_handling import db_operation, register_passthrough_exception
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.models import CredentialModel, OrganizationPhoneNumberModel, OrganizationSMSConfigModel
from skyvern.forge.sdk.encrypt import EncryptMethod, encryptor
from skyvern.forge.sdk.schemas.sms import PHONE_NUMBER_STATUSES, SMS_CONFIG_MODES, OrganizationPhoneNumber, SMSConfig
from skyvern.forge.sdk.workflow.secret_encryption import encrypt_secret_field_value
from skyvern.utils.phone_validation import (
    is_e164_phone_number,
    looks_like_phone_identifier,
    normalize_phone_identifier,
)

_PHONE_NUMBER_UNIQUE_INDEX = "uq_org_phone_numbers_org_number"
_PHONE_NUMBER_SQLITE_UNIQUE_ERROR = (
    "UNIQUE constraint failed: organization_phone_numbers.organization_id, organization_phone_numbers.phone_number"
)


class PhoneNumberAlreadyExists(ValueError):
    """Raised when an active phone number is already registered for an organization."""


class InvalidPhoneNumberStatus(ValueError):
    """Raised when a phone number update specifies an unsupported status."""


class InvalidSMSConfigMode(ValueError):
    """Raised when an SMS config specifies an unsupported mode."""


class SMSConfigHasActivePhoneNumbers(ValueError):
    """Raised when an SMS config still has active phone number references."""


register_passthrough_exception(InvalidPhoneNumberStatus)
register_passthrough_exception(InvalidSMSConfigMode)
register_passthrough_exception(PhoneNumberAlreadyExists)
register_passthrough_exception(SMSConfigHasActivePhoneNumbers)


def _is_phone_number_unique_violation(error: IntegrityError) -> bool:
    original: BaseException | None = error.orig
    seen: set[int] = set()
    while original is not None and id(original) not in seen:
        seen.add(id(original))
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(original, "constraint_name", None) or getattr(diagnostic, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name == _PHONE_NUMBER_UNIQUE_INDEX
        original = original.__cause__
    return str(error.orig) == _PHONE_NUMBER_SQLITE_UNIQUE_ERROR


def _hide_integrity_error_parameters(error: IntegrityError) -> None:
    error.hide_parameters = True


async def _lock_sms_configs(
    session: AsyncSession,
    organization_id: str,
    sms_config_ids: set[str],
) -> dict[str, OrganizationSMSConfigModel]:
    rows = (
        await session.scalars(
            select(OrganizationSMSConfigModel)
            .where(
                OrganizationSMSConfigModel.organization_id == organization_id,
                OrganizationSMSConfigModel.sms_config_id.in_(sms_config_ids),
                OrganizationSMSConfigModel.deleted_at.is_(None),
            )
            .order_by(OrganizationSMSConfigModel.sms_config_id)
            .with_for_update()
        )
    ).all()
    return {row.sms_config_id: row for row in rows}


_POSTGRES_LIFECYCLE_SERIALIZER = asyncio.Lock()
_POSTGRES_LIFECYCLE_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


async def _encrypt_previous_sms_url(value: str | None, organization_id: str) -> str | None:
    return await encrypt_secret_field_value(
        value, organization_id=organization_id, field_name="previous_sms_url", full_template_reference_only=True
    )


class SMSRepository(BaseRepository):
    _local_advisory_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _postgres_pool_info(self) -> tuple[bool, int | None, int | None]:
        bind: AsyncEngine | None = self.Session.kw.get("bind")
        if bind is None:
            return True, None, None
        engine = bind.sync_engine
        if engine.dialect.name != "postgresql":
            return False, None, None
        pool = engine.pool
        capacity = pool.size() + pool._max_overflow if isinstance(pool, QueuePool) and pool._max_overflow >= 0 else None
        return True, id(pool), capacity

    @asynccontextmanager
    async def _lifecycle_permit(self) -> AsyncIterator[None]:
        is_postgres, pool_key, pool_capacity = self._postgres_pool_info()
        if not is_postgres:
            yield
            return
        if pool_key is None or pool_capacity is None or pool_capacity <= 1:
            async with _POSTGRES_LIFECYCLE_SERIALIZER:
                yield
            return
        semaphore = _POSTGRES_LIFECYCLE_SEMAPHORES.setdefault(
            pool_key,
            asyncio.Semaphore(pool_capacity - 1),
        )
        async with semaphore:
            yield

    @asynccontextmanager
    async def lifecycle_lock(self, organization_id: str, phone_number: str | None = None) -> AsyncIterator[None]:
        """Hold ordered organization and optional phone locks on one database session."""
        lock_keys = [f"twilio-sms:organization:{organization_id}"]
        if phone_number is not None:
            normalized_phone_number = normalize_phone_identifier(phone_number)
            lock_keys.append(f"twilio-sms:phone:{organization_id}:{normalized_phone_number}")
        async with self._lifecycle_permit():
            async with self.Session() as session:
                bind = session.get_bind()
                is_postgres = bind.dialect.name == "postgresql"
                if not is_postgres:
                    acquired_locks: list[asyncio.Lock] = []
                    try:
                        for lock_key in lock_keys:
                            lock = self._local_advisory_locks[lock_key]
                            await lock.acquire()
                            acquired_locks.append(lock)
                        yield
                    finally:
                        for lock in reversed(acquired_locks):
                            lock.release()
                    return
                lock_sql = text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))")
                unlock_sql = text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))")
                acquired_keys: list[str] = []
                try:
                    for lock_key in lock_keys:
                        await session.execute(lock_sql, {"lock_key": lock_key})
                        acquired_keys.append(lock_key)
                    yield
                finally:
                    for lock_key in reversed(acquired_keys):
                        await session.execute(unlock_sql, {"lock_key": lock_key})

    @asynccontextmanager
    async def organization_lock(self, organization_id: str) -> AsyncIterator[None]:
        """Hold the organization lifecycle lock for all Twilio mutations."""
        async with self.lifecycle_lock(organization_id):
            yield

    @db_operation("create_sms_config")
    async def create_sms_config(
        self,
        organization_id: str,
        mode: str,
        webhook_secret: str,
        daily_ingest_cap: int = 100,
    ) -> tuple[SMSConfig, str]:
        if mode not in SMS_CONFIG_MODES:
            raise InvalidSMSConfigMode("Invalid SMS config mode")
        encrypted_webhook_secret = await encryptor.encrypt(webhook_secret, EncryptMethod.AES)
        async with self.Session() as session:
            now = naive_utc_now()
            row = OrganizationSMSConfigModel(
                organization_id=organization_id,
                mode=mode,
                encrypted_webhook_secret=encrypted_webhook_secret,
                webhook_secret_encrypted_method=EncryptMethod.AES.value,
                daily_ingest_cap=daily_ingest_cap,
                created_at=now,
                modified_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                _hide_integrity_error_parameters(error)
                await session.rollback()
                if mode != "connected":
                    raise
                winner = await session.scalar(
                    select(OrganizationSMSConfigModel).where(
                        OrganizationSMSConfigModel.organization_id == organization_id,
                        OrganizationSMSConfigModel.mode == "connected",
                        OrganizationSMSConfigModel.deleted_at.is_(None),
                    )
                )
                if winner is None:
                    raise
                winner_secret = await encryptor.decrypt(
                    winner.encrypted_webhook_secret,
                    EncryptMethod(winner.webhook_secret_encrypted_method),
                )
                return SMSConfig.model_validate(winner), winner_secret
            await session.refresh(row)
            return SMSConfig.model_validate(row), webhook_secret

    @db_operation("get_sms_config_with_secret")
    async def get_sms_config_with_secret(
        self,
        organization_id: str,
        sms_config_id: str,
    ) -> tuple[SMSConfig, str] | None:
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationSMSConfigModel).where(
                    OrganizationSMSConfigModel.organization_id == organization_id,
                    OrganizationSMSConfigModel.sms_config_id == sms_config_id,
                    OrganizationSMSConfigModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            webhook_secret = await encryptor.decrypt(
                row.encrypted_webhook_secret,
                EncryptMethod(row.webhook_secret_encrypted_method),
            )
            return SMSConfig.model_validate(row), webhook_secret

    @db_operation("get_sms_config_with_secret_for_inbound")
    async def get_sms_config_with_secret_for_inbound(self, sms_config_id: str) -> tuple[SMSConfig, str] | None:
        """Only for the unauthenticated inbound webhook; this lookup is intentionally unscoped.

        The webhook binds the tenant by sms_config_id plus destination number.
        """
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationSMSConfigModel).where(
                    OrganizationSMSConfigModel.sms_config_id == sms_config_id,
                    OrganizationSMSConfigModel.deleted_at.is_(None),
                )
            )
            if row is None:
                return None
            webhook_secret = await encryptor.decrypt(
                row.encrypted_webhook_secret,
                EncryptMethod(row.webhook_secret_encrypted_method),
            )
            return SMSConfig.model_validate(row), webhook_secret

    @db_operation("get_sms_config")
    async def get_sms_config(
        self,
        sms_config_id: str,
        organization_id: str,
        include_deleted: bool = False,
    ) -> SMSConfig | None:
        async with self.Session() as session:
            query = select(OrganizationSMSConfigModel).where(
                OrganizationSMSConfigModel.sms_config_id == sms_config_id,
                OrganizationSMSConfigModel.organization_id == organization_id,
            )
            if not include_deleted:
                query = query.where(OrganizationSMSConfigModel.deleted_at.is_(None))
            row = await session.scalar(query)
            return SMSConfig.model_validate(row) if row is not None else None

    @db_operation("get_connected_sms_config")
    async def get_connected_sms_config(self, organization_id: str) -> tuple[SMSConfig, str] | None:
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationSMSConfigModel)
                .where(
                    OrganizationSMSConfigModel.organization_id == organization_id,
                    OrganizationSMSConfigModel.mode == "connected",
                    OrganizationSMSConfigModel.deleted_at.is_(None),
                )
                .order_by(OrganizationSMSConfigModel.created_at)
                .limit(1)
            )
            if row is None:
                return None
            webhook_secret = await encryptor.decrypt(
                row.encrypted_webhook_secret,
                EncryptMethod(row.webhook_secret_encrypted_method),
            )
            return SMSConfig.model_validate(row), webhook_secret

    @db_operation("list_sms_configs")
    async def list_sms_configs(self, organization_id: str) -> list[SMSConfig]:
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    select(OrganizationSMSConfigModel)
                    .where(
                        OrganizationSMSConfigModel.organization_id == organization_id,
                        OrganizationSMSConfigModel.deleted_at.is_(None),
                    )
                    .order_by(OrganizationSMSConfigModel.created_at)
                )
            ).all()
            return [SMSConfig.model_validate(row) for row in rows]

    @db_operation("count_active_phone_numbers_for_config")
    async def count_active_phone_numbers_for_config(self, sms_config_id: str, organization_id: str) -> int:
        async with self.Session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OrganizationPhoneNumberModel)
                .where(
                    OrganizationPhoneNumberModel.sms_config_id == sms_config_id,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.status.in_(("active", "quarantined")),
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
            )
            return int(count or 0)

    @db_operation("count_active_twilio_phone_numbers_for_config")
    async def count_active_twilio_phone_numbers_for_config(
        self,
        sms_config_id: str,
        organization_id: str,
    ) -> int:
        async with self.Session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OrganizationPhoneNumberModel)
                .where(
                    OrganizationPhoneNumberModel.sms_config_id == sms_config_id,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.provider == "twilio",
                    OrganizationPhoneNumberModel.status.in_(("active", "quarantined")),
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
            )
            return int(count or 0)

    @db_operation("delete_sms_config")
    async def delete_sms_config(self, sms_config_id: str, organization_id: str) -> None:
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationSMSConfigModel)
                .where(
                    OrganizationSMSConfigModel.sms_config_id == sms_config_id,
                    OrganizationSMSConfigModel.organization_id == organization_id,
                    OrganizationSMSConfigModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return
            active_phone_id = await session.scalar(
                select(OrganizationPhoneNumberModel.phone_number_id)
                .where(
                    OrganizationPhoneNumberModel.sms_config_id == sms_config_id,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.status == "active",
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
                .limit(1)
            )
            if active_phone_id is not None:
                raise SMSConfigHasActivePhoneNumbers("SMS config has active phone numbers")
            row.deleted_at = naive_utc_now()
            row.modified_at = row.deleted_at
            await session.commit()

    @db_operation("create_phone_number")
    async def create_phone_number(
        self,
        organization_id: str,
        sms_config_id: str,
        phone_number: str,
        provider: str = "twilio",
        provider_number_sid: str | None = None,
        previous_sms_url: str | None = None,
        previous_sms_method: str | None = None,
        previous_sms_application_sid: str | None = None,
        credential_id: str | None = None,
        status: str = "active",
    ) -> OrganizationPhoneNumber:
        if not looks_like_phone_identifier(phone_number):
            raise ValueError("phone_number must include a country code and normalize to E.164")
        phone_number = normalize_phone_identifier(phone_number)
        if not is_e164_phone_number(phone_number):
            raise ValueError("phone_number must include a country code and normalize to E.164")
        async with self.Session() as session:
            configs = await _lock_sms_configs(session, organization_id, {sms_config_id})
            if sms_config_id not in configs:
                raise ValueError("SMS config does not belong to the organization")
            if credential_id is not None:
                credential_exists = await session.scalar(
                    select(CredentialModel.credential_id).where(
                        CredentialModel.credential_id == credential_id,
                        CredentialModel.organization_id == organization_id,
                        CredentialModel.deleted_at.is_(None),
                    )
                )
                if credential_exists is None:
                    raise ValueError("Credential does not belong to the organization")
            now = naive_utc_now()
            row = OrganizationPhoneNumberModel(
                organization_id=organization_id,
                sms_config_id=sms_config_id,
                phone_number=phone_number,
                provider=provider,
                provider_number_sid=provider_number_sid,
                previous_sms_url=await _encrypt_previous_sms_url(previous_sms_url, organization_id),
                previous_sms_method=previous_sms_method,
                previous_sms_application_sid=previous_sms_application_sid,
                credential_id=credential_id,
                created_at=now,
                modified_at=now,
                status=status,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                _hide_integrity_error_parameters(error)
                await session.rollback()
                if _is_phone_number_unique_violation(error):
                    raise PhoneNumberAlreadyExists(
                        "Phone number is already registered for this organization"
                    ) from error
                raise
            await session.refresh(row)
            return OrganizationPhoneNumber.model_validate(row)

    @db_operation("get_phone_number_by_number")
    async def get_phone_number_by_number(
        self,
        phone_number: str,
        organization_id: str,
    ) -> OrganizationPhoneNumber | None:
        if not looks_like_phone_identifier(phone_number):
            return None
        phone_number = normalize_phone_identifier(phone_number)
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationPhoneNumberModel)
                .where(
                    OrganizationPhoneNumberModel.phone_number == phone_number,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
                .order_by(
                    case((OrganizationPhoneNumberModel.status == "active", 0), else_=1),
                    OrganizationPhoneNumberModel.modified_at.desc(),
                    OrganizationPhoneNumberModel.phone_number_id.desc(),
                )
            )
            return OrganizationPhoneNumber.model_validate(row) if row is not None else None

    @db_operation("get_phone_number")
    async def get_phone_number(self, phone_number_id: str, organization_id: str) -> OrganizationPhoneNumber | None:
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationPhoneNumberModel).where(
                    OrganizationPhoneNumberModel.phone_number_id == phone_number_id,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
            )
            return OrganizationPhoneNumber.model_validate(row) if row is not None else None

    @db_operation("list_phone_numbers")
    async def list_phone_numbers(
        self,
        organization_id: str,
        sms_config_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[OrganizationPhoneNumber]:
        async with self.Session() as session:
            query = select(OrganizationPhoneNumberModel).where(
                OrganizationPhoneNumberModel.organization_id == organization_id
            )
            if sms_config_id is not None:
                query = query.where(OrganizationPhoneNumberModel.sms_config_id == sms_config_id)
            if not include_deleted:
                query = query.where(OrganizationPhoneNumberModel.deleted_at.is_(None))
            rows = (await session.scalars(query.order_by(OrganizationPhoneNumberModel.created_at))).all()
            return [OrganizationPhoneNumber.model_validate(row) for row in rows]

    @db_operation("update_phone_number")
    async def update_phone_number(
        self,
        phone_number_id: str,
        organization_id: str,
        status: str | None = None,
        provider: str | object = _UNSET,
        sms_config_id: str | object = _UNSET,
        provider_number_sid: str | None | object = _UNSET,
        previous_sms_url: str | None | object = _UNSET,
        previous_sms_method: str | None | object = _UNSET,
        previous_sms_application_sid: str | None | object = _UNSET,
        credential_id: str | None | object = _UNSET,
        quarantined_until: datetime | None | object = _UNSET,
    ) -> OrganizationPhoneNumber | None:
        if status is not None and status not in PHONE_NUMBER_STATUSES:
            raise InvalidPhoneNumberStatus("Invalid phone number status")
        async with self.Session() as session:
            row = await session.scalar(
                select(OrganizationPhoneNumberModel)
                .where(
                    OrganizationPhoneNumberModel.phone_number_id == phone_number_id,
                    OrganizationPhoneNumberModel.organization_id == organization_id,
                    OrganizationPhoneNumberModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return None
            target_sms_config_id = row.sms_config_id if sms_config_id is _UNSET else cast(str, sms_config_id)
            configs = await _lock_sms_configs(session, organization_id, {row.sms_config_id, target_sms_config_id})
            if target_sms_config_id not in configs:
                raise ValueError("SMS config does not belong to the organization")
            if credential_id is not _UNSET and credential_id is not None:
                credential_exists = await session.scalar(
                    select(CredentialModel.credential_id).where(
                        CredentialModel.credential_id == credential_id,
                        CredentialModel.organization_id == organization_id,
                        CredentialModel.deleted_at.is_(None),
                    )
                )
                if credential_exists is None:
                    raise ValueError("Credential does not belong to the organization")
            if status is not None:
                row.status = status
            if provider is not _UNSET:
                row.provider = cast(str, provider)
            if sms_config_id is not _UNSET:
                row.sms_config_id = target_sms_config_id
            if provider_number_sid is not _UNSET:
                row.provider_number_sid = cast(str | None, provider_number_sid)
            if previous_sms_url is not _UNSET:
                row.previous_sms_url = await _encrypt_previous_sms_url(
                    cast(str | None, previous_sms_url), organization_id
                )
            if previous_sms_method is not _UNSET:
                row.previous_sms_method = cast(str | None, previous_sms_method)
            if previous_sms_application_sid is not _UNSET:
                row.previous_sms_application_sid = cast(str | None, previous_sms_application_sid)
            if credential_id is not _UNSET:
                row.credential_id = cast(str | None, credential_id)
            if quarantined_until is not _UNSET:
                row.quarantined_until = to_naive_utc(cast(datetime | None, quarantined_until))
            row.modified_at = naive_utc_now()
            try:
                await session.commit()
            except IntegrityError as error:
                _hide_integrity_error_parameters(error)
                await session.rollback()
                if _is_phone_number_unique_violation(error):
                    raise PhoneNumberAlreadyExists(
                        "Phone number is already registered for this organization"
                    ) from error
                raise
            await session.refresh(row)
            return OrganizationPhoneNumber.model_validate(row)
