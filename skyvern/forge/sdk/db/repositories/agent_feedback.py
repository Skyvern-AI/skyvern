from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import func, select

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import to_naive_utc
from skyvern.forge.sdk.db.models import AgentFeedbackModel
from skyvern.forge.sdk.schemas.feedback import AgentFeedback, AgentFeedbackCategory, AgentFeedbackSeverity

LOG = structlog.get_logger()


class AgentFeedbackRepository(BaseRepository):
    @db_operation("create_agent_feedback", log_errors=False)
    async def create_agent_feedback(
        self,
        *,
        organization_id: str,
        category: AgentFeedbackCategory,
        summary: str,
        severity: AgentFeedbackSeverity | None = None,
        details: str | None = None,
        tool_name: str | None = None,
        context: dict[str, str | None] | None = None,
    ) -> AgentFeedback:
        try:
            async with self.Session() as session:
                row = AgentFeedbackModel(
                    organization_id=organization_id,
                    category=category,
                    severity=severity,
                    summary=summary,
                    details=details,
                    tool_name=tool_name,
                    context=context,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return AgentFeedback.model_validate(row)
        except Exception:
            # Database and validation exceptions can include report text.
            LOG.error("Failed to store agent feedback", organization_id=organization_id)
            raise RuntimeError("Failed to store agent feedback") from None

    @db_operation("count_agent_feedback_since")
    async def count_agent_feedback_since(self, organization_id: str, since: datetime) -> int:
        async with self.Session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(AgentFeedbackModel)
                .where(
                    AgentFeedbackModel.organization_id == organization_id,
                    AgentFeedbackModel.created_at >= to_naive_utc(since),
                )
            )
            return count or 0
