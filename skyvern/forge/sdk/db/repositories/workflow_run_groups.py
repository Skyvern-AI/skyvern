from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal

from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from skyvern.config import settings
from skyvern.constants import SCRUBBED_VALUE
from skyvern.exceptions import GroupAccountsRanSinceReview, WorkflowChangedSinceReview
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.id import generate_workflow_run_id
from skyvern.forge.sdk.db.models import (
    StepModel,
    TaskModel,
    WorkflowModel,
    WorkflowRunGroupItemModel,
    WorkflowRunGroupModel,
)
from skyvern.forge.sdk.workflow.models.workflow import COPILOT_TEST_WORKFLOW_CREATOR
from skyvern.schemas.workflow_run_groups import (
    IN_FLIGHT_ITEM_STATES,
    SCRUBBED_SUBMISSION_KEY_PREFIX,
    WorkflowRunGroup,
    WorkflowRunGroupItem,
    WorkflowRunGroupItemState,
    WorkflowRunGroupStatus,
)

FlipResult = Literal["dispatched", "canceled", "lost"]
_SCRUBBED_KEY_DOMAIN = b"skyvern.workflow_run_group_scrubbed_submission_key.v1\0"


def scrubbed_submission_key(submission_key: str) -> str:
    # Keyed so a scrubbed row cannot be matched to a guessed account name, yet a replay of the raw key still finds it.
    digest = hmac.new(settings.SECRET_KEY.encode(), _SCRUBBED_KEY_DOMAIN + submission_key.encode(), sha256).hexdigest()
    return f"{SCRUBBED_SUBMISSION_KEY_PREFIX}{digest}"


async def _latest_group_ids_by_item_key(
    session: AsyncSession,
    organization_id: str,
    workflow_permanent_id: str,
    submission_key_prefix: str,
    item_keys: Sequence[str],
) -> dict[str, str]:
    ranked = (
        select(
            WorkflowRunGroupItemModel.item_key,
            WorkflowRunGroupItemModel.workflow_run_group_id,
            func.row_number()
            .over(
                partition_by=WorkflowRunGroupItemModel.item_key,
                order_by=WorkflowRunGroupItemModel.created_at.desc(),
            )
            .label("recency"),
        )
        .join(
            WorkflowRunGroupModel,
            WorkflowRunGroupModel.workflow_run_group_id == WorkflowRunGroupItemModel.workflow_run_group_id,
        )
        .where(
            WorkflowRunGroupModel.organization_id == organization_id,
            WorkflowRunGroupModel.workflow_permanent_id == workflow_permanent_id,
            WorkflowRunGroupModel.submission_key.startswith(submission_key_prefix, autoescape=True),
            WorkflowRunGroupItemModel.item_key.in_(item_keys),
        )
        .subquery()
    )
    rows = await session.execute(select(ranked.c.item_key, ranked.c.workflow_run_group_id).where(ranked.c.recency == 1))
    return {item_key: group_id for item_key, group_id in rows}


async def _lock_repeat_check(session: AsyncSession, organization_id: str, workflow_permanent_id: str) -> None:
    # The repeat check spans every version, so approvals serialize per workflow. An advisory lock rather than
    # every version's row, which bulk writers lock in their own order and could deadlock against.
    bind = session.get_bind()
    if bind is not None and bind.dialect.name not in {"postgresql", "postgres"}:
        return
    lock_key = f"workflow_run_group_repeat:{organization_id}:{workflow_permanent_id}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))


