from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql, sqlite

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.id import generate_run_feedback_id
from skyvern.forge.sdk.db.models import RunFeedbackModel
from skyvern.forge.sdk.schemas.feedback import FeedbackRating, RunFeedback, RunFeedbackTargetType


class RunFeedbackRepository(BaseRepository):
    @db_operation("upsert_run_feedback")
    async def upsert_run_feedback(
        self,
        *,
        organization_id: str,
        target_type: RunFeedbackTargetType,
        target_id: str,
        context_id: str | None,
        rating: FeedbackRating,
        reason: str | None,
        needs_support: bool,
        submitted_by: str | None,
    ) -> RunFeedback:
        now = naive_utc_now()
        values = {
            "context_id": context_id,
            "rating": rating,
            "reason": reason or None,
            "needs_support": needs_support,
            "submitted_by": submitted_by or None,
            "modified_at": now,
        }
        async with self.Session() as session:
            dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
            insert = sqlite.insert if dialect_name == "sqlite" else postgresql.insert
            statement = insert(RunFeedbackModel.__table__).values(
                run_feedback_id=generate_run_feedback_id(),
                organization_id=organization_id,
                target_type=target_type,
                target_id=target_id,
                created_at=now,
                **values,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["organization_id", "target_type", "target_id"],
                    set_={field: getattr(statement.excluded, field) for field in values},
                )
            )
            await session.commit()
            row = (
                await session.scalars(
                    select(RunFeedbackModel).filter_by(
                        organization_id=organization_id, target_type=target_type, target_id=target_id
                    )
                )
            ).one()
            return RunFeedback.model_validate(row)

    @db_operation("get_run_feedback")
    async def get_run_feedback(
        self, *, organization_id: str, target_type: RunFeedbackTargetType, target_id: str
    ) -> RunFeedback | None:
        async with self.Session() as session:
            row = (
                await session.scalars(
                    select(RunFeedbackModel).filter_by(
                        organization_id=organization_id, target_type=target_type, target_id=target_id
                    )
                )
            ).first()
            return RunFeedback.model_validate(row) if row else None

    @db_operation("delete_run_feedback")
    async def delete_run_feedback(
        self, *, organization_id: str, target_type: RunFeedbackTargetType, target_id: str
    ) -> None:
        async with self.Session() as session:
            await session.execute(
                delete(RunFeedbackModel)
                .where(RunFeedbackModel.organization_id == organization_id)
                .where(RunFeedbackModel.target_type == target_type)
                .where(RunFeedbackModel.target_id == target_id)
            )
            await session.commit()
