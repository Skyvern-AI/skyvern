from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, asc, select

from skyvern.config import settings
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.models import TOTPCodeModel
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCode

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory


class OTPMixin:
    """Database operations for OTP/TOTP management."""

    Session: _SessionFactory

    @db_operation("get_otp_codes")
    async def get_otp_codes(
        self,
        organization_id: str,
        totp_identifier: str,
        valid_lifespan_minutes: int = settings.TOTP_LIFESPAN_MINUTES,
        otp_type: OTPType | None = None,
        workflow_run_id: str | None = None,
        limit: int | None = None,
    ) -> list[TOTPCode]:
        """
        1. filter by:
        - organization_id
        - totp_identifier
        - workflow_run_id (optional)
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
                .filter_by(totp_identifier=totp_identifier)
                .filter(TOTPCodeModel.created_at > datetime.utcnow() - timedelta(minutes=valid_lifespan_minutes))
            )
            if otp_type:
                query = query.filter(TOTPCodeModel.otp_type == otp_type)
            if workflow_run_id is not None:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
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
                .filter(TOTPCodeModel.created_at > datetime.utcnow() - timedelta(minutes=valid_lifespan_minutes))
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
            query = select(TOTPCodeModel).filter_by(organization_id=organization_id)

            if valid_lifespan_minutes is not None:
                query = query.filter(
                    TOTPCodeModel.created_at > datetime.utcnow() - timedelta(minutes=valid_lifespan_minutes)
                )

            if otp_type:
                query = query.filter(TOTPCodeModel.otp_type == otp_type)
            if workflow_run_id is not None:
                query = query.filter(TOTPCodeModel.workflow_run_id == workflow_run_id)
            if totp_identifier:
                query = query.filter(TOTPCodeModel.totp_identifier == totp_identifier)
            query = query.order_by(TOTPCodeModel.created_at.desc()).limit(limit)
            totp_codes = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(totp_code) for totp_code in totp_codes]

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
        source: str | None = None,
        expired_at: datetime | None = None,
    ) -> TOTPCode:
        async with self.Session() as session:
            new_totp_code = TOTPCodeModel(
                organization_id=organization_id,
                totp_identifier=totp_identifier,
                content=content,
                code=code,
                task_id=task_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                source=source,
                expired_at=expired_at,
                otp_type=otp_type,
            )
            session.add(new_totp_code)
            await session.commit()
            await session.refresh(new_totp_code)
            return TOTPCode.model_validate(new_totp_code)
