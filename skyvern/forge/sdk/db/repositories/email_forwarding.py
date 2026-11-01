from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

import structlog
from sqlalchemy import case, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    CredentialModel,
    OrganizationEmailConfigModel,
    OrganizationForwardingAddressModel,
    OrganizationInboundEmailModel,
    TOTPCodeModel,
)
from skyvern.forge.sdk.schemas.email_forwarding import (
    EmailConfig,
    EmailConfigInternal,
    ForwardingAddress,
    ForwardingAddressStatus,
    ForwardingMatchMode,
    InboundEmail,
    InboundEmailCategory,
)
from skyvern.forge.sdk.schemas.totp_codes import RawTOTPCode
from skyvern.forge.sdk.utils.sanitization import sanitize_postgres_text
from skyvern.utils.email_validation import normalize_email_address

LOG = structlog.get_logger()
MAX_DROP_DIAGNOSTICS_PER_CONFIG = 50
_FORWARDING_ADDRESS_UNIQUE_CONSTRAINT = "uq_organization_forwarding_addresses_email"


class ForwardingAddressAlreadyExists(ValueError):
    pass


class CredentialTOTPBindingConflict(ValueError):
    pass


class CredentialBindingAccessChanged(ValueError):
    pass


def _is_forwarding_address_unique_violation(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == _FORWARDING_ADDRESS_UNIQUE_CONSTRAINT
    message = str(error.orig)
    return (
        _FORWARDING_ADDRESS_UNIQUE_CONSTRAINT in message
        or "UNIQUE constraint failed: organization_forwarding_addresses.email_address" in message
    )


class EmailForwardingRepository(BaseRepository):
    async def _clear_credential_bindings(
        self,
        session: AsyncSession,
        organization_id: str,
        addresses: Sequence[OrganizationForwardingAddressModel],
        now: datetime,
    ) -> None:
        bindings: dict[str, set[str]] = {}
        for address in addresses:
            if address.credential_id:
                bindings.setdefault(address.credential_id, set()).add(normalize_email_address(address.email_address))
        if not bindings:
            return
        credentials = await session.scalars(
            select(CredentialModel)
            .where(
                CredentialModel.organization_id == organization_id,
                CredentialModel.credential_id.in_(bindings),
                CredentialModel.deleted_at.is_(None),
            )
            .order_by(CredentialModel.credential_id)
            .with_for_update()
        )
        for credential in credentials:
            if (
                credential.totp_type == "email"
                and normalize_email_address(credential.totp_identifier or "") in bindings[credential.credential_id]
            ):
                credential.totp_type = "none"
                credential.totp_identifier = None
                credential.modified_at = now

    @db_operation("create_email_config")
    async def create_config(
        self,
        organization_id: str,
        webhook_secret_hash: str,
        daily_inbound_cap: int,
        drop_flagged_spam: bool,
    ) -> EmailConfigInternal:
        async with self.Session() as session:
            row = OrganizationEmailConfigModel(
                organization_id=organization_id,
                mode="webhook",
                webhook_secret_hash=webhook_secret_hash,
                daily_inbound_cap=daily_inbound_cap,
                drop_flagged_spam=drop_flagged_spam,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return EmailConfigInternal.model_validate(row)

    @db_operation("get_email_config")
    async def get_config(self, email_config_id: str, organization_id: str | None = None) -> EmailConfigInternal | None:
        async with self.Session() as session:
            query = select(OrganizationEmailConfigModel).where(
                OrganizationEmailConfigModel.email_config_id == email_config_id,
                OrganizationEmailConfigModel.deleted_at.is_(None),
            )
            if organization_id is not None:
                query = query.where(OrganizationEmailConfigModel.organization_id == organization_id)
            row = (await session.scalars(query)).one_or_none()
            return EmailConfigInternal.model_validate(row) if row else None

    @db_operation("rotate_email_config_secret")
    async def rotate_config_secret(
        self, email_config_id: str, organization_id: str, webhook_secret_hash: str
    ) -> EmailConfigInternal | None:
        async with self.Session() as session:
            now = datetime.now(timezone.utc)
            row = (
                await session.scalars(
                    update(OrganizationEmailConfigModel)
                    .where(
                        OrganizationEmailConfigModel.email_config_id == email_config_id,
                        OrganizationEmailConfigModel.organization_id == organization_id,
                        OrganizationEmailConfigModel.deleted_at.is_(None),
                    )
                    .values(webhook_secret_hash=webhook_secret_hash, modified_at=now)
                    .returning(OrganizationEmailConfigModel)
                )
            ).one_or_none()
            if row is None:
                return None
            result = EmailConfigInternal.model_validate(row)
            await session.commit()
            return result

    @db_operation("list_email_configs")
    async def list_configs(self, organization_id: str) -> list[EmailConfig]:
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    select(OrganizationEmailConfigModel)
                    .where(
                        OrganizationEmailConfigModel.organization_id == organization_id,
                        OrganizationEmailConfigModel.deleted_at.is_(None),
                    )
                    .order_by(OrganizationEmailConfigModel.created_at.desc())
                )
            ).all()
            return [EmailConfig.model_validate(row) for row in rows]

    @db_operation("delete_email_config", expected_errors=(CredentialBindingAccessChanged,))
    async def delete_config(
        self, email_config_id: str, organization_id: str, authorized_credential_ids: set[str]
    ) -> bool:
        now = datetime.now(UTC)
        async with self.Session() as session:
            config = await session.scalar(
                select(OrganizationEmailConfigModel)
                .where(
                    OrganizationEmailConfigModel.email_config_id == email_config_id,
                    OrganizationEmailConfigModel.organization_id == organization_id,
                    OrganizationEmailConfigModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if config is None:
                return False
            addresses = (
                await session.scalars(
                    select(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.email_config_id == email_config_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
            active_addresses = [address for address in addresses if address.status != ForwardingAddressStatus.REVOKED]
            current_credential_ids = {address.credential_id for address in active_addresses if address.credential_id}
            if not current_credential_ids <= authorized_credential_ids:
                raise CredentialBindingAccessChanged("Credential bindings changed; reauthorize before deleting")
            config.deleted_at = now
            config.modified_at = now
            await self._clear_credential_bindings(session, organization_id, active_addresses, now)
            await session.execute(
                update(OrganizationForwardingAddressModel)
                .where(
                    OrganizationForwardingAddressModel.email_config_id == email_config_id,
                    OrganizationForwardingAddressModel.deleted_at.is_(None),
                )
                .values(status=ForwardingAddressStatus.REVOKED, modified_at=now)
            )
            await session.commit()
            return True

    @db_operation(
        "create_forwarding_address",
        expected_errors=(ForwardingAddressAlreadyExists, CredentialTOTPBindingConflict),
    )
    async def create_address(
        self,
        organization_id: str,
        email_config_id: str,
        email_address: str,
        org_slug: str,
        match_mode: str,
        credential_id: str | None,
        sender_allowlist: list[str] | None,
    ) -> ForwardingAddress:
        email_address = normalize_email_address(email_address)
        if credential_id and match_mode == ForwardingMatchMode.ORIGINAL_RECIPIENT:
            raise ValueError("credential_id cannot be used with match_mode=original_recipient")
        async with self.Session() as session:
            locked_config_id = await session.scalar(
                select(OrganizationEmailConfigModel.email_config_id)
                .where(
                    OrganizationEmailConfigModel.email_config_id == email_config_id,
                    OrganizationEmailConfigModel.organization_id == organization_id,
                    OrganizationEmailConfigModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if locked_config_id is None:
                raise NotFoundError(f"Email config {email_config_id} not found")
            if credential_id:
                credential = (
                    await session.scalars(
                        select(CredentialModel)
                        .where(
                            CredentialModel.credential_id == credential_id,
                            CredentialModel.organization_id == organization_id,
                            CredentialModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if credential is None:
                    raise NotFoundError(f"Credential {credential_id} not found")
                has_existing_binding = (
                    credential.totp_type not in (None, "none") or credential.totp_identifier is not None
                )
                if has_existing_binding and (
                    credential.totp_type != "email"
                    or normalize_email_address(credential.totp_identifier or "") != email_address
                ):
                    raise CredentialTOTPBindingConflict("Credential already has a different TOTP binding")
                credential.totp_type = "email"
                credential.totp_identifier = email_address
            row = OrganizationForwardingAddressModel(
                organization_id=organization_id,
                email_config_id=locked_config_id,
                email_address=email_address,
                org_slug=org_slug,
                match_mode=match_mode,
                credential_id=credential_id,
                sender_allowlist=(
                    [sanitize_postgres_text(entry) for entry in sender_allowlist]
                    if sender_allowlist is not None
                    else None
                ),
                status=ForwardingAddressStatus.PENDING,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if _is_forwarding_address_unique_violation(exc):
                    raise ForwardingAddressAlreadyExists("Forwarding address is already registered") from exc
                raise
            await session.refresh(row)
            return ForwardingAddress.model_validate(row)

    @db_operation("list_forwarding_addresses")
    async def list_addresses(self, organization_id: str) -> list[ForwardingAddress]:
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    select(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                    )
                    .order_by(OrganizationForwardingAddressModel.created_at.desc())
                )
            ).all()
            address_ids = [row.forwarding_address_id for row in rows]
            pending_ids: set[str] = set()
            if address_ids:
                confirmations = OrganizationInboundEmailModel.__table__.alias("confirmations")
                later_otps = OrganizationInboundEmailModel.__table__.alias("later_otps")
                pending_ids = set(
                    (
                        await session.scalars(
                            select(confirmations.c.forwarding_address_id)
                            .where(
                                confirmations.c.forwarding_address_id.in_(address_ids),
                                confirmations.c.category == InboundEmailCategory.PROVIDER_CONFIRMATION,
                                ~exists(
                                    select(later_otps.c.inbound_email_id).where(
                                        later_otps.c.forwarding_address_id == confirmations.c.forwarding_address_id,
                                        later_otps.c.category == InboundEmailCategory.OTP,
                                        later_otps.c.created_at > confirmations.c.created_at,
                                    )
                                ),
                            )
                            .distinct()
                        )
                    ).all()
                )
            return [
                ForwardingAddress.model_validate(row).model_copy(
                    update={"has_pending_confirmation": row.forwarding_address_id in pending_ids}
                )
                for row in rows
            ]

    @db_operation("get_forwarding_address")
    async def get_address(self, forwarding_address_id: str, organization_id: str) -> ForwardingAddress | None:
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(OrganizationForwardingAddressModel).where(
                        OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                    )
                )
            ).one_or_none()
            return ForwardingAddress.model_validate(row) if row else None

    @db_operation("set_forwarding_address_status")
    async def set_address_status(
        self,
        forwarding_address_id: str,
        organization_id: str,
        status: ForwardingAddressStatus,
        allowed_current_statuses: tuple[ForwardingAddressStatus, ...],
    ) -> ForwardingAddress | None:
        async with self.Session() as session:
            address_config_id = (
                select(OrganizationForwardingAddressModel.email_config_id)
                .where(
                    OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                    OrganizationForwardingAddressModel.organization_id == organization_id,
                    OrganizationForwardingAddressModel.deleted_at.is_(None),
                )
                .scalar_subquery()
            )
            locked_config_id = await session.scalar(
                select(OrganizationEmailConfigModel.email_config_id)
                .where(
                    OrganizationEmailConfigModel.email_config_id == address_config_id,
                    OrganizationEmailConfigModel.organization_id == organization_id,
                    OrganizationEmailConfigModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if locked_config_id is None:
                return None
            now = datetime.now(timezone.utc)
            if status == ForwardingAddressStatus.REVOKED:
                addresses = (
                    await session.scalars(
                        select(OrganizationForwardingAddressModel).where(
                            OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                            OrganizationForwardingAddressModel.organization_id == organization_id,
                            OrganizationForwardingAddressModel.email_config_id == locked_config_id,
                            OrganizationForwardingAddressModel.deleted_at.is_(None),
                            OrganizationForwardingAddressModel.status.in_(allowed_current_statuses),
                        )
                    )
                ).all()
                await self._clear_credential_bindings(session, organization_id, addresses, now)
            row = (
                await session.scalars(
                    update(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.email_config_id == locked_config_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                        OrganizationForwardingAddressModel.status.in_(allowed_current_statuses),
                    )
                    .values(status=status, modified_at=now)
                    .returning(OrganizationForwardingAddressModel)
                )
            ).one_or_none()
            if row is None:
                return None
            result = ForwardingAddress.model_validate(row)
            await session.commit()
            return result

    @db_operation("rotate_forwarding_address", expected_errors=(ForwardingAddressAlreadyExists,))
    async def rotate_address(
        self, forwarding_address_id: str, organization_id: str, replacement_email: str
    ) -> tuple[ForwardingAddress, ForwardingAddress] | None:
        replacement_email = normalize_email_address(replacement_email)
        async with self.Session() as session:
            now = datetime.now(timezone.utc)
            address_config_id = (
                select(OrganizationForwardingAddressModel.email_config_id)
                .where(
                    OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                    OrganizationForwardingAddressModel.organization_id == organization_id,
                    OrganizationForwardingAddressModel.deleted_at.is_(None),
                )
                .scalar_subquery()
            )
            locked_config_id = await session.scalar(
                select(OrganizationEmailConfigModel.email_config_id)
                .where(
                    OrganizationEmailConfigModel.email_config_id == address_config_id,
                    OrganizationEmailConfigModel.organization_id == organization_id,
                    OrganizationEmailConfigModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if locked_config_id is None:
                return None
            credential: CredentialModel | None = None
            credential_id = await session.scalar(
                select(OrganizationForwardingAddressModel.credential_id).where(
                    OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                    OrganizationForwardingAddressModel.organization_id == organization_id,
                    OrganizationForwardingAddressModel.deleted_at.is_(None),
                    OrganizationForwardingAddressModel.email_config_id == locked_config_id,
                    OrganizationForwardingAddressModel.status.in_(
                        (
                            ForwardingAddressStatus.PENDING,
                            ForwardingAddressStatus.ACTIVE,
                            ForwardingAddressStatus.DISABLED,
                        )
                    ),
                )
            )
            if credential_id:
                credential = (
                    await session.scalars(
                        select(CredentialModel)
                        .where(
                            CredentialModel.credential_id == credential_id,
                            CredentialModel.organization_id == organization_id,
                            CredentialModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).one_or_none()
            old = (
                await session.scalars(
                    update(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                        OrganizationForwardingAddressModel.email_config_id == locked_config_id,
                        OrganizationForwardingAddressModel.status.in_(
                            (
                                ForwardingAddressStatus.PENDING,
                                ForwardingAddressStatus.ACTIVE,
                                ForwardingAddressStatus.DISABLED,
                            )
                        ),
                    )
                    .values(status=ForwardingAddressStatus.REVOKED, modified_at=now)
                    .returning(OrganizationForwardingAddressModel)
                )
            ).one_or_none()
            if old is None:
                return None
            replacement = OrganizationForwardingAddressModel(
                organization_id=organization_id,
                email_config_id=old.email_config_id,
                email_address=replacement_email,
                org_slug=old.org_slug,
                match_mode=old.match_mode,
                credential_id=old.credential_id,
                sender_allowlist=(
                    [sanitize_postgres_text(entry) for entry in old.sender_allowlist]
                    if old.sender_allowlist is not None
                    else None
                ),
                status=ForwardingAddressStatus.PENDING,
            )
            if credential:
                if credential.totp_type == "email" and normalize_email_address(
                    credential.totp_identifier or ""
                ) == normalize_email_address(old.email_address):
                    credential.totp_identifier = replacement_email
                else:
                    LOG.warning(
                        "Skipping credential TOTP binding update during forwarding address rotation",
                        forwarding_address_id=forwarding_address_id,
                        organization_id=organization_id,
                        credential_id=credential.credential_id,
                        credential_totp_type=credential.totp_type,
                    )
            session.add(replacement)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if _is_forwarding_address_unique_violation(exc):
                    raise ForwardingAddressAlreadyExists("Forwarding address is already registered") from exc
                raise
            revoked_result = ForwardingAddress.model_validate(old)
            replacement_result = ForwardingAddress.model_validate(replacement)
            await session.commit()
            return revoked_result, replacement_result

    @db_operation("find_forwarding_address")
    async def find_address(self, email_config_id: str, recipients: list[str]) -> ForwardingAddress | None:
        if not recipients:
            return None
        recipients = list(dict.fromkeys(normalize_email_address(recipient) for recipient in recipients))
        recipient_rank = case(
            {recipient: rank for rank, recipient in enumerate(dict.fromkeys(recipients))},
            value=func.lower(OrganizationForwardingAddressModel.email_address),
            else_=len(recipients),
        )
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.email_config_id == email_config_id,
                        func.lower(OrganizationForwardingAddressModel.email_address).in_(recipients),
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                        OrganizationForwardingAddressModel.status.in_(
                            (ForwardingAddressStatus.PENDING, ForwardingAddressStatus.ACTIVE)
                        ),
                    )
                    .order_by(recipient_rank)
                )
            ).first()
            return ForwardingAddress.model_validate(row) if row else None

    @db_operation("record_inbound_email")
    async def record_inbound(
        self,
        *,
        organization_id: str,
        email_config_id: str,
        daily_inbound_cap: int,
        forwarding_address_id: str,
        external_message_id: str | None,
        from_address: str,
        original_recipient: str | None,
        subject: str,
        body_text: str,
        links: list[dict[str, str]],
        category: InboundEmailCategory,
        verdicts: dict[str, str] | None,
        totp_identifier: str | None = None,
        parser_content: str | None = None,
        received_at: datetime | None = None,
    ) -> tuple[str, InboundEmail | None, RawTOTPCode | None]:
        async with self.Session() as session:
            try:
                config_id = await session.scalar(
                    select(OrganizationEmailConfigModel.email_config_id)
                    .where(
                        OrganizationEmailConfigModel.email_config_id == email_config_id,
                        OrganizationEmailConfigModel.organization_id == organization_id,
                        OrganizationEmailConfigModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if config_id is None:
                    return "inactive", None, None

                address_id = await session.scalar(
                    select(OrganizationForwardingAddressModel.forwarding_address_id)
                    .where(
                        OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                        OrganizationForwardingAddressModel.organization_id == organization_id,
                        OrganizationForwardingAddressModel.email_config_id == email_config_id,
                        OrganizationForwardingAddressModel.deleted_at.is_(None),
                        OrganizationForwardingAddressModel.status.in_(
                            (ForwardingAddressStatus.PENDING, ForwardingAddressStatus.ACTIVE)
                        ),
                    )
                    .with_for_update()
                )
                if address_id is None:
                    return "inactive", None, None
                if external_message_id is not None:
                    inbound_duplicate = await session.scalar(
                        select(OrganizationInboundEmailModel.inbound_email_id).where(
                            OrganizationInboundEmailModel.organization_id == organization_id,
                            OrganizationInboundEmailModel.external_message_id == external_message_id,
                        )
                    )
                    if inbound_duplicate is not None:
                        return "deduped", None, None
                    if totp_identifier is not None and parser_content is not None:
                        totp_duplicate = await session.scalar(
                            select(TOTPCodeModel.totp_code_id).where(
                                TOTPCodeModel.organization_id == organization_id,
                                TOTPCodeModel.external_message_id == external_message_id,
                            )
                        )
                        if totp_duplicate is not None:
                            return "deduped", None, None

                cutoff = datetime.now(timezone.utc) - timedelta(days=1)
                if category in (InboundEmailCategory.OTP, InboundEmailCategory.PROVIDER_CONFIRMATION):
                    recent_count = await session.scalar(
                        select(func.count(OrganizationInboundEmailModel.inbound_email_id))
                        .join(
                            OrganizationForwardingAddressModel,
                            OrganizationForwardingAddressModel.forwarding_address_id
                            == OrganizationInboundEmailModel.forwarding_address_id,
                        )
                        .where(
                            OrganizationForwardingAddressModel.email_config_id == email_config_id,
                            OrganizationInboundEmailModel.created_at >= cutoff,
                            OrganizationInboundEmailModel.category.in_(
                                (InboundEmailCategory.OTP, InboundEmailCategory.PROVIDER_CONFIRMATION)
                            ),
                        )
                    )
                    if int(recent_count or 0) >= daily_inbound_cap:
                        return "over-cap", None, None
                elif category in (InboundEmailCategory.DROPPED_SPAM, InboundEmailCategory.DROPPED_OTHER):
                    recent_drop_count = await session.scalar(
                        select(func.count(OrganizationInboundEmailModel.inbound_email_id))
                        .join(
                            OrganizationForwardingAddressModel,
                            OrganizationForwardingAddressModel.forwarding_address_id
                            == OrganizationInboundEmailModel.forwarding_address_id,
                        )
                        .where(
                            OrganizationForwardingAddressModel.email_config_id == email_config_id,
                            OrganizationInboundEmailModel.created_at >= cutoff,
                            OrganizationInboundEmailModel.category.in_(
                                (InboundEmailCategory.DROPPED_SPAM, InboundEmailCategory.DROPPED_OTHER)
                            ),
                        )
                    )
                    if int(recent_drop_count or 0) >= MAX_DROP_DIAGNOSTICS_PER_CONFIG:
                        LOG.info(
                            "Skipped inbound email drop diagnostic over cap",
                            organization_id=organization_id,
                            email_config_id=email_config_id,
                            forwarding_address_id=forwarding_address_id,
                            drop_diagnostic_cap=MAX_DROP_DIAGNOSTICS_PER_CONFIG,
                        )
                        return "drop-cap", None, None

                raw_row: TOTPCodeModel | None = None
                raw_result: RawTOTPCode | None = None
                if totp_identifier is not None and parser_content is not None:
                    raw_row = TOTPCodeModel(
                        organization_id=organization_id,
                        totp_identifier=totp_identifier,
                        content=parser_content,
                        code=None,
                        otp_type=None,
                        parse_status="raw",
                        source="email_forwarding",
                        external_message_id=external_message_id,
                        created_at=received_at or datetime.now(timezone.utc),
                    )
                    session.add(raw_row)
                    await session.flush()
                    raw_result = RawTOTPCode.model_validate(raw_row)
                inbound = OrganizationInboundEmailModel(
                    organization_id=organization_id,
                    forwarding_address_id=forwarding_address_id,
                    external_message_id=external_message_id,
                    from_address=from_address,
                    original_recipient=original_recipient,
                    subject=subject,
                    body_text=body_text,
                    links=links,
                    category=category,
                    verdicts=verdicts,
                    totp_code_id=raw_row.totp_code_id if raw_row else None,
                    created_at=received_at or datetime.now(timezone.utc),
                )
                session.add(inbound)
                await session.execute(
                    update(OrganizationForwardingAddressModel)
                    .where(
                        OrganizationForwardingAddressModel.forwarding_address_id == forwarding_address_id,
                        OrganizationForwardingAddressModel.status.in_(
                            (ForwardingAddressStatus.PENDING, ForwardingAddressStatus.ACTIVE)
                        ),
                    )
                    .values(
                        last_received_at=datetime.now(timezone.utc),
                        status=(
                            case(
                                (
                                    OrganizationForwardingAddressModel.status == ForwardingAddressStatus.PENDING,
                                    ForwardingAddressStatus.ACTIVE,
                                ),
                                else_=OrganizationForwardingAddressModel.status,
                            )
                            if raw_row is not None
                            else OrganizationForwardingAddressModel.status
                        ),
                    )
                )
                await session.commit()
                await session.refresh(inbound)
                return "recorded", InboundEmail.model_validate(inbound), raw_result
            except IntegrityError:
                await session.rollback()
                if external_message_id is None:
                    raise
                inbound_duplicate = await session.scalar(
                    select(OrganizationInboundEmailModel.inbound_email_id).where(
                        OrganizationInboundEmailModel.organization_id == organization_id,
                        OrganizationInboundEmailModel.external_message_id == external_message_id,
                    )
                )
                totp_duplicate = await session.scalar(
                    select(TOTPCodeModel.totp_code_id).where(
                        TOTPCodeModel.organization_id == organization_id,
                        TOTPCodeModel.external_message_id == external_message_id,
                    )
                )
                if inbound_duplicate or totp_duplicate:
                    return "deduped", None, None
                raise

    @db_operation("list_inbound_emails")
    async def list_inbound_emails(
        self, forwarding_address_id: str, organization_id: str, limit: int, offset: int
    ) -> list[InboundEmail]:
        async with self.Session() as session:
            rows = (
                await session.scalars(
                    select(OrganizationInboundEmailModel)
                    .where(
                        OrganizationInboundEmailModel.forwarding_address_id == forwarding_address_id,
                        OrganizationInboundEmailModel.organization_id == organization_id,
                    )
                    .order_by(OrganizationInboundEmailModel.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [InboundEmail.model_validate(row) for row in rows]
