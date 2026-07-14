from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.models import WorkflowRunCredentialSelectionModel


class WorkflowRunCredentialSelectionsRepository(BaseRepository):
    @db_operation("get_workflow_run_credential_selection")
    async def get_selection(self, workflow_run_id: str, parameter_key: str) -> str | None:
        async with self.Session() as session:
            selection = (
                await session.scalars(
                    select(WorkflowRunCredentialSelectionModel)
                    .where(WorkflowRunCredentialSelectionModel.workflow_run_id == workflow_run_id)
                    .where(WorkflowRunCredentialSelectionModel.parameter_key == parameter_key)
                )
            ).first()
            return selection.credential_id if selection else None

    @db_operation("get_workflow_run_credential_selections_for_run")
    async def get_selections_for_run(self, workflow_run_id: str) -> dict[str, str]:
        async with self.Session() as session:
            rows = (
                await session.execute(
                    select(
                        WorkflowRunCredentialSelectionModel.parameter_key,
                        WorkflowRunCredentialSelectionModel.credential_id,
                    ).where(WorkflowRunCredentialSelectionModel.workflow_run_id == workflow_run_id)
                )
            ).all()
            return {parameter_key: credential_id for parameter_key, credential_id in rows}

    async def _get_selection(self, session: AsyncSession, workflow_run_id: str, parameter_key: str) -> str | None:
        selection = (
            await session.scalars(
                select(WorkflowRunCredentialSelectionModel)
                .where(WorkflowRunCredentialSelectionModel.workflow_run_id == workflow_run_id)
                .where(WorkflowRunCredentialSelectionModel.parameter_key == parameter_key)
            )
        ).first()
        return selection.credential_id if selection else None

    @db_operation("get_latest_workflow_run_credential_selections")
    async def get_latest_selections(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_ids: list[str],
    ) -> dict[str, datetime]:
        if not credential_ids:
            return {}
        async with self.Session() as session:
            rows = (
                await session.execute(
                    select(
                        WorkflowRunCredentialSelectionModel.credential_id,
                        func.max(WorkflowRunCredentialSelectionModel.created_at),
                    )
                    .where(WorkflowRunCredentialSelectionModel.organization_id == organization_id)
                    .where(WorkflowRunCredentialSelectionModel.workflow_permanent_id == workflow_permanent_id)
                    .where(WorkflowRunCredentialSelectionModel.parameter_key == parameter_key)
                    .where(WorkflowRunCredentialSelectionModel.credential_id.in_(credential_ids))
                    .group_by(WorkflowRunCredentialSelectionModel.credential_id)
                )
            ).all()
            return {credential_id: created_at for credential_id, created_at in rows if created_at is not None}

    async def _get_latest_selections(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_ids: list[str],
    ) -> dict[str, datetime]:
        if not credential_ids:
            return {}
        rows = (
            await session.execute(
                select(
                    WorkflowRunCredentialSelectionModel.credential_id,
                    func.max(WorkflowRunCredentialSelectionModel.created_at),
                )
                .where(WorkflowRunCredentialSelectionModel.organization_id == organization_id)
                .where(WorkflowRunCredentialSelectionModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowRunCredentialSelectionModel.parameter_key == parameter_key)
                .where(WorkflowRunCredentialSelectionModel.credential_id.in_(credential_ids))
                .group_by(WorkflowRunCredentialSelectionModel.credential_id)
            )
        ).all()
        return {credential_id: created_at for credential_id, created_at in rows if created_at is not None}

    async def _take_rotation_advisory_lock(self, session: AsyncSession, lock_key: str) -> None:
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else "postgresql"
        if dialect_name not in {"postgresql", "postgres"}:
            return
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))

    @db_operation("create_round_robin_workflow_run_credential_selection", log_errors=False)
    async def create_round_robin_selection(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_ids: list[str],
    ) -> str:
        async with self.Session() as session:
            lock_key = f"wrcs:{organization_id}:{workflow_permanent_id}:{parameter_key}"
            # The lock, idempotency check, LRU read, and insert must stay in this transaction.
            await self._take_rotation_advisory_lock(session, lock_key)

            existing = await self._get_selection(
                session,
                workflow_run_id=workflow_run_id,
                parameter_key=parameter_key,
            )
            if existing:
                return existing

            latest_selections = await self._get_latest_selections(
                session,
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter_key,
                credential_ids=credential_ids,
            )
            unseen = next((candidate for candidate in credential_ids if candidate not in latest_selections), None)
            credential_id = (
                unseen
                if unseen is not None
                else min(credential_ids, key=lambda candidate: latest_selections[candidate])
            )

            selection = WorkflowRunCredentialSelectionModel(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter_key,
                credential_id=credential_id,
            )
            session.add(selection)
            await session.commit()
            return credential_id

    @db_operation("create_workflow_run_credential_selection", log_errors=False)
    async def create_selection(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_id: str,
    ) -> str:
        async with self.Session() as session:
            selection = WorkflowRunCredentialSelectionModel(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                parameter_key=parameter_key,
                credential_id=credential_id,
            )
            session.add(selection)
            await session.commit()
            return credential_id
