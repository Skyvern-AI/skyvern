from __future__ import annotations

from sqlalchemy import select

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import (
    BlockRunModel,
    DebugSessionModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.schemas.debug_sessions import BlockRun, DebugSession, DebugSessionRun
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class DebugRepository(BaseRepository):
    """Database operations for debug sessions and block runs."""

    @db_operation("get_debug_session")
    async def get_debug_session(
        self,
        *,
        organization_id: str,
        user_id: str,
        workflow_permanent_id: str,
    ) -> DebugSession | None:
        async with self.Session() as session:
            debug_session = (
                await session.scalars(
                    select(DebugSessionModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(workflow_permanent_id=workflow_permanent_id)
                    .filter_by(user_id=user_id)
                    .filter_by(deleted_at=None)
                    .filter_by(status="created")
                    .order_by(DebugSessionModel.created_at.desc())
                )
            ).first()

            if not debug_session:
                return None

            return DebugSession.model_validate(debug_session)

    @db_operation("get_latest_block_run")
    async def get_latest_block_run(
        self,
        *,
        organization_id: str,
        user_id: str,
        block_label: str,
    ) -> BlockRun | None:
        async with self.Session() as session:
            query = (
                select(BlockRunModel)
                .filter_by(organization_id=organization_id)
                .filter_by(user_id=user_id)
                .filter_by(block_label=block_label)
                .order_by(BlockRunModel.created_at.desc())
            )

            model = (await session.scalars(query)).first()

            return BlockRun.model_validate(model) if model else None

    @db_operation("get_latest_completed_block_run")
    async def get_latest_completed_block_run(
        self,
        *,
        organization_id: str,
        user_id: str,
        block_label: str,
        workflow_permanent_id: str,
    ) -> BlockRun | None:
        async with self.Session() as session:
            query = (
                select(BlockRunModel)
                .join(WorkflowRunModel, BlockRunModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .filter(BlockRunModel.organization_id == organization_id)
                .filter(BlockRunModel.user_id == user_id)
                .filter(BlockRunModel.block_label == block_label)
                .filter(WorkflowRunModel.status == WorkflowRunStatus.completed)
                .filter(WorkflowRunModel.workflow_permanent_id == workflow_permanent_id)
                .order_by(BlockRunModel.created_at.desc())
            )

            model = (await session.scalars(query)).first()

            return BlockRun.model_validate(model) if model else None

    @db_operation("create_block_run")
    async def create_block_run(
        self,
        *,
        organization_id: str,
        user_id: str,
        block_label: str,
        output_parameter_id: str,
        workflow_run_id: str,
    ) -> None:
        async with self.Session() as session:
            block_run = BlockRunModel(
                organization_id=organization_id,
                user_id=user_id,
                block_label=block_label,
                output_parameter_id=output_parameter_id,
                workflow_run_id=workflow_run_id,
            )

            session.add(block_run)

            await session.commit()

    @db_operation("get_debug_session_by_browser_session_id")
    async def get_debug_session_by_browser_session_id(
        self,
        browser_session_id: str,
        organization_id: str,
    ) -> DebugSession | None:
        async with self.Session() as session:
            query = (
                select(DebugSessionModel)
                .filter_by(browser_session_id=browser_session_id)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
            )
            model = (await session.scalars(query)).first()
            return DebugSession.model_validate(model) if model else None

    @db_operation("get_debug_session_by_id")
    async def get_debug_session_by_id(
        self,
        debug_session_id: str,
        organization_id: str,
    ) -> DebugSession | None:
        async with self.Session() as session:
            query = (
                select(DebugSessionModel)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
                .filter_by(debug_session_id=debug_session_id)
            )

            model = (await session.scalars(query)).first()

            return DebugSession.model_validate(model) if model else None

    @db_operation("get_workflow_runs_by_debug_session_id")
    async def get_workflow_runs_by_debug_session_id(
        self,
        debug_session_id: str,
        organization_id: str,
    ) -> list[DebugSessionRun]:
        async with self.Session() as session:
            query = (
                select(WorkflowRunModel, BlockRunModel)
                .join(BlockRunModel, BlockRunModel.workflow_run_id == WorkflowRunModel.workflow_run_id)
                .filter(WorkflowRunModel.organization_id == organization_id)
                .filter(WorkflowRunModel.debug_session_id == debug_session_id)
                .order_by(WorkflowRunModel.created_at.desc())
            )

            results = (await session.execute(query)).all()

            debug_session_runs = []
            for workflow_run, block_run in results:
                debug_session_runs.append(
                    DebugSessionRun(
                        ai_fallback=workflow_run.ai_fallback,
                        block_label=block_run.block_label,
                        browser_session_id=workflow_run.browser_session_id,
                        code_gen=workflow_run.code_gen,
                        debug_session_id=workflow_run.debug_session_id,
                        failure_reason=workflow_run.failure_reason,
                        output_parameter_id=block_run.output_parameter_id,
                        run_with=workflow_run.run_with,
                        script_run_id=workflow_run.script_run.get("script_run_id") if workflow_run.script_run else None,
                        status=workflow_run.status,
                        workflow_id=workflow_run.workflow_id,
                        workflow_permanent_id=workflow_run.workflow_permanent_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                        created_at=workflow_run.created_at,
                        queued_at=workflow_run.queued_at,
                        started_at=workflow_run.started_at,
                        finished_at=workflow_run.finished_at,
                    )
                )

            return debug_session_runs

    @db_operation("complete_debug_sessions")
    async def complete_debug_sessions(
        self,
        *,
        organization_id: str,
        user_id: str | None = None,
        workflow_permanent_id: str | None = None,
    ) -> list[DebugSession]:
        async with self.Session() as session:
            query = (
                select(DebugSessionModel)
                .filter_by(organization_id=organization_id)
                .filter_by(deleted_at=None)
                .filter_by(status="created")
            )

            if user_id:
                query = query.filter_by(user_id=user_id)
            if workflow_permanent_id:
                query = query.filter_by(workflow_permanent_id=workflow_permanent_id)

            models = (await session.scalars(query)).all()

            for model in models:
                model.status = "completed"

            debug_sessions = [DebugSession.model_validate(model) for model in models]

            await session.commit()

            return debug_sessions

    @db_operation("create_debug_session")
    async def create_debug_session(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        user_id: str,
        workflow_permanent_id: str,
        vnc_streaming_supported: bool,
    ) -> DebugSession:
        async with self.Session() as session:
            debug_session = DebugSessionModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                user_id=user_id,
                browser_session_id=browser_session_id,
                vnc_streaming_supported=vnc_streaming_supported,
                status="created",
            )

            session.add(debug_session)
            await session.commit()
            await session.refresh(debug_session)

            return DebugSession.model_validate(debug_session)
