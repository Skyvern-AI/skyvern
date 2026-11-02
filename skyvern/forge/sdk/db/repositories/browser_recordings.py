from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.id import generate_browser_recording_id
from skyvern.forge.sdk.db.models import BrowserRecordingModel, WorkflowModel
from skyvern.schemas.browser_sessions import BrowserRecording


def _to_browser_recording(model: BrowserRecordingModel, workflow_version: int | None = None) -> BrowserRecording:
    return BrowserRecording(
        recording_id=model.recording_id,
        organization_id=model.organization_id,
        recording_attempt_id=model.recording_attempt_id,
        browser_session_id=model.browser_session_id,
        workflow_permanent_id=model.workflow_permanent_id,
        workflow_id=model.workflow_id,
        workflow_version=workflow_version,
        evidence=model.evidence,
        metadata=model.recording_metadata,
        created_at=model.created_at,
        modified_at=model.modified_at,
    )


class BrowserRecordingsRepository(BaseRepository):
    @db_operation("create_browser_recording")
    async def create_recording(
        self,
        *,
        organization_id: str,
        recording_attempt_id: str | None,
        browser_session_id: str,
        workflow_permanent_id: str,
        evidence: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> BrowserRecording:
        recording_id = generate_browser_recording_id()
        attempt_id = recording_attempt_id or recording_id
        async with self.Session() as session:
            existing_query = select(BrowserRecordingModel).where(
                BrowserRecordingModel.organization_id == organization_id,
                BrowserRecordingModel.recording_attempt_id == attempt_id,
                BrowserRecordingModel.deleted_at.is_(None),
            )
            existing = await session.scalar(existing_query)
            if existing is not None:
                if (
                    existing.browser_session_id != browser_session_id
                    or existing.workflow_permanent_id != workflow_permanent_id
                ):
                    raise ValueError("Recording attempt identity does not match the existing recording")
                if existing.workflow_id is None and (
                    existing.evidence != evidence or existing.recording_metadata != metadata
                ):
                    existing.evidence = evidence
                    existing.recording_metadata = metadata
                    await session.commit()
                    await session.refresh(existing)
                return _to_browser_recording(existing)

            model = BrowserRecordingModel(
                recording_id=recording_id,
                organization_id=organization_id,
                recording_attempt_id=attempt_id,
                browser_session_id=browser_session_id,
                workflow_permanent_id=workflow_permanent_id,
                evidence=evidence,
                recording_metadata=metadata,
            )
            session.add(model)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(existing_query)
                if existing is None:
                    raise
                if (
                    existing.browser_session_id != browser_session_id
                    or existing.workflow_permanent_id != workflow_permanent_id
                ):
                    raise ValueError("Recording attempt identity does not match the existing recording")
                if existing.workflow_id is None and (
                    existing.evidence != evidence or existing.recording_metadata != metadata
                ):
                    existing.evidence = evidence
                    existing.recording_metadata = metadata
                    await session.commit()
                    await session.refresh(existing)
                return _to_browser_recording(existing)
            await session.refresh(model)
            return _to_browser_recording(model)

    @db_operation("get_browser_recording")
    async def get_recording(self, recording_id: str, organization_id: str) -> BrowserRecording | None:
        async with self.Session() as session:
            row = (
                await session.execute(
                    select(BrowserRecordingModel, WorkflowModel.version)
                    .outerjoin(WorkflowModel, WorkflowModel.workflow_id == BrowserRecordingModel.workflow_id)
                    .where(
                        BrowserRecordingModel.recording_id == recording_id,
                        BrowserRecordingModel.organization_id == organization_id,
                        BrowserRecordingModel.deleted_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                return None
            return _to_browser_recording(row[0], row[1])
