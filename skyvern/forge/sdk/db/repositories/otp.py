from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import and_, asc, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from skyvern.config import settings
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.models import (
    OTPRequestAcquisitionModel,
    OTPRequestBindingModel,
    TOTPCodeModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.schemas.totp_codes import OTPType, RawTOTPCode, TOTPCode
from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN, normalize_email_address
from skyvern.utils.phone_validation import looks_like_phone_identifier, phone_identifier_candidates

_EXTERNAL_MESSAGE_DEDUPE_INDEX = "uq_totp_codes_org_external_message_id"
_STRICT_CODE_DEDUPE_INDEX = "uq_totp_codes_strict_code"
_STRICT_WEBHOOK_CODE_DEDUPE_INDEX = "uq_totp_codes_strict_webhook_code"
_STRICT_WEBHOOK_SOURCE = "verification_url"
_STRICT_IDENTIFIER_NAMESPACE = "strict:v1:"
_STRICT_PARSED_STATUS = "strict_parsed"
_STRICT_RAW_STATUS = "strict_raw"
_MAX_STRICT_OWNER_DEPTH = 10


def _is_duplicate_external_message(error: IntegrityError) -> bool:
    constraint_name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint_name == _EXTERNAL_MESSAGE_DEDUPE_INDEX:
        return True
    error_text = str(error.orig).lower()
    return (
        _EXTERNAL_MESSAGE_DEDUPE_INDEX in error_text
        or "unique constraint failed: totp_codes.organization_id, totp_codes.external_message_id" in error_text
    )


def _is_duplicate_strict_code(error: IntegrityError) -> bool:
    constraint_name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint_name == _STRICT_CODE_DEDUPE_INDEX:
        return True
    error_text = str(error.orig).lower()
    return (
        _STRICT_CODE_DEDUPE_INDEX in error_text
        or (
            "unique constraint failed: totp_codes.organization_id, totp_codes.otp_request_id, "
            "totp_codes.totp_identifier, totp_codes.code"
        )
        in error_text
    )


def _is_duplicate_strict_webhook_code(error: IntegrityError) -> bool:
    constraint_name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint_name == _STRICT_WEBHOOK_CODE_DEDUPE_INDEX:
        return True
    error_text = str(error.orig).lower()
    return (
        _STRICT_WEBHOOK_CODE_DEDUPE_INDEX in error_text
        or ("unique constraint failed: totp_codes.organization_id, totp_codes.otp_request_id, totp_codes.code")
        in error_text
    )


def _identifier_filter(totp_identifier: str) -> ColumnElement[bool]:
    stripped_identifier = totp_identifier.strip()
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_identifier):
        return func.lower(TOTPCodeModel.totp_identifier) == normalize_email_address(totp_identifier)
    if looks_like_phone_identifier(stripped_identifier):
        return TOTPCodeModel.totp_identifier.in_({totp_identifier, *phone_identifier_candidates(stripped_identifier)})
    return TOTPCodeModel.totp_identifier == totp_identifier


def _normalized_strict_identifier(totp_identifier: str) -> str:
    stripped_identifier = totp_identifier.strip()
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_identifier):
        return normalize_email_address(stripped_identifier)
    if looks_like_phone_identifier(stripped_identifier):
        return next(candidate for candidate in phone_identifier_candidates(stripped_identifier) if candidate.isdigit())
    return stripped_identifier


def _strict_identifier(totp_identifier: str, otp_request_id: str) -> str:
    """Build an opaque storage key that base-version identifier readers cannot match."""
    normalized_identifier = _normalized_strict_identifier(totp_identifier)
    digest = sha256(f"{otp_request_id}\0{normalized_identifier}".encode()).hexdigest()
    return f"{_STRICT_IDENTIFIER_NAMESPACE}{digest}"


def _restore_public_identifier(code: TOTPCode | RawTOTPCode, totp_identifier: str) -> None:
    code.totp_identifier = totp_identifier


