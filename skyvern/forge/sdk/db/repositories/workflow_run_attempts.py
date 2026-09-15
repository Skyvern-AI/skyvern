from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Literal, TypedDict

from sqlalchemy import (
    JSON,
    ColumnElement,
    and_,
    bindparam,
    case,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    type_coerce,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Load, with_expression

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now, to_naive_utc
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import TaskRunModel, WorkflowRunAttemptModel, WorkflowRunModel
from skyvern.schemas.run_enums import TERMINAL_STATUSES

# Keep terminal recovery bounded so a degraded database does not load the entire backlog at once.
ATTEMPT_RECOVERY_BATCH_SIZE = 200
# A sweep handles at most this many pages (20,000 rows). The next periodic sweep resumes from a
# fresh keyset, so an outage cannot make one executor loop unbounded.
ATTEMPT_RECOVERY_MAX_PAGES = 100

_undecided_recovery_attempt: ContextVar[WorkflowRunAttemptModel | None] = ContextVar(
    "undecided_recovery_attempt", default=None
)


class AttemptRecoverySuperseded(Exception):
    pass


@contextmanager
def recovering_undecided_attempt(attempt: WorkflowRunAttemptModel) -> Iterator[None]:
    token = _undecided_recovery_attempt.set(attempt)
    try:
        yield
    finally:
        _undecided_recovery_attempt.reset(token)


class TerminalSideEffectCheckpoint(TypedDict):
    completed_effects: list[str]
    final_hook_invoked: bool


def _progress_object(column: ColumnElement, dialect: str) -> ColumnElement:
    if dialect == "sqlite":
        return case((func.json_type(column) == "object", column), else_=literal({}, type_=JSON))
    value = cast(column, JSONB)
    return case((func.jsonb_typeof(value) == "object", value), else_=literal({}, type_=JSONB))


def progress_without_outputs(column: ColumnElement, dialect: str) -> ColumnElement:
    if dialect == "sqlite":
        return type_coerce(func.json_remove(column, "$.output_parameters"), JSON)
    value = cast(column, JSONB)
    return cast(case((func.jsonb_typeof(value) == "object", value - "output_parameters"), else_=value), JSON)


def merge_attempt_progress(column: ColumnElement, updates: dict[str, Any], dialect: str) -> ColumnElement:
    result = _progress_object(column, dialect)
    if dialect == "sqlite":
        for key, value in updates.items():
            result = func.json_set(result, f"$.{key}", func.json(literal(value, type_=JSON)))
        return type_coerce(result, JSON)
    return cast(result.op("||")(literal(updates, type_=JSONB)), JSON)


def attempt_metadata_options(dialect: str) -> Load:
    return with_expression(
        WorkflowRunAttemptModel.interim_side_effects_progress,
        progress_without_outputs(WorkflowRunAttemptModel.interim_side_effects_progress, dialect),
    )


def _stale_dispatch_claim_conditions(stale_before: datetime) -> ColumnElement[bool]:
    return and_(
        WorkflowRunModel.status == "queued",
        WorkflowRunModel.started_at.isnot(None),
        WorkflowRunAttemptModel.workflow_run_id == WorkflowRunModel.workflow_run_id,
        WorkflowRunAttemptModel.organization_id == WorkflowRunModel.organization_id,
        WorkflowRunAttemptModel.status == "running",
        WorkflowRunAttemptModel.started_at.isnot(None),
        WorkflowRunAttemptModel.retry_decision.is_(None),
        WorkflowRunAttemptModel.modified_at < stale_before,
    )


class WorkflowRunAttemptsRepository(BaseRepository):
    @db_operation("create_attempt")
    async def create_attempt(
        self,
        workflow_run_id: str,
        organization_id: str,
        attempt_number: int,
        status: str,
        pinned_browser_session_id: str | None = None,
    ) -> WorkflowRunAttemptModel:
        async with self.Session() as session:
            attempt = WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                attempt_number=attempt_number,
                status=status,
                pinned_browser_session_id=pinned_browser_session_id,
            )
            session.add(attempt)
            await session.flush()
            session.expunge(attempt)
            await session.commit()
            return attempt

    @db_operation("pin_first_attempt_browser_session_if_unset")
    async def pin_first_attempt_browser_session_if_unset(
        self,
        workflow_run_id: str,
        organization_id: str,
        browser_session_id: str,
    ) -> None:
        async with self.Session() as session:
            await session.execute(
                update(WorkflowRunAttemptModel)
                .where(
                    WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                    WorkflowRunAttemptModel.organization_id == organization_id,
                    WorkflowRunAttemptModel.attempt_number == 1,
                    WorkflowRunAttemptModel.pinned_browser_session_id.is_(None),
                )
                .values(pinned_browser_session_id=browser_session_id, modified_at=naive_utc_now())
            )
            await session.commit()

    @db_operation("get_attempts", expected_errors=(AttemptRecoverySuperseded,))
    async def get_attempts(self, workflow_run_id: str) -> list[WorkflowRunAttemptModel]:
        async with self.Session() as session:
            attempts = (
                await session.scalars(
                    select(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                    .where(WorkflowRunAttemptModel.workflow_run_id == workflow_run_id)
                    .order_by(WorkflowRunAttemptModel.attempt_number)
                )
            ).all()
            recovering = _undecided_recovery_attempt.get()
            if recovering is not None and recovering.workflow_run_id == workflow_run_id:
                current = max(attempts, key=lambda row: row.attempt_number, default=None)
                if (
                    current is None
                    or current.attempt_number != recovering.attempt_number
                    or current.retry_decision is not None
                    or current.started_at is None
                    or current.modified_at != recovering.modified_at
                ):
                    raise AttemptRecoverySuperseded
            return list(attempts)

    @db_operation("get_interim_payload_snapshot")
    async def get_interim_payload_snapshot(self, workflow_run_id: str, attempt_number: int) -> dict[str, Any]:
        async with self.Session() as session:
            return dict(
                await session.scalar(
                    select(WorkflowRunAttemptModel.interim_side_effects_progress).where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                    )
                )
                or {}
            )

    @db_operation("get_latest_attempts_for_runs")
    async def get_latest_attempts_for_runs(self, workflow_run_ids: Sequence[str]) -> dict[str, WorkflowRunAttemptModel]:
        if not workflow_run_ids:
            return {}

        ranked_attempts = (
            select(
                WorkflowRunAttemptModel.workflow_run_id,
                WorkflowRunAttemptModel.attempt_number,
                func.row_number()
                .over(
                    partition_by=WorkflowRunAttemptModel.workflow_run_id,
                    order_by=WorkflowRunAttemptModel.attempt_number.desc(),
                )
                .label("attempt_rank"),
            )
            .where(WorkflowRunAttemptModel.workflow_run_id.in_(workflow_run_ids))
            .subquery()
        )
        query = select(WorkflowRunAttemptModel).join(
            ranked_attempts,
            and_(
                WorkflowRunAttemptModel.workflow_run_id == ranked_attempts.c.workflow_run_id,
                WorkflowRunAttemptModel.attempt_number == ranked_attempts.c.attempt_number,
                ranked_attempts.c.attempt_rank == 1,
            ),
        )

        async with self.Session() as session:
            attempts = (await session.scalars(query.options(attempt_metadata_options(session.bind.dialect.name)))).all()
            return {attempt.workflow_run_id: attempt for attempt in attempts}

    @db_operation("list_stale_pending_retries")
    async def list_stale_pending_retries(self, cutoff: datetime) -> list[WorkflowRunAttemptModel]:
        normalized_cutoff = to_naive_utc(cutoff)
        assert normalized_cutoff is not None
        async with self.Session() as session:
            attempts = (
                await session.scalars(
                    select(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                    .where(
                        WorkflowRunAttemptModel.retry_decision == "retry",
                        WorkflowRunAttemptModel.next_attempt_prepared_at.is_(None),
                        WorkflowRunAttemptModel.next_attempt_at.isnot(None),
                        WorkflowRunAttemptModel.next_attempt_at < normalized_cutoff,
                    )
                    .order_by(
                        WorkflowRunAttemptModel.next_attempt_at.asc(),
                        WorkflowRunAttemptModel.workflow_run_id.asc(),
                        WorkflowRunAttemptModel.attempt_number.asc(),
                    )
                )
            ).all()
            return list(attempts)

    @db_operation("list_attempts_needing_recovery")
    async def list_attempts_needing_recovery(
        self,
        cutoff: datetime,
        *,
        cursor: tuple[datetime, str, int] | None = None,
        limit: int = ATTEMPT_RECOVERY_BATCH_SIZE,
    ) -> list[WorkflowRunAttemptModel]:
        normalized_cutoff = to_naive_utc(cutoff)
        assert normalized_cutoff is not None
        if limit <= 0:
            raise ValueError("Recovery page limit must be positive")
        async with self.Session() as session:
            retry_attempts = and_(
                WorkflowRunAttemptModel.retry_decision == "retry",
                WorkflowRunAttemptModel.next_attempt_prepared_at.is_(None),
                WorkflowRunAttemptModel.next_attempt_at.isnot(None),
            )
            terminal_attempts = and_(
                WorkflowRunAttemptModel.retry_decision.in_(
                    ["final", "revoked", "abandoned"],
                ),
                or_(
                    WorkflowRunAttemptModel.side_effects_released_at.is_(None),
                    WorkflowRunAttemptModel.side_effects_released_at < normalized_cutoff,
                ),
                WorkflowRunAttemptModel.webhook_sent_at.is_(None),
            )
            interim_attempts = and_(
                WorkflowRunAttemptModel.retry_decision == "retry",
                WorkflowRunAttemptModel.next_attempt_prepared_at.isnot(None),
                WorkflowRunAttemptModel.interim_webhook_sent_at.is_(None),
                or_(
                    WorkflowRunAttemptModel.side_effects_released_at.is_(None),
                    WorkflowRunAttemptModel.side_effects_released_at < normalized_cutoff,
                ),
            )
            prepared_attempts = and_(
                WorkflowRunAttemptModel.retry_decision.is_(None),
                WorkflowRunAttemptModel.status == "queued",
                WorkflowRunAttemptModel.started_at.is_(None),
                WorkflowRunAttemptModel.modified_at < normalized_cutoff,
            )
            # A run can reach a terminal status before its attempt starts (a cancel while queued), so
            # the attempt's started_at does not gate this branch.
            undecided_terminal_attempts = and_(
                WorkflowRunAttemptModel.retry_decision.is_(None),
                WorkflowRunAttemptModel.modified_at < normalized_cutoff,
                WorkflowRunModel.status.in_(TERMINAL_STATUSES),
            )
            decision_timestamp = func.coalesce(
                WorkflowRunAttemptModel.next_attempt_at,
                WorkflowRunAttemptModel.modified_at,
            )
            conditions = [
                or_(retry_attempts, terminal_attempts, interim_attempts, prepared_attempts, undecided_terminal_attempts)
            ]
            if cursor is not None:
                cursor_timestamp, cursor_workflow_run_id, cursor_attempt_number = cursor
                normalized_cursor_timestamp = to_naive_utc(cursor_timestamp)
                assert normalized_cursor_timestamp is not None
                conditions.append(
                    or_(
                        decision_timestamp > normalized_cursor_timestamp,
                        and_(
                            decision_timestamp == normalized_cursor_timestamp,
                            WorkflowRunAttemptModel.workflow_run_id > cursor_workflow_run_id,
                        ),
                        and_(
                            decision_timestamp == normalized_cursor_timestamp,
                            WorkflowRunAttemptModel.workflow_run_id == cursor_workflow_run_id,
                            WorkflowRunAttemptModel.attempt_number > cursor_attempt_number,
                        ),
                    )
                )
            query = (
                select(WorkflowRunAttemptModel)
                .options(attempt_metadata_options(session.bind.dialect.name))
                .outerjoin(
                    WorkflowRunModel,
                    and_(
                        WorkflowRunModel.workflow_run_id == WorkflowRunAttemptModel.workflow_run_id,
                        WorkflowRunModel.organization_id == WorkflowRunAttemptModel.organization_id,
                    ),
                )
                .where(*conditions)
                .order_by(
                    decision_timestamp.asc(),
                    WorkflowRunAttemptModel.workflow_run_id.asc(),
                    WorkflowRunAttemptModel.attempt_number.asc(),
                )
                .limit(limit)
            )
            attempts = (await session.scalars(query)).all()
            return list(attempts)

    @db_operation("list_stale_dispatch_claims")
    async def list_stale_dispatch_claims(
        self,
        stale_before: datetime,
        *,
        cursor: tuple[str, int] | None = None,
        limit: int = ATTEMPT_RECOVERY_BATCH_SIZE,
    ) -> list[WorkflowRunAttemptModel]:
        normalized_cutoff = to_naive_utc(stale_before)
        assert normalized_cutoff is not None
        if limit <= 0:
            raise ValueError("Recovery page limit must be positive")
        query = (
            select(WorkflowRunAttemptModel)
            .join(WorkflowRunModel, WorkflowRunModel.workflow_run_id == WorkflowRunAttemptModel.workflow_run_id)
            .where(_stale_dispatch_claim_conditions(normalized_cutoff))
            .order_by(WorkflowRunAttemptModel.workflow_run_id, WorkflowRunAttemptModel.attempt_number)
            .limit(limit)
        )
        if cursor is not None:
            workflow_run_id, attempt_number = cursor
            query = query.where(
                or_(
                    WorkflowRunAttemptModel.workflow_run_id > workflow_run_id,
                    and_(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number > attempt_number,
                    ),
                )
            )
        async with self.Session() as session:
            query = query.options(attempt_metadata_options(session.bind.dialect.name))
            return list((await session.scalars(query)).all())

    @db_operation("release_stale_dispatch_claim")
    async def release_stale_dispatch_claim(
        self, workflow_run_id: str, organization_id: str, attempt_number: int, *, stale_before: datetime
    ) -> bool:
        normalized_cutoff = to_naive_utc(stale_before)
        assert normalized_cutoff is not None
        now = naive_utc_now()
        attempt_conditions = (
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.organization_id == organization_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            WorkflowRunAttemptModel.status == "running",
            WorkflowRunAttemptModel.started_at.isnot(None),
            WorkflowRunAttemptModel.retry_decision.is_(None),
            WorkflowRunAttemptModel.modified_at < normalized_cutoff,
        )
        async with self.Session() as session:
            # Lock the run before the attempt, as in claim_prepared_attempt_execution. Both resets
            # commit together; the complete stale predicate is checked by the conditional run update.
            run_release = await session.execute(
                update(WorkflowRunModel)
                .where(
                    WorkflowRunModel.workflow_run_id == workflow_run_id,
                    WorkflowRunModel.organization_id == organization_id,
                    WorkflowRunModel.status == "queued",
                    WorkflowRunModel.started_at.isnot(None),
                    select(WorkflowRunAttemptModel.workflow_run_id).where(*attempt_conditions).exists(),
                )
                .values(started_at=None, modified_at=now)
                .returning(WorkflowRunModel.workflow_run_id)
            )
            if run_release.scalar_one_or_none() is None:
                return False
            attempt_release = await session.execute(
                update(WorkflowRunAttemptModel)
                .where(*attempt_conditions)
                .values(started_at=None, status="queued", modified_at=now)
                .returning(WorkflowRunAttemptModel.attempt_number)
            )
            if attempt_release.scalar_one_or_none() is None:
                await session.rollback()
                return False
            await session.commit()
            return True

    @db_operation("mark_attempt_inputs_unrecoverable")
    async def mark_attempt_inputs_unrecoverable(self, workflow_run_id: str, attempt_number: int) -> None:
        async with self.Session() as session:
            await session.execute(
                update(WorkflowRunAttemptModel)
                .where(
                    WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                    WorkflowRunAttemptModel.attempt_number == attempt_number,
                    WorkflowRunAttemptModel.retry_decision.is_(None),
                )
                .values(decision_reason="unrecoverable_block_outputs", modified_at=naive_utc_now())
            )
            await session.commit()

    @db_operation("refresh_attempt_finished_at")
    async def refresh_attempt_finished_at(
        self, workflow_run_id: str, attempt_number: int, *, finished_at: datetime
    ) -> WorkflowRunAttemptModel | None:
        normalized_finished_at = to_naive_utc(finished_at)
        assert normalized_finished_at is not None
        async with self.Session() as session:
            refreshed = (
                await session.scalars(
                    update(WorkflowRunAttemptModel)
                    .where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                        WorkflowRunAttemptModel.retry_decision.isnot(None),
                        or_(
                            WorkflowRunAttemptModel.finished_at.is_(None),
                            WorkflowRunAttemptModel.finished_at < normalized_finished_at,
                        ),
                    )
                    .values(finished_at=normalized_finished_at, modified_at=naive_utc_now())
                    .returning(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                )
            ).one_or_none()
            if refreshed is not None:
                session.expunge(refreshed)
            await session.commit()
            return refreshed

    @db_operation("fail_prepared_workflow_run")
    async def fail_prepared_workflow_run(
        self,
        workflow_run_id: str,
        organization_id: str,
        attempt_number: int,
        failure_reason: str,
    ) -> bool:
        now = naive_utc_now()
        prepared_attempt = select(WorkflowRunAttemptModel.workflow_run_id).where(
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            WorkflowRunAttemptModel.retry_decision.is_(None),
            WorkflowRunAttemptModel.status == "queued",
            WorkflowRunAttemptModel.started_at.is_(None),
        )
        async with self.Session() as session:
            result = await session.execute(
                update(WorkflowRunModel)
                .where(
                    WorkflowRunModel.workflow_run_id == workflow_run_id,
                    WorkflowRunModel.organization_id == organization_id,
                    WorkflowRunModel.status == "queued",
                    WorkflowRunModel.started_at.is_(None),
                    prepared_attempt.exists(),
                )
                .values(status="failed", failure_reason=failure_reason, finished_at=now, modified_at=now)
                .returning(WorkflowRunModel.workflow_run_id)
            )
            claimed = result.scalar_one_or_none() is not None
            if claimed:
                await session.execute(
                    update(WorkflowRunAttemptModel)
                    .where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                    )
                    .values(
                        status="failed",
                        retry_decision="abandoned",
                        decision_reason="prepared_attempt_never_started",
                        failure_reason=failure_reason,
                        finished_at=now,
                        modified_at=now,
                    )
                )
                # The runs list reads finished_at from the task_runs mirror that retry preparation cleared.
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.run_id == workflow_run_id,
                        TaskRunModel.organization_id == organization_id,
                        TaskRunModel.task_run_type == "workflow_run",
                    )
                    .values(status="failed", finished_at=now, modified_at=now)
                )
            await session.commit()
            return claimed

    @db_operation("claim_prepared_attempt_execution")
    async def claim_prepared_attempt_execution(
        self, workflow_run_id: str, organization_id: str, attempt_number: int
    ) -> datetime | None:
        now = naive_utc_now()
        conditions = (
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.organization_id == organization_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            WorkflowRunAttemptModel.status == "queued",
            WorkflowRunAttemptModel.retry_decision.is_(None),
            WorkflowRunAttemptModel.started_at.is_(None),
        )
        async with self.Session() as session:
            run_claim = await session.execute(
                update(WorkflowRunModel)
                .where(
                    WorkflowRunModel.workflow_run_id == workflow_run_id,
                    WorkflowRunModel.organization_id == organization_id,
                    WorkflowRunModel.status == "queued",
                    WorkflowRunModel.started_at.is_(None),
                    select(WorkflowRunAttemptModel.workflow_run_id).where(*conditions).exists(),
                )
                .values(started_at=now, modified_at=now)
                .returning(WorkflowRunModel.workflow_run_id)
            )
            if run_claim.scalar_one_or_none() is None:
                return None
            attempt_claim = await session.execute(
                update(WorkflowRunAttemptModel)
                .where(*conditions)
                .values(started_at=now, status="running", modified_at=now)
                .returning(WorkflowRunAttemptModel.started_at)
            )
            started_at = attempt_claim.scalar_one_or_none()
            if started_at is None:
                await session.rollback()
                return None
            await session.commit()
            return started_at

    @db_operation("finalize_attempt", expected_errors=(AttemptRecoverySuperseded,))
    async def finalize_attempt(
        self,
        workflow_run_id: str,
        attempt_number: int,
        *,
        status: str,
        failure_reason: str | None,
        failure_category: list[dict[str, Any]] | None,
        error_codes: list[str] | None,
        finished_at: datetime,
        retry_decision: str,
        decision_reason: str | None,
        next_attempt_at: datetime | None,
    ) -> WorkflowRunAttemptModel:
        normalized_finished_at = to_naive_utc(finished_at)
        assert normalized_finished_at is not None
        next_attempt_at = to_naive_utc(next_attempt_at)
        now = naive_utc_now()
        recovering = _undecided_recovery_attempt.get()
        recovery_conditions: list[ColumnElement[bool]] = []
        if (
            recovering is not None
            and recovering.workflow_run_id == workflow_run_id
            and recovering.attempt_number == attempt_number
        ):
            recovery_conditions.append(WorkflowRunAttemptModel.modified_at == recovering.modified_at)
        async with self.Session() as session:
            finalized_attempt = (
                await session.scalars(
                    update(WorkflowRunAttemptModel)
                    .where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                        WorkflowRunAttemptModel.retry_decision.is_(None),
                        *recovery_conditions,
                    )
                    .values(
                        status=status,
                        failure_reason=failure_reason,
                        failure_category=failure_category,
                        error_codes=error_codes,
                        finished_at=normalized_finished_at,
                        retry_decision=retry_decision,
                        decision_reason=decision_reason,
                        next_attempt_at=next_attempt_at,
                        modified_at=now,
                    )
                    .returning(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                )
            ).one_or_none()
            if recovery_conditions and finalized_attempt is None:
                raise AttemptRecoverySuperseded
            revoked_attempt = (
                await session.scalars(
                    update(WorkflowRunAttemptModel)
                    .where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                        WorkflowRunAttemptModel.retry_decision == "revoked",
                    )
                    .values(
                        status=case(
                            (
                                WorkflowRunAttemptModel.status.in_(TERMINAL_STATUSES),
                                WorkflowRunAttemptModel.status,
                            ),
                            else_=bindparam(
                                "terminal_status",
                                status,
                                type_=WorkflowRunAttemptModel.status.type,
                            ),
                        ),
                        failure_reason=func.coalesce(
                            WorkflowRunAttemptModel.failure_reason,
                            bindparam(
                                "revoked_failure_reason",
                                failure_reason,
                                type_=WorkflowRunAttemptModel.failure_reason.type,
                            ),
                        ),
                        failure_category=func.coalesce(
                            WorkflowRunAttemptModel.failure_category,
                            bindparam(
                                "revoked_failure_category",
                                failure_category,
                                type_=WorkflowRunAttemptModel.failure_category.type,
                            ),
                        ),
                        error_codes=func.coalesce(
                            WorkflowRunAttemptModel.error_codes,
                            bindparam(
                                "revoked_error_codes",
                                error_codes,
                                type_=WorkflowRunAttemptModel.error_codes.type,
                            ),
                        ),
                        finished_at=func.coalesce(
                            WorkflowRunAttemptModel.finished_at,
                            bindparam(
                                "revoked_finished_at",
                                normalized_finished_at,
                                type_=WorkflowRunAttemptModel.finished_at.type,
                            ),
                        ),
                        modified_at=now,
                    )
                    .returning(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                )
            ).one_or_none()
            attempt = revoked_attempt or finalized_attempt
            if attempt is None:
                attempt = (
                    await session.scalars(
                        select(WorkflowRunAttemptModel)
                        .options(attempt_metadata_options(session.bind.dialect.name))
                        .where(
                            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                            WorkflowRunAttemptModel.attempt_number == attempt_number,
                        )
                    )
                ).one_or_none()
            if attempt is None:
                raise NotFoundError(f"Workflow run attempt not found: {workflow_run_id}/{attempt_number}")
            session.expunge(attempt)
            await session.commit()
            return attempt

    @db_operation("revoke_or_abandon_attempt")
    async def revoke_or_abandon_attempt(
        self,
        workflow_run_id: str,
        attempt_number: int,
        decision: str,
        reason: str,
        status: str | None = None,
        failure_reason: str | None = None,
        finished_at: datetime | None = None,
        failure_category: list[dict[str, Any]] | None = None,
        error_codes: list[str] | None = None,
        lease_takeover_seconds: int | None = None,
    ) -> WorkflowRunAttemptModel | None:
        eligible_decision = or_(
            WorkflowRunAttemptModel.retry_decision == "retry",
            WorkflowRunAttemptModel.retry_decision.is_(None),
        )
        now = naive_utc_now()
        if decision in {"revoked", "abandoned"}:
            expected_decision = and_(
                eligible_decision,
                WorkflowRunAttemptModel.next_attempt_prepared_at.is_(None),
            )
            if decision == "abandoned":
                if lease_takeover_seconds is None:
                    raise ValueError("lease_takeover_seconds is required when abandoning an attempt")
                lease_available = or_(
                    WorkflowRunAttemptModel.side_effects_released_at.is_(None),
                    WorkflowRunAttemptModel.side_effects_released_at < now - timedelta(seconds=lease_takeover_seconds),
                )
                expected_decision = or_(
                    and_(expected_decision, lease_available),
                    and_(
                        WorkflowRunAttemptModel.retry_decision == "abandoned",
                        WorkflowRunAttemptModel.finished_at.is_(None),
                    ),
                )
        else:
            expected_decision = WorkflowRunAttemptModel.retry_decision == "retry"
        if reason == "process_restart":
            if lease_takeover_seconds is None:
                raise ValueError("lease_takeover_seconds is required for stale retry recovery")
            expected_decision = and_(
                expected_decision,
                WorkflowRunAttemptModel.retry_decision == "retry",
                WorkflowRunAttemptModel.next_attempt_prepared_at.is_(None),
                WorkflowRunAttemptModel.next_attempt_at < now - timedelta(seconds=lease_takeover_seconds),
            )
        values: dict[str, Any] = {
            "retry_decision": decision,
            "decision_reason": reason,
            "modified_at": now,
        }
        if decision in {"revoked", "abandoned"}:
            # An interim retry webhook may have claimed this attempt before cancellation or a
            # stale-retry recovery won the abandonment race. The final abandonment path must be
            # able to claim the remaining terminal effects for the same attempt. Preserve a
            # claim when a concurrent abandonment re-reads an already-abandoned row, otherwise
            # two finalizers could both become side-effect winners.
            values["side_effects_released_at"] = case(
                (eligible_decision, None),
                else_=WorkflowRunAttemptModel.side_effects_released_at,
            )
        if status is not None:
            # ``status`` is the non-null current lifecycle value, so an abandonment that happens
            # before the terminal transition must promote it from running/queued to the terminal
            # status supplied by that transition. Nullable terminal metadata below is fill-once.
            values["status"] = status
        if failure_reason is not None:
            values["failure_reason"] = func.coalesce(
                WorkflowRunAttemptModel.failure_reason,
                bindparam(
                    "abandon_failure_reason",
                    failure_reason,
                    type_=WorkflowRunAttemptModel.failure_reason.type,
                ),
            )
        if failure_category is not None:
            values["failure_category"] = func.coalesce(
                WorkflowRunAttemptModel.failure_category,
                bindparam(
                    "abandon_failure_category",
                    failure_category,
                    type_=WorkflowRunAttemptModel.failure_category.type,
                ),
            )
        if error_codes is not None:
            values["error_codes"] = func.coalesce(
                WorkflowRunAttemptModel.error_codes,
                bindparam(
                    "abandon_error_codes",
                    error_codes,
                    type_=WorkflowRunAttemptModel.error_codes.type,
                ),
            )
        normalized_finished_at = to_naive_utc(finished_at)
        if normalized_finished_at is not None:
            values["finished_at"] = func.coalesce(
                WorkflowRunAttemptModel.finished_at,
                bindparam(
                    "abandon_finished_at",
                    normalized_finished_at,
                    type_=WorkflowRunAttemptModel.finished_at.type,
                ),
            )
        async with self.Session() as session:
            attempt = (
                await session.scalars(
                    update(WorkflowRunAttemptModel)
                    .where(
                        WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                        WorkflowRunAttemptModel.attempt_number == attempt_number,
                        expected_decision,
                    )
                    .values(**values)
                    .returning(WorkflowRunAttemptModel)
                    .options(attempt_metadata_options(session.bind.dialect.name))
                )
            ).one_or_none()
            if attempt is not None:
                session.expunge(attempt)
            await session.commit()
            return attempt

    @db_operation("reserve_attempt_webhook_delivery")
    async def reserve_attempt_webhook_delivery(
        self,
        workflow_run_id: str,
        attempt_number: int,
        *,
        kind: Literal["interim", "final"],
        expected_claim_at: datetime | None,
        max_attempts: int,
    ) -> int | None:
        if kind == "interim":
            completion_column = WorkflowRunAttemptModel.interim_webhook_sent_at
            progress_column = WorkflowRunAttemptModel.interim_side_effects_progress
        elif kind == "final":
            completion_column = WorkflowRunAttemptModel.webhook_sent_at
            progress_column = WorkflowRunAttemptModel.final_side_effects_progress
        else:
            raise ValueError(f"Unsupported workflow attempt webhook kind: {kind}")
        conditions = [
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            WorkflowRunAttemptModel.side_effects_released_at == to_naive_utc(expected_claim_at),
            completion_column.is_(None),
        ]
        async with self.Session() as session:
            attempt = await session.scalar(
                select(WorkflowRunAttemptModel)
                .options(attempt_metadata_options(session.bind.dialect.name))
                .where(*conditions)
                .with_for_update()
            )
            if attempt is None:
                return None
            progress = dict(getattr(attempt, progress_column.key) or {})
            delivery_attempts = progress.get("webhook_delivery_attempts", 0)
            now = naive_utc_now()
            values: dict[str, Any] = {"modified_at": now}
            if delivery_attempts >= max_attempts:
                # A process can stop after reserving its last call, before recording exhaustion.
                progress["webhook_delivery_exhausted_at"] = now.isoformat()
                values[completion_column.key] = now
                reserved_attempt = 0
            else:
                reserved_attempt = delivery_attempts + 1
                progress["webhook_delivery_attempts"] = reserved_attempt
            values[progress_column.key] = merge_attempt_progress(progress_column, progress, session.bind.dialect.name)
            await session.execute(update(WorkflowRunAttemptModel).where(*conditions).values(**values))
            await session.commit()
            return reserved_attempt

    @db_operation("claim_attempt_webhook")
    async def claim_attempt_webhook(
        self,
        workflow_run_id: str,
        attempt_number: int,
        kind: Literal["interim", "final"] = "final",
        *,
        expected_claim_at: datetime | None = None,
        delivery_attempted: bool = False,
        delivery_exhausted: bool = False,
    ) -> bool:
        if kind == "interim":
            claim_column = WorkflowRunAttemptModel.interim_webhook_sent_at
            progress_column = WorkflowRunAttemptModel.interim_side_effects_progress
        elif kind == "final":
            claim_column = WorkflowRunAttemptModel.webhook_sent_at
            progress_column = WorkflowRunAttemptModel.final_side_effects_progress
        else:
            raise ValueError(f"Unsupported workflow attempt webhook kind: {kind}")

        now = naive_utc_now()
        conditions = [
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            claim_column.is_(None),
        ]
        if expected_claim_at is not None:
            normalized_expected_claim_at = to_naive_utc(expected_claim_at)
            assert normalized_expected_claim_at is not None
            conditions.append(WorkflowRunAttemptModel.side_effects_released_at == normalized_expected_claim_at)
        async with self.Session() as session:
            values: dict[str, Any] = {claim_column.key: now, "modified_at": now}
            if delivery_attempted or delivery_exhausted:
                progress = dict(
                    await session.scalar(
                        select(progress_without_outputs(progress_column, session.bind.dialect.name))
                        .where(*conditions)
                        .with_for_update()
                    )
                    or {}
                )
                if delivery_exhausted:
                    progress["webhook_delivery_exhausted_at"] = now.isoformat()
                else:
                    progress["webhook_delivery_attempted"] = True
                values[progress_column.key] = merge_attempt_progress(
                    progress_column, progress, session.bind.dialect.name
                )
            result = await session.execute(update(WorkflowRunAttemptModel).where(*conditions).values(**values))
            await session.commit()
            return bool(result.rowcount)

    @db_operation("claim_attempt_side_effects")
    async def claim_attempt_side_effects(
        self,
        workflow_run_id: str,
        attempt_number: int,
        kind: Literal["interim", "final"] = "final",
        *,
        expected_claim_at: datetime | None = None,
        stale_before: datetime | None = None,
        expected_retry_decision: str | None = None,
    ) -> datetime | None:
        if kind == "interim":
            completion_column = WorkflowRunAttemptModel.interim_webhook_sent_at
        elif kind == "final":
            completion_column = WorkflowRunAttemptModel.webhook_sent_at
        else:
            raise ValueError(f"Unsupported workflow attempt side-effect kind: {kind}")

        claim_condition = WorkflowRunAttemptModel.side_effects_released_at.is_(None)
        if expected_claim_at is not None:
            normalized_expected_claim_at = to_naive_utc(expected_claim_at)
            assert normalized_expected_claim_at is not None
            normalized_stale_before = to_naive_utc(stale_before)
            assert normalized_stale_before is not None
            claim_condition = and_(
                WorkflowRunAttemptModel.side_effects_released_at == normalized_expected_claim_at,
                WorkflowRunAttemptModel.side_effects_released_at < normalized_stale_before,
            )

        now = naive_utc_now()
        conditions = [
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            completion_column.is_(None),
            claim_condition,
        ]
        if expected_retry_decision is not None:
            conditions.append(WorkflowRunAttemptModel.retry_decision == expected_retry_decision)
        async with self.Session() as session:
            result = await session.execute(
                update(WorkflowRunAttemptModel)
                .where(*conditions)
                .values(side_effects_released_at=now, modified_at=now)
                .returning(WorkflowRunAttemptModel.side_effects_released_at)
            )
            claimed_at = result.scalar_one_or_none()
            await session.commit()
            return claimed_at

    @db_operation("save_side_effect_progress")
    async def save_side_effect_progress(
        self,
        workflow_run_id: str,
        attempt_number: int,
        *,
        kind: Literal["interim", "final"],
        expected_claim_at: datetime,
        progress: TerminalSideEffectCheckpoint,
    ) -> bool:
        """Checkpoint only the current owner's release, keeping interim and final progress separate."""
        claim_at = to_naive_utc(expected_claim_at)
        assert claim_at is not None
        if kind == "interim":
            progress_column = WorkflowRunAttemptModel.interim_side_effects_progress
            decision_condition = WorkflowRunAttemptModel.retry_decision == "retry"
        elif kind == "final":
            progress_column = WorkflowRunAttemptModel.final_side_effects_progress
            decision_condition = WorkflowRunAttemptModel.retry_decision.in_(["final", "revoked", "abandoned"])
        else:
            raise ValueError(f"Unsupported workflow attempt side-effect kind: {kind}")
        conditions = [
            WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
            WorkflowRunAttemptModel.attempt_number == attempt_number,
            WorkflowRunAttemptModel.side_effects_released_at == claim_at,
            decision_condition,
        ]
        async with self.Session() as session:
            checkpoint = await session.scalar(
                select(progress_without_outputs(progress_column, session.bind.dialect.name))
                .where(*conditions)
                .with_for_update()
            )
            # Delivery evidence and release checkpoints share JSON storage and must survive either writer.
            checkpoint = {**(checkpoint or {}), **progress}
            result = await session.execute(
                update(WorkflowRunAttemptModel)
                .where(*conditions)
                .values(
                    {
                        progress_column: merge_attempt_progress(progress_column, checkpoint, session.bind.dialect.name),
                        WorkflowRunAttemptModel.modified_at: naive_utc_now(),
                    }
                )
            )
            await session.commit()
            return bool(result.rowcount)

    @db_operation("clear_failed_interim_side_effect_lease")
    async def clear_failed_interim_side_effect_lease(
        self,
        workflow_run_id: str,
        attempt_number: int,
        expected_claim_at: datetime,
    ) -> bool:
        """Release an interim side-effect lease after its owner failed before recording delivery.

        The conditional update keeps a takeover winner from being cleared by the failed owner.
        Final-release leases are intentionally never cleared here: a final effect may have
        reached a once-only hook before failing, so recovery must retain the takeover bound.
        """
        normalized_expected_claim_at = to_naive_utc(expected_claim_at)
        assert normalized_expected_claim_at is not None
        now = naive_utc_now()
        async with self.Session() as session:
            result = await session.execute(
                update(WorkflowRunAttemptModel)
                .where(
                    WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                    WorkflowRunAttemptModel.attempt_number == attempt_number,
                    WorkflowRunAttemptModel.retry_decision == "retry",
                    WorkflowRunAttemptModel.side_effects_released_at == normalized_expected_claim_at,
                    WorkflowRunAttemptModel.interim_webhook_sent_at.is_(None),
                )
                .values(side_effects_released_at=None, modified_at=now)
            )
            await session.commit()
            return bool(result.rowcount)

    @db_operation("delete_attempts_for_run")
    async def delete_attempts_for_run(self, workflow_run_id: str) -> None:
        async with self.Session() as session:
            await session.execute(
                delete(WorkflowRunAttemptModel).where(
                    WorkflowRunAttemptModel.workflow_run_id == workflow_run_id,
                )
            )
            await session.commit()
