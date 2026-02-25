from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, asc, select
from sqlalchemy.exc import SQLAlchemyError

from skyvern.config import settings
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import TaskModel, TOTPCodeModel, WorkflowRunModel
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCode
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB

LOG = structlog.get_logger()


class OTPMixin:
    """Mixin providing OTP/TOTP and 2FA verification database operations.

    Requires: self.Session (from BaseAlchemyDB)
    """

    async def get_otp_codes(
        self: BaseAlchemyDB,
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
            totp_code = (await session.scalars(query)).all()
            return [TOTPCode.model_validate(totp_code) for totp_code in totp_code]

    async def get_otp_codes_by_run(
        self: BaseAlchemyDB,
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

    async def get_recent_otp_codes(
        self: BaseAlchemyDB,
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

    async def create_otp_code(
        self: BaseAlchemyDB,
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

    async def update_task_2fa_state(
        self: BaseAlchemyDB,
        task_id: str,
        organization_id: str,
        waiting_for_verification_code: bool,
        verification_code_identifier: str | None = None,
        verification_code_polling_started_at: datetime | None = None,
    ) -> Task:
        """Update task 2FA verification code waiting state."""
        try:
            async with self.Session() as session:
                if task := (
                    await session.scalars(
                        select(TaskModel).filter_by(task_id=task_id).filter_by(organization_id=organization_id)
                    )
                ).first():
                    task.waiting_for_verification_code = waiting_for_verification_code
                    if verification_code_identifier is not None:
                        task.verification_code_identifier = verification_code_identifier
                    if verification_code_polling_started_at is not None:
                        task.verification_code_polling_started_at = verification_code_polling_started_at
                    if not waiting_for_verification_code:
                        # Clear identifiers when no longer waiting
                        task.verification_code_identifier = None
                        task.verification_code_polling_started_at = None
                    await session.commit()
                    updated_task = await self.get_task(task_id, organization_id=organization_id)
                    if not updated_task:
                        raise NotFoundError("Task not found")
                    return updated_task
                else:
                    raise NotFoundError("Task not found")
        except SQLAlchemyError:
            LOG.error("SQLAlchemyError", exc_info=True)
            raise

    @read_retry()
    async def get_active_verification_requests(self: BaseAlchemyDB, organization_id: str) -> list[dict]:
        """Return active 2FA verification requests for an organization.

        Queries both tasks and workflow runs where waiting_for_verification_code=True.
        Used to provide initial state when a WebSocket notification client connects.
        """
        results: list[dict] = []
        async with self.Session() as session:
            # Tasks waiting for verification (exclude finalized tasks)
            finalized_task_statuses = [s.value for s in TaskStatus if s.is_final()]
            task_rows = (
                await session.scalars(
                    select(TaskModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(waiting_for_verification_code=True)
                    .filter_by(workflow_run_id=None)
                    .filter(TaskModel.status.not_in(finalized_task_statuses))
                    .filter(TaskModel.created_at > datetime.utcnow() - timedelta(hours=1))
                )
            ).all()
            for t in task_rows:
                results.append(
                    {
                        "task_id": t.task_id,
                        "workflow_run_id": None,
                        "verification_code_identifier": t.verification_code_identifier,
                        "verification_code_polling_started_at": (
                            t.verification_code_polling_started_at.isoformat()
                            if t.verification_code_polling_started_at
                            else None
                        ),
                    }
                )
            # Workflow runs waiting for verification (exclude finalized runs)
            finalized_wr_statuses = [s.value for s in WorkflowRunStatus if s.is_final()]
            wr_rows = (
                await session.scalars(
                    select(WorkflowRunModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(waiting_for_verification_code=True)
                    .filter(WorkflowRunModel.status.not_in(finalized_wr_statuses))
                    .filter(WorkflowRunModel.created_at > datetime.utcnow() - timedelta(hours=1))
                )
            ).all()
            for wr in wr_rows:
                results.append(
                    {
                        "task_id": None,
                        "workflow_run_id": wr.workflow_run_id,
                        "verification_code_identifier": wr.verification_code_identifier,
                        "verification_code_polling_started_at": (
                            wr.verification_code_polling_started_at.isoformat()
                            if wr.verification_code_polling_started_at
                            else None
                        ),
                    }
                )
        return results