class OTPRepository(BaseRepository):
    """Database operations for OTP/TOTP management."""

    @db_operation("seal_strict_browser_acquisition")
    async def seal_strict_browser_acquisition(
        self,
        *,
        acquisition_id: str,
        organization_id: str,
        otp_request_id: str,
        execution_owner_type: str,
        execution_owner_id: str,
        execution_attempt: str,
        worker_pid: int,
        worker_process_started_at: str,
        pod_uid: str | None = None,
        container_identity: str | None = None,
    ) -> bool:
        now = naive_utc_now()
        async with self.Session() as session:
            binding = (
                await session.scalars(
                    select(OTPRequestBindingModel)
                    .where(
                        OTPRequestBindingModel.organization_id == organization_id,
                        OTPRequestBindingModel.otp_request_id == otp_request_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if binding is None:
                return False
            owner_matches = await self._strict_owner_matches(
                session,
                binding=binding,
                organization_id=organization_id,
                otp_request_id=otp_request_id,
                task_id=execution_owner_id if execution_owner_type == "task" else None,
                workflow_run_id=execution_owner_id if execution_owner_type == "workflow_run" else None,
            )
            if not owner_matches:
                return False
            unreleased_acquisition = await session.scalar(
                select(OTPRequestAcquisitionModel.acquisition_id)
                .where(
                    OTPRequestAcquisitionModel.organization_id == organization_id,
                    OTPRequestAcquisitionModel.otp_request_id == otp_request_id,
                    OTPRequestAcquisitionModel.execution_owner_type == execution_owner_type,
                    OTPRequestAcquisitionModel.execution_owner_id == execution_owner_id,
                    OTPRequestAcquisitionModel.lifecycle_state != "released",
                )
                .limit(1)
            )
            if unreleased_acquisition is not None:
                return False
            session.add(
                OTPRequestAcquisitionModel(
                    acquisition_id=acquisition_id,
                    organization_id=organization_id,
                    otp_request_id=otp_request_id,
                    execution_owner_type=execution_owner_type,
                    execution_owner_id=execution_owner_id,
                    execution_attempt=execution_attempt,
                    worker_pid=worker_pid,
                    worker_process_started_at=worker_process_started_at,
                    pod_uid=pod_uid,
                    container_identity=container_identity,
                    lifecycle_state="sealed",
                    acquisition_sealed_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def _strict_owner_matches(
        self,
        session: AsyncSession,
        *,
        binding: OTPRequestBindingModel,
        organization_id: str,
        otp_request_id: str,
        task_id: str | None,
        workflow_run_id: str | None,
    ) -> bool:
        if binding.owner_type == "task":
            return task_id is not None and binding.owner_task_id == task_id
        if binding.owner_type != "workflow_run" or binding.owner_workflow_run_id is None:
            return False
        return (
            await self._strict_workflow_owner_chain(
                session,
                binding=binding,
                organization_id=organization_id,
                otp_request_id=otp_request_id,
                workflow_run_id=workflow_run_id,
            )
            is not None
        )

    async def _strict_workflow_owner_chain(
        self,
        session: AsyncSession,
        *,
        binding: OTPRequestBindingModel,
        organization_id: str,
        otp_request_id: str,
        workflow_run_id: str | None,
    ) -> list[tuple[str, str]] | None:
        """Return the marked execution-to-root chain when it reaches the binding owner."""
        if binding.owner_type != "workflow_run" or binding.owner_workflow_run_id is None:
            return None
        owner_chain: list[tuple[str, str]] = []
        current_run_id = workflow_run_id
        for _ in range(_MAX_STRICT_OWNER_DEPTH + 1):
            if current_run_id is None:
                return None
            run = (
                await session.execute(
                    select(
                        WorkflowRunModel.organization_id,
                        WorkflowRunModel.otp_request_id,
                        WorkflowRunModel.parent_workflow_run_id,
                        WorkflowRunModel.workflow_id,
                    ).where(WorkflowRunModel.workflow_run_id == current_run_id)
                )
            ).one_or_none()
            if run is None or run.organization_id != organization_id or run.otp_request_id != otp_request_id:
                return None
            owner_chain.append((current_run_id, run.workflow_id))
            if current_run_id == binding.owner_workflow_run_id:
                return owner_chain
            current_run_id = run.parent_workflow_run_id
        return None

    @db_operation("claim_strict_webhook_otp_code", log_errors=False)
    async def claim_strict_webhook_otp_code(
        self,
        *,
        organization_id: str,
        otp_request_id: str,
        content: str,
        code: str,
        otp_type: OTPType,
        task_id: str | None,
        workflow_run_id: str | None,
    ) -> bool:
        """Atomically persist and claim one strict verification-URL response.

        The original binding owner (or a marked workflow descendant) must make the
        claim. A repeated response loses the partial-unique insert race, while a
        distinct resend remains eligible for the same request marker.
        """
        if not task_id and not workflow_run_id:
            return False
        now = naive_utc_now()
        async with self.Session() as session:
            binding = (
                await session.scalars(
                    select(OTPRequestBindingModel)
                    .where(
                        OTPRequestBindingModel.organization_id == organization_id,
                        OTPRequestBindingModel.otp_request_id == otp_request_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if binding is None or not await self._strict_owner_matches(
                session,
                binding=binding,
                organization_id=organization_id,
                otp_request_id=otp_request_id,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
            ):
                return False
            row = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=None,
                content=content,
                code=code,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                otp_request_id=otp_request_id,
                source=_STRICT_WEBHOOK_SOURCE,
                otp_type=otp_type,
                # Base-version readers only admit the legacy "parsed" state. Keeping marked
                # deliveries in a distinct state makes the rollout boundary enforceable by
                # old binaries as well as the marker-aware readers below.
                parse_status=_STRICT_PARSED_STATUS,
                claimed_by_task_id=task_id,
                claimed_by_workflow_run_id=workflow_run_id,
                claimed_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                error.hide_parameters = True
                await session.rollback()
                if _is_duplicate_strict_webhook_code(error):
                    return False
                raise
            return True

    @db_operation("record_strict_browser_selection")
    async def record_strict_browser_selection(
        self,
        *,
        acquisition_id: str,
        organization_id: str,
        otp_request_id: str,
        browser_type: str,
        browser_engine: str,
    ) -> bool:
        now = naive_utc_now()
        async with self.Session() as session:
            result = await session.execute(
                update(OTPRequestAcquisitionModel)
                .where(
                    OTPRequestAcquisitionModel.acquisition_id == acquisition_id,
                    OTPRequestAcquisitionModel.organization_id == organization_id,
                    OTPRequestAcquisitionModel.otp_request_id == otp_request_id,
                    OTPRequestAcquisitionModel.lifecycle_state == "sealed",
                    or_(
                        OTPRequestAcquisitionModel.browser_type.is_(None),
                        OTPRequestAcquisitionModel.browser_type == browser_type,
                    ),
                    or_(
                        OTPRequestAcquisitionModel.browser_engine.is_(None),
                        OTPRequestAcquisitionModel.browser_engine == browser_engine,
                    ),
                )
                .values(browser_type=browser_type, browser_engine=browser_engine, modified_at=now)
            )
            await session.commit()
            return bool(result.rowcount)

    @db_operation("record_strict_browser_stop")
    async def record_strict_browser_stop(
        self,
        *,
        acquisition_id: str,
        organization_id: str,
        confirmed: bool,
        failure_reason: str | None = None,
    ) -> bool:
        now = naive_utc_now()
        values = {
            "stop_started_at": func.coalesce(OTPRequestAcquisitionModel.stop_started_at, now),
            "lifecycle_state": "released" if confirmed else "failed",
            "stop_confirmed_at": (
                func.coalesce(OTPRequestAcquisitionModel.stop_confirmed_at, now) if confirmed else None
            ),
            "failure_reason": None if confirmed else failure_reason or "strict browser stop was not confirmed",
            "modified_at": now,
        }
        eligible_states = ("sealed", "failed", "released") if confirmed else ("sealed", "failed")
        async with self.Session() as session:
            result = await session.execute(
                update(OTPRequestAcquisitionModel)
                .where(
                    OTPRequestAcquisitionModel.acquisition_id == acquisition_id,
                    OTPRequestAcquisitionModel.organization_id == organization_id,
                    OTPRequestAcquisitionModel.lifecycle_state.in_(eligible_states),
                )
                .values(**values)
            )
            await session.commit()
            return bool(result.rowcount)

    @db_operation("get_otp_codes")
    async def get_otp_codes(
        self,
        organization_id: str,
        totp_identifier: str,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        otp_type: OTPType | None = None,
        workflow_run_id: str | None = None,
        include_unscoped_workflow_run: bool = False,
        created_after: datetime | None = None,
        limit: int | None = None,
    ) -> list[TOTPCode]:
        """
        1. filter by:
        - organization_id
        - totp_identifier
        - workflow_run_id (optional); include unscoped rows too when requested
        - created_after (optional): only codes created at/after this instant
        2. make sure created_at is within the valid lifespan
        3. sort by task_id/workflow_id/workflow_run_id nullslast and created_at desc
        4. apply an optional limit at the DB layer
        """
        all_null = and_(
            TOTPCodeModel.task_id.is_(None),
            TOTPCodeModel.workflow_id.is_(None),
            TOTPCodeModel.workflow_run_id.is_(None),
        )
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel)
                .filter_by(organization_id=organization_id)
                .filter(TOTPCodeModel.otp_request_id.is_(None))
                .filter(_identifier_filter(totp_identifier))
                .filter_by(parse_status="parsed")
                .filter(TOTPCodeModel.created_at > naive_utc_now() - timedelta(minutes=valid_lifespan_minutes))
            )
            if otp_type:
                query = query.filter(TOTPCodeModel.otp_type == otp_type)
            if workflow_run_id is not None and include_unscoped_workflow_run:
                query = query.filter(
                    or_(TOTPCodeModel.workflow_run_id == workflow_run_id, TOTPCodeModel.workflow_run_id.is_(None))
                )
            elif workflow_run_id is not None:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
            if created_after is not None:
                query = query.filter(TOTPCodeModel.created_at >= created_after)
            query = query.order_by(asc(all_null), TOTPCodeModel.created_at.desc())
            if limit is not None:
                query = query.limit(limit)
            totp_codes = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(code) for code in totp_codes]

    @db_operation("get_otp_codes_by_run")
    async def get_otp_codes_by_run(
        self,
        organization_id: str,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        limit: int = 1,
    ) -> list[TOTPCode]:
        """Get OTP codes matching a specific task or workflow run (no totp_identifier required).

        Used when the agent detects a 2FA page but no TOTP credentials are pre-configured.
        The user submits codes manually via the UI, and this method finds them by run context.
        """
        if not workflow_run_id and not task_id:
            return []
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel)
                .filter_by(organization_id=organization_id)
                .filter(TOTPCodeModel.otp_request_id.is_(None))
                .filter_by(parse_status="parsed")
                .filter(TOTPCodeModel.created_at > naive_utc_now() - timedelta(minutes=valid_lifespan_minutes))
            )
            if workflow_run_id:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
            elif task_id:
                query = query.filter(TOTPCodeModel.task_id == task_id)
            query = query.order_by(TOTPCodeModel.created_at.desc()).limit(limit)
            results = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(r) for r in results]

    @db_operation("get_recent_otp_codes")
    async def get_recent_otp_codes(
        self,
        organization_id: str,
        limit: int = 50,
        offset: int = 0,
        valid_lifespan_minutes: int | None = None,
        otp_type: OTPType | None = None,
        workflow_run_id: str | None = None,
        totp_identifier: str | None = None,
    ) -> list[TOTPCode]:
        """
        Return recent otp codes for an organization ordered by newest first with optional
        workflow_run_id filtering.
        """
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel)
                .filter_by(organization_id=organization_id)
                .filter(TOTPCodeModel.otp_request_id.is_(None))
                .filter_by(parse_status="parsed")
            )

            if valid_lifespan_minutes is not None:
                query = query.filter(
                    TOTPCodeModel.created_at > naive_utc_now() - timedelta(minutes=valid_lifespan_minutes)
                )

            if otp_type:
                query = query.filter(TOTPCodeModel.otp_type == otp_type)
            if workflow_run_id is not None:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
            if totp_identifier:
                query = query.filter(_identifier_filter(totp_identifier))
            query = query.order_by(TOTPCodeModel.created_at.desc(), TOTPCodeModel.totp_code_id.desc())
            query = query.offset(offset).limit(limit)
            totp_codes = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(totp_code) for totp_code in totp_codes]

    @db_operation("count_otp_codes_since")
    async def count_otp_codes_since(
        self,
        organization_id: str,
        totp_identifier: str,
        created_after: datetime,
        source: str | None = None,
    ) -> int:
        async with self.Session() as session:
            query = (
                select(func.count())
                .select_from(TOTPCodeModel)
                .where(
                    TOTPCodeModel.organization_id == organization_id,
                    _identifier_filter(totp_identifier),
                    TOTPCodeModel.created_at >= to_naive_utc(created_after),
                )
            )
            if source is not None:
                query = query.where(TOTPCodeModel.source == source)
            return int((await session.scalar(query)) or 0)

    @db_operation("create_otp_code")
    async def create_otp_code(
        self,
        organization_id: str,
        totp_identifier: str,
        content: str,
        code: str,
        otp_type: OTPType,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        otp_request_id: str | None = None,
        source: str | None = None,
        expired_at: datetime | None = None,
    ) -> TOTPCode:
        stored_identifier = (
            _strict_identifier(totp_identifier, otp_request_id) if otp_request_id is not None else totp_identifier
        )
        async with self.Session() as session:
            new_totp_code = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=stored_identifier,
                content=content,
                code=code,
                # These columns are FKs: a blank string from an API caller is "not set",
                # but only NULL satisfies the constraint.
                task_id=task_id or None,
                workflow_id=workflow_id or None,
                workflow_run_id=workflow_run_id or None,
                otp_request_id=otp_request_id,
                source=source,
                expired_at=to_naive_utc(expired_at),
                otp_type=otp_type,
                parse_status=_STRICT_PARSED_STATUS if otp_request_id is not None else "parsed",
            )
            session.add(new_totp_code)
            # The flush fills Python defaults and RETURNING brings back server defaults, so the row needs no
            # re-read; validate it before commit expires it.
            await session.flush()
            totp_code = TOTPCode.model_validate(new_totp_code)
            await session.commit()
            if otp_request_id is not None:
                _restore_public_identifier(totp_code, totp_identifier)
            return totp_code

    @db_operation("create_otp_code_if_new", log_errors=False)
    async def create_otp_code_if_new(
        self,
        organization_id: str,
        totp_identifier: str,
        content: str,
        code: str,
        otp_type: OTPType,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        otp_request_id: str | None = None,
        source: str | None = None,
        external_message_id: str | None = None,
        expired_at: datetime | None = None,
    ) -> TOTPCode | None:
        stored_identifier = (
            _strict_identifier(totp_identifier, otp_request_id) if otp_request_id is not None else totp_identifier
        )
        async with self.Session() as session:
            if external_message_id is not None:
                existing = await session.scalar(
                    select(TOTPCodeModel).filter_by(
                        organization_id=organization_id,
                        external_message_id=external_message_id,
                    )
                )
                if existing is not None:
                    return None
            row = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=stored_identifier,
                content=content,
                code=code,
                task_id=task_id or None,
                workflow_id=workflow_id or None,
                workflow_run_id=workflow_run_id or None,
                otp_request_id=otp_request_id,
                source=source,
                external_message_id=external_message_id,
                expired_at=to_naive_utc(expired_at),
                otp_type=otp_type,
                parse_status=_STRICT_PARSED_STATUS if otp_request_id is not None else "parsed",
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                error.hide_parameters = True
                await session.rollback()
                if _is_duplicate_external_message(error) or _is_duplicate_strict_code(error):
                    return None
                raise
            await session.refresh(row)
            totp_code = TOTPCode.model_validate(row)
            if otp_request_id is not None:
                _restore_public_identifier(totp_code, totp_identifier)
            return totp_code

    @db_operation("create_raw_otp_code")
    async def create_raw_otp_code(
        self,
        organization_id: str,
        totp_identifier: str,
        content: str,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        otp_request_id: str | None = None,
        source: str | None = None,
        expired_at: datetime | None = None,
    ) -> RawTOTPCode:
        stored_identifier = (
            _strict_identifier(totp_identifier, otp_request_id) if otp_request_id is not None else totp_identifier
        )
        async with self.Session() as session:
            row = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=stored_identifier,
                content=content,
                code=None,
                otp_type=None,
                parse_status=_STRICT_RAW_STATUS if otp_request_id is not None else "raw",
                task_id=task_id or None,
                workflow_id=workflow_id or None,
                workflow_run_id=workflow_run_id or None,
                otp_request_id=otp_request_id,
                source=source,
                expired_at=to_naive_utc(expired_at),
            )
            session.add(row)
            await session.flush()
            raw_code = RawTOTPCode.model_validate(row)
            await session.commit()
            if otp_request_id is not None:
                _restore_public_identifier(raw_code, totp_identifier)
            return raw_code

    @db_operation("create_raw_otp_code_if_new", log_errors=False)
    async def create_raw_otp_code_if_new(
        self,
        organization_id: str,
        totp_identifier: str,
        content: str,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        otp_request_id: str | None = None,
        source: str | None = None,
        external_message_id: str | None = None,
        expired_at: datetime | None = None,
    ) -> RawTOTPCode | None:
        stored_identifier = (
            _strict_identifier(totp_identifier, otp_request_id) if otp_request_id is not None else totp_identifier
        )
        async with self.Session() as session:
            if external_message_id is not None:
                existing = await session.scalar(
                    select(TOTPCodeModel).filter_by(
                        organization_id=organization_id,
                        external_message_id=external_message_id,
                    )
                )
                if existing is not None:
                    return None
            row = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=stored_identifier,
                content=content,
                code=None,
                otp_type=None,
                parse_status=_STRICT_RAW_STATUS if otp_request_id is not None else "raw",
                task_id=task_id or None,
                workflow_id=workflow_id or None,
                workflow_run_id=workflow_run_id or None,
                otp_request_id=otp_request_id,
                source=source,
                external_message_id=external_message_id,
                expired_at=to_naive_utc(expired_at),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                error.hide_parameters = True
                await session.rollback()
                if _is_duplicate_external_message(error):
                    return None
                raise
            await session.refresh(row)
            raw_code = RawTOTPCode.model_validate(row)
            if otp_request_id is not None:
                _restore_public_identifier(raw_code, totp_identifier)
            return raw_code

    @db_operation("get_raw_otp_codes")
    async def get_raw_otp_codes(
        self,
        organization_id: str,
        totp_identifier: str,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        workflow_run_id: str | None = None,
        include_unscoped_workflow_run: bool = False,
        created_after: datetime | None = None,
        excluded_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[RawTOTPCode]:
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel)
                .filter_by(
                    organization_id=organization_id,
                    parse_status="raw",
                )
                .filter(TOTPCodeModel.otp_request_id.is_(None))
                .filter(_identifier_filter(totp_identifier))
                .filter(TOTPCodeModel.created_at > naive_utc_now() - timedelta(minutes=valid_lifespan_minutes))
            )
            if workflow_run_id is not None and include_unscoped_workflow_run:
                query = query.filter(
                    or_(TOTPCodeModel.workflow_run_id == workflow_run_id, TOTPCodeModel.workflow_run_id.is_(None))
                )
            elif workflow_run_id is not None:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
            if created_after is not None:
                query = query.filter(TOTPCodeModel.created_at >= created_after)
            if excluded_ids:
                query = query.filter(TOTPCodeModel.totp_code_id.not_in(excluded_ids))
            query = query.order_by(TOTPCodeModel.created_at.desc())
            if limit is not None:
                query = query.limit(limit)
            rows = (await session.scalars(query)).all()
            return [RawTOTPCode.model_validate(row) for row in rows]

    @db_operation("get_other_run_otp_code_ids")
    async def get_other_run_otp_code_ids(
        self,
        organization_id: str,
        totp_identifier: str,
        workflow_run_id: str,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        include_raw: bool = True,
        limit: int = 50,
    ) -> list[str]:
        """Ids of in-lifespan codes for this identifier that another workflow run owns. Never selects a code.
        Raw rows count only when ``include_raw``, as the poll reads them only when it asked for a type."""
        async with self.Session() as session:
            query = (
                select(TOTPCodeModel.totp_code_id)
                .filter_by(organization_id=organization_id)
                .filter(TOTPCodeModel.otp_request_id.is_(None))
                .filter(_identifier_filter(totp_identifier))
                .filter(TOTPCodeModel.parse_status.in_(("parsed", "raw") if include_raw else ("parsed",)))
                .filter(TOTPCodeModel.workflow_run_id.is_not(None))
                .filter(TOTPCodeModel.workflow_run_id != workflow_run_id)
                .filter(TOTPCodeModel.created_at > naive_utc_now() - timedelta(minutes=valid_lifespan_minutes))
                .order_by(TOTPCodeModel.created_at.desc())
                .limit(limit)
            )
            return list((await session.scalars(query)).all())

    @db_operation("claim_otp_code")
    async def claim_otp_code(
        self,
        organization_id: str,
        totp_identifier: str,
        otp_request_id: str,
        *,
        task_id: str | None = None,
        workflow_id: str | None = None,
        workflow_run_id: str | None = None,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        otp_type: OTPType | None = None,
        created_after: datetime | None = None,
    ) -> TOTPCode | None:
        """Atomically claim one eligible code for one explicit request owner.

        A marked claim never sees an unmarked or differently marked row. At least one execution
        owner is required. Workflow descendants may claim a delivery addressed to any marked
        ancestor through the original binding owner.
        """
        if not task_id and not workflow_run_id:
            return None
        async with self.Session() as session:
            binding = await session.get(OTPRequestBindingModel, (organization_id, otp_request_id))
            if binding is None:
                return None
            owner_chain: list[tuple[str, str]] | None = None
            if binding.owner_type == "workflow_run":
                owner_chain = await self._strict_workflow_owner_chain(
                    session,
                    binding=binding,
                    organization_id=organization_id,
                    otp_request_id=otp_request_id,
                    workflow_run_id=workflow_run_id,
                )
                if owner_chain is None:
                    return None
            elif not await self._strict_owner_matches(
                session,
                binding=binding,
                organization_id=organization_id,
                otp_request_id=otp_request_id,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
            ):
                return None

            now = naive_utc_now()
            predicates: list[ColumnElement[bool]] = [
                TOTPCodeModel.organization_id == organization_id,
                TOTPCodeModel.totp_identifier == _strict_identifier(totp_identifier, otp_request_id),
                TOTPCodeModel.otp_request_id == otp_request_id,
                TOTPCodeModel.parse_status == _STRICT_PARSED_STATUS,
                TOTPCodeModel.claimed_at.is_(None),
                TOTPCodeModel.rejected_at.is_(None),
                TOTPCodeModel.created_at > now - timedelta(minutes=valid_lifespan_minutes),
                or_(TOTPCodeModel.expired_at.is_(None), TOTPCodeModel.expired_at > now),
            ]
            if owner_chain is not None:
                if workflow_id is None:
                    workflow_scope = TOTPCodeModel.workflow_run_id.in_(
                        [owner_run_id for owner_run_id, _ in owner_chain]
                    )
                else:
                    workflow_scope = or_(
                        *(
                            and_(
                                TOTPCodeModel.workflow_run_id == owner_run_id,
                                TOTPCodeModel.workflow_id == owner_workflow_id,
                            )
                            for owner_run_id, owner_workflow_id in owner_chain
                        )
                    )
                if task_id is not None:
                    predicates.append(
                        or_(
                            TOTPCodeModel.task_id == task_id,
                            and_(TOTPCodeModel.task_id.is_(None), workflow_scope),
                        )
                    )
                predicates.append(workflow_scope)
            else:
                predicates.append(TOTPCodeModel.task_id == task_id)
                if workflow_id is not None:
                    predicates.append(TOTPCodeModel.workflow_id == workflow_id)
                if workflow_run_id is not None:
                    predicates.append(TOTPCodeModel.workflow_run_id == workflow_run_id)
            if otp_type is not None:
                predicates.append(TOTPCodeModel.otp_type == otp_type)
            if created_after is not None:
                normalized_created_after = to_naive_utc(created_after)
                assert normalized_created_after is not None
                predicates.append(TOTPCodeModel.created_at >= normalized_created_after)

            candidate_id = (
                select(TOTPCodeModel.totp_code_id)
                .where(*predicates)
                .order_by(TOTPCodeModel.created_at.desc(), TOTPCodeModel.totp_code_id.desc())
                .limit(1)
                .scalar_subquery()
            )
            query = (
                update(TOTPCodeModel)
                .where(
                    TOTPCodeModel.totp_code_id == candidate_id,
                    TOTPCodeModel.claimed_at.is_(None),
                )
                .values(
                    claimed_by_task_id=task_id,
                    claimed_by_workflow_run_id=workflow_run_id,
                    claimed_at=now,
                    modified_at=now,
                )
                .returning(TOTPCodeModel)
            )
            row = (await session.scalars(query)).one_or_none()
            if row is None:
                return None
            claimed = TOTPCode.model_validate(row)
            await session.commit()
            _restore_public_identifier(claimed, totp_identifier)
            return claimed

    @db_operation("reject_claimed_otp_code")
    async def reject_claimed_otp_code(
        self,
        *,
        totp_code_id: str,
        organization_id: str,
        task_id: str | None,
        workflow_run_id: str | None,
    ) -> bool:
        """Permanently reject a claimed strict code for its exact claimant."""
        if not task_id and not workflow_run_id:
            return False
        now = naive_utc_now()
        task_owner = (
            TOTPCodeModel.claimed_by_task_id == task_id
            if task_id is not None
            else TOTPCodeModel.claimed_by_task_id.is_(None)
        )
        workflow_owner = (
            TOTPCodeModel.claimed_by_workflow_run_id == workflow_run_id
            if workflow_run_id is not None
            else TOTPCodeModel.claimed_by_workflow_run_id.is_(None)
        )
        async with self.Session() as session:
            result = await session.execute(
                update(TOTPCodeModel)
                .where(
                    TOTPCodeModel.totp_code_id == totp_code_id,
                    TOTPCodeModel.organization_id == organization_id,
                    TOTPCodeModel.claimed_at.is_not(None),
                    TOTPCodeModel.rejected_at.is_(None),
                    task_owner,
                    workflow_owner,
                )
                .values(rejected_at=now, modified_at=now)
            )
            await session.commit()
            return bool(result.rowcount)

    @db_operation("promote_raw_otp_code")
    async def promote_raw_otp_code(
        self,
        totp_code_id: str,
        organization_id: str,
        code: str,
        otp_type: OTPType,
    ) -> TOTPCode | None:
        async with self.Session() as session:
            query = (
                update(TOTPCodeModel)
                .where(
                    TOTPCodeModel.totp_code_id == totp_code_id,
                    TOTPCodeModel.organization_id == organization_id,
                    TOTPCodeModel.parse_status.in_(("raw", _STRICT_RAW_STATUS)),
                )
                .values(
                    code=code,
                    otp_type=otp_type,
                    parse_status=case(
                        (TOTPCodeModel.otp_request_id.is_not(None), _STRICT_PARSED_STATUS),
                        else_="parsed",
                    ),
                    modified_at=naive_utc_now(),
                )
                .returning(TOTPCodeModel)
            )
            row = (await session.scalars(query)).one_or_none()
            if row is None:
                return None
            # Read the row before committing: commit expires the instance, and the
            # refetch it would trigger runs outside the async greenlet context.
            promoted = TOTPCode.model_validate(row)
            await session.commit()
            return promoted