class WorkflowRunGroupsRepository(BaseRepository):
    # Item parameters are cleared on every transition out of pending/dispatching; the child run holds its own copy.
    # Every state transition first touches the group row, so transitions of one group serialize on its row lock.
    async def _lock_group(self, session: AsyncSession, workflow_run_group_id: str) -> WorkflowRunGroupStatus | None:
        status = await session.scalar(
            update(WorkflowRunGroupModel)
            .where(WorkflowRunGroupModel.workflow_run_group_id == workflow_run_group_id)
            .values(modified_at=naive_utc_now())
            .returning(WorkflowRunGroupModel.status)
        )
        return WorkflowRunGroupStatus(status) if status is not None else None

    @db_operation(
        "create_workflow_run_group",
        expected_errors=(IntegrityError, WorkflowChangedSinceReview, GroupAccountsRanSinceReview),
    )
    async def create_group(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        requested_version: int | None,
        workflow_id: str,
        submission_key: str,
        input_fingerprint: str,
        items: Sequence[tuple[str, dict[str, Any]]],
        expected_workflow_modified_at: datetime | None = None,
        expected_latest_groups: tuple[str, Mapping[str, str]] | None = None,
    ) -> WorkflowRunGroup:
        async with self.Session() as session:
            if expected_latest_groups is not None:
                await _lock_repeat_check(session, organization_id, workflow_permanent_id)
            # Locks the row in-place edits lock, so an edit either lands before this check or sees this group.
            modified_at = await session.scalar(
                select(WorkflowModel.modified_at).where(WorkflowModel.workflow_id == workflow_id).with_for_update()
            )
            if expected_workflow_modified_at is not None and modified_at != expected_workflow_modified_at:
                raise WorkflowChangedSinceReview(workflow_id)
            if expected_latest_groups is not None:
                # A new version is a new row, so the reviewed row's lock cannot see it; recheck it is still latest.
                latest_version_id = await session.scalar(
                    select(WorkflowModel.workflow_id)
                    .where(WorkflowModel.workflow_permanent_id == workflow_permanent_id)
                    .where(WorkflowModel.organization_id == organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                    .where(
                        or_(
                            WorkflowModel.created_by.is_(None),
                            WorkflowModel.created_by != COPILOT_TEST_WORKFLOW_CREATOR,
                        )
                    )
                    .order_by(WorkflowModel.version.desc())
                    .limit(1)
                )
                if latest_version_id != workflow_id:
                    raise WorkflowChangedSinceReview(workflow_id)
                key_prefix, expected = expected_latest_groups
                latest = await _latest_group_ids_by_item_key(
                    session, organization_id, workflow_permanent_id, key_prefix, [key for key, _ in items]
                )
                if changed := sorted(key for key, group_id in latest.items() if expected.get(key) != group_id):
                    raise GroupAccountsRanSinceReview(changed)
            group = WorkflowRunGroupModel(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                requested_version=requested_version,
                workflow_id=workflow_id,
                submission_key=submission_key,
                input_fingerprint=input_fingerprint,
                status=WorkflowRunGroupStatus.active.value,
            )
            session.add(group)
            await session.flush()
            for position, (item_key, parameters) in enumerate(items):
                session.add(
                    WorkflowRunGroupItemModel(
                        workflow_run_group_id=group.workflow_run_group_id,
                        position=position,
                        item_key=item_key,
                        parameters=parameters,
                        workflow_run_id=generate_workflow_run_id(),
                        state=WorkflowRunGroupItemState.pending.value,
                        dispatch_attempts=0,
                    )
                )
            await session.flush()
            created = WorkflowRunGroup.model_validate(group)
            await session.commit()
            return created

    @db_operation("get_workflow_run_group")
    async def get_group(
        self, workflow_run_group_id: str, organization_id: str | None = None
    ) -> WorkflowRunGroup | None:
        query = select(WorkflowRunGroupModel).where(
            WorkflowRunGroupModel.workflow_run_group_id == workflow_run_group_id
        )
        if organization_id is not None:
            query = query.where(WorkflowRunGroupModel.organization_id == organization_id)
        async with self.Session() as session:
            group = (await session.scalars(query)).first()
            return WorkflowRunGroup.model_validate(group) if group else None

    @db_operation("get_workflow_run_group_by_submission_key")
    async def get_group_by_submission_key(self, organization_id: str, submission_key: str) -> WorkflowRunGroup | None:
        async with self.Session() as session:
            group = (
                await session.scalars(
                    select(WorkflowRunGroupModel)
                    .where(WorkflowRunGroupModel.organization_id == organization_id)
                    .where(WorkflowRunGroupModel.submission_key == submission_key)
                )
            ).first()
            return WorkflowRunGroup.model_validate(group) if group else None

    @db_operation("get_latest_group_ids_by_item_key")
    async def get_latest_group_ids_by_item_key(
        self, organization_id: str, workflow_permanent_id: str, submission_key_prefix: str, item_keys: Sequence[str]
    ) -> dict[str, str]:
        async with self.Session() as session:
            return await _latest_group_ids_by_item_key(
                session, organization_id, workflow_permanent_id, submission_key_prefix, item_keys
            )

    @db_operation("get_workflow_run_groups_by_submission_keys")
    async def get_groups_by_submission_keys(
        self, organization_id: str, submission_keys: Sequence[str]
    ) -> dict[str, WorkflowRunGroup]:
        async with self.Session() as session:
            groups = await session.scalars(
                select(WorkflowRunGroupModel)
                .where(WorkflowRunGroupModel.organization_id == organization_id)
                .where(WorkflowRunGroupModel.submission_key.in_(submission_keys))
            )
            return {group.submission_key: WorkflowRunGroup.model_validate(group) for group in groups}

    @db_operation("scrub_workflow_run_group_keys")
    async def scrub_keys(self, organization_id: str, workflow_run_group_ids: Sequence[str]) -> int:
        """Replace the caller's submission and item keys; returns the groups scrubbed.

        Item parameters are left alone: a pending or dispatching item still needs them to run, and every other state
        has already cleared them.
        """
        async with self.Session() as session:
            groups = (
                await session.scalars(
                    select(WorkflowRunGroupModel)
                    .where(WorkflowRunGroupModel.organization_id == organization_id)
                    .where(WorkflowRunGroupModel.workflow_run_group_id.in_(workflow_run_group_ids))
                    .where(~WorkflowRunGroupModel.submission_key.startswith(SCRUBBED_SUBMISSION_KEY_PREFIX))
                )
            ).all()
            if not groups:
                return 0
            for group in groups:
                group.submission_key = scrubbed_submission_key(group.submission_key)
            await session.execute(
                update(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id.in_([g.workflow_run_group_id for g in groups]))
                .values(item_key=f"{SCRUBBED_VALUE}-" + cast(WorkflowRunGroupItemModel.position, String))
            )
            await session.commit()
            return len(groups)

    @db_operation("get_workflow_run_group_items")
    async def get_items(self, workflow_run_group_id: str) -> list[WorkflowRunGroupItem]:
        async with self.Session() as session:
            items = (
                await session.scalars(
                    select(WorkflowRunGroupItemModel)
                    .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                    .order_by(WorkflowRunGroupItemModel.position)
                )
            ).all()
            return [WorkflowRunGroupItem.model_validate(item) for item in items]

    @db_operation("get_workflow_run_group_item_by_workflow_run_id")
    async def get_item_by_workflow_run_id(self, workflow_run_id: str) -> WorkflowRunGroupItem | None:
        async with self.Session() as session:
            item = (
                await session.scalars(
                    select(WorkflowRunGroupItemModel).where(
                        WorkflowRunGroupItemModel.workflow_run_id == workflow_run_id
                    )
                )
            ).first()
            return WorkflowRunGroupItem.model_validate(item) if item else None

    @db_operation("list_unfinished_workflow_run_groups")
    async def list_unfinished_groups(self, limit: int) -> list[tuple[str, str]]:
        async with self.Session() as session:
            rows = (
                await session.execute(
                    select(WorkflowRunGroupModel.workflow_run_group_id, WorkflowRunGroupModel.organization_id)
                    .where(
                        WorkflowRunGroupModel.status.in_(
                            [WorkflowRunGroupStatus.active.value, WorkflowRunGroupStatus.cancel_requested.value]
                        )
                    )
                    .order_by(WorkflowRunGroupModel.modified_at)
                    .limit(limit)
                )
            ).all()
            return [(group_id, organization_id) for group_id, organization_id in rows]

    @db_operation("claim_next_workflow_run_group_item")
    async def claim_next_item(self, workflow_run_group_id: str, dispatch_token: str) -> WorkflowRunGroupItem | None:
        async with self.Session() as session:
            if await self._lock_group(session, workflow_run_group_id) != WorkflowRunGroupStatus.active:
                await session.commit()
                return None
            in_flight = await session.scalar(
                select(func.count())
                .select_from(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                .where(WorkflowRunGroupItemModel.state.in_([state.value for state in IN_FLIGHT_ITEM_STATES]))
            )
            next_position = None
            if not in_flight:
                next_position = await session.scalar(
                    select(func.min(WorkflowRunGroupItemModel.position))
                    .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                    .where(WorkflowRunGroupItemModel.state == WorkflowRunGroupItemState.pending.value)
                )
            if next_position is None:
                await session.commit()
                return None
            claimed = await session.scalar(
                update(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                .where(WorkflowRunGroupItemModel.position == next_position)
                .where(WorkflowRunGroupItemModel.state == WorkflowRunGroupItemState.pending.value)
                .values(
                    state=WorkflowRunGroupItemState.dispatching.value,
                    dispatch_token=dispatch_token,
                    claimed_at=naive_utc_now(),
                    dispatch_attempts=WorkflowRunGroupItemModel.dispatch_attempts + 1,
                )
                .returning(WorkflowRunGroupItemModel)
            )
            result = WorkflowRunGroupItem.model_validate(claimed) if claimed else None
            await session.commit()
            return result

    @db_operation("reclaim_stale_workflow_run_group_item")
    async def reclaim_stale_item(
        self,
        workflow_run_group_id: str,
        position: int,
        *,
        dispatch_token: str,
        stale_before: datetime,
    ) -> WorkflowRunGroupItem | None:
        async with self.Session() as session:
            await self._lock_group(session, workflow_run_group_id)
            reclaimed = await session.scalar(
                update(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                .where(WorkflowRunGroupItemModel.position == position)
                .where(WorkflowRunGroupItemModel.state == WorkflowRunGroupItemState.dispatching.value)
                .where(WorkflowRunGroupItemModel.claimed_at < stale_before)
                .values(
                    dispatch_token=dispatch_token,
                    claimed_at=naive_utc_now(),
                    dispatch_attempts=WorkflowRunGroupItemModel.dispatch_attempts + 1,
                )
                .returning(WorkflowRunGroupItemModel)
            )
            result = WorkflowRunGroupItem.model_validate(reclaimed) if reclaimed else None
            await session.commit()
            return result

    @db_operation("flip_workflow_run_group_item_to_dispatched")
    async def flip_to_dispatched(self, workflow_run_group_id: str, position: int, dispatch_token: str) -> FlipResult:
        async with self.Session() as session:
            group_status = await self._lock_group(session, workflow_run_group_id)
            item = (
                await session.scalars(
                    select(WorkflowRunGroupItemModel)
                    .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                    .where(WorkflowRunGroupItemModel.position == position)
                )
            ).first()
            if (
                item is None
                or item.state != WorkflowRunGroupItemState.dispatching.value
                or item.dispatch_token != dispatch_token
            ):
                await session.commit()
                return "lost"
            result: FlipResult = "dispatched"
            item.parameters = {}
            if group_status != WorkflowRunGroupStatus.active:
                result = "canceled"
                item.state = WorkflowRunGroupItemState.canceled.value
            else:
                item.state = WorkflowRunGroupItemState.dispatched.value
                item.claimed_at = naive_utc_now()
            await session.commit()
            return result

    @db_operation("finish_workflow_run_group_item")
    async def finish_item(
        self,
        workflow_run_group_id: str,
        position: int,
        *,
        state: WorkflowRunGroupItemState,
        from_states: Sequence[WorkflowRunGroupItemState],
        dispatch_token: str | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        async with self.Session() as session:
            await self._lock_group(session, workflow_run_group_id)
            statement = (
                update(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                .where(WorkflowRunGroupItemModel.position == position)
                .where(WorkflowRunGroupItemModel.state.in_([from_state.value for from_state in from_states]))
            )
            if dispatch_token is not None:
                statement = statement.where(WorkflowRunGroupItemModel.dispatch_token == dispatch_token)
            values: dict[str, str | dict[str, Any]] = {"state": state.value, "parameters": {}}
            if failure_reason is not None:
                values["failure_reason"] = failure_reason
            updated = await session.scalar(statement.values(**values).returning(WorkflowRunGroupItemModel.position))
            await session.commit()
            return updated is not None

    @db_operation("request_workflow_run_group_cancel")
    async def request_cancel(self, workflow_run_group_id: str, organization_id: str) -> WorkflowRunGroup | None:
        async with self.Session() as session:
            group = (
                await session.scalars(
                    update(WorkflowRunGroupModel)
                    .where(WorkflowRunGroupModel.workflow_run_group_id == workflow_run_group_id)
                    .where(WorkflowRunGroupModel.organization_id == organization_id)
                    .values(modified_at=naive_utc_now())
                    .returning(WorkflowRunGroupModel)
                )
            ).first()
            if group is None:
                await session.commit()
                return None
            if group.status == WorkflowRunGroupStatus.active.value:
                group.status = WorkflowRunGroupStatus.cancel_requested.value
                await session.execute(
                    update(WorkflowRunGroupItemModel)
                    .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                    .where(WorkflowRunGroupItemModel.state == WorkflowRunGroupItemState.pending.value)
                    .values(state=WorkflowRunGroupItemState.canceled.value, parameters={})
                )
            await session.flush()
            result = WorkflowRunGroup.model_validate(group)
            await session.commit()
            return result

    @db_operation("finish_workflow_run_group_if_complete")
    async def finish_group_if_complete(self, workflow_run_group_id: str) -> bool:
        async with self.Session() as session:
            group_status = await self._lock_group(session, workflow_run_group_id)
            if group_status in (None, WorkflowRunGroupStatus.finished):
                await session.commit()
                return False
            unfinished = await session.scalar(
                select(func.count())
                .select_from(WorkflowRunGroupItemModel)
                .where(WorkflowRunGroupItemModel.workflow_run_group_id == workflow_run_group_id)
                .where(
                    WorkflowRunGroupItemModel.state.not_in(
                        [state.value for state in WorkflowRunGroupItemState if state.is_final()]
                    )
                )
            )
            if unfinished:
                await session.commit()
                return False
            now = naive_utc_now()
            await session.execute(
                update(WorkflowRunGroupModel)
                .where(WorkflowRunGroupModel.workflow_run_group_id == workflow_run_group_id)
                .values(status=WorkflowRunGroupStatus.finished.value, finished_at=now, modified_at=now)
            )
            await session.commit()
            return True

    @db_operation("count_workflow_run_steps")
    async def count_steps(self, workflow_run_id: str) -> int:
        async with self.Session() as session:
            count = await session.scalar(
                select(func.count(StepModel.step_id))
                .join(TaskModel, TaskModel.task_id == StepModel.task_id)
                .where(TaskModel.workflow_run_id == workflow_run_id)
            )
            return int(count or 0)
