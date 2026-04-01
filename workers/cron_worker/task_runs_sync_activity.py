"""Temporal activity that syncs workflow_runs, tasks, and observer_cruises into the task_runs table.

Each source is synced independently so a failure in one does not block the others.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
import temporalio
from cloud.db.cloud_agent_db import cloud_db
from sqlalchemy import column, insert, literal_column, select

from skyvern.forge.sdk.db.models import (
    TaskModel,
    TaskRunModel,
    TaskV2Model,
    WorkflowRunModel,
)
from skyvern.forge.sdk.schemas.runs import TERMINAL_STATUSES

LOG = structlog.get_logger()

# How far back to look for new rows that need syncing.
_DEFAULT_LOOKBACK = timedelta(hours=24)


def _build_sync_workflow_runs_stmt(cutoff: datetime) -> Any:
    """Build an INSERT ... SELECT that copies qualifying workflow_runs into task_runs."""
    created_at_col = column("created_at")
    source = (
        select(
            WorkflowRunModel.workflow_run_id.label("run_id"),
            literal_column("'workflow_run'").label("task_run_type"),
            WorkflowRunModel.status,
            WorkflowRunModel.started_at,
            WorkflowRunModel.finished_at,
            WorkflowRunModel.script_run,
            WorkflowRunModel.workflow_permanent_id,
            WorkflowRunModel.parent_workflow_run_id,
            WorkflowRunModel.debug_session_id,
            created_at_col,
        )
        .select_from(WorkflowRunModel.__table__)
        .where(created_at_col >= cutoff)
        .where(WorkflowRunModel.status.in_(TERMINAL_STATUSES))
    )

    stmt = (
        insert(TaskRunModel)
        .from_select(
            [
                "run_id",
                "task_run_type",
                "status",
                "started_at",
                "finished_at",
                "script_run",
                "workflow_permanent_id",
                "parent_workflow_run_id",
                "debug_session_id",
                "created_at",
            ],
            source,
        )
        .prefix_with("OR IGNORE")
    )
    return stmt


def _build_sync_tasks_stmt(cutoff: datetime) -> Any:
    """Build an INSERT ... SELECT that copies qualifying tasks into task_runs."""
    created_at_col = column("created_at")
    source = (
        select(
            TaskModel.task_id.label("run_id"),
            literal_column("'task_v1'").label("task_run_type"),
            TaskModel.status,
            TaskModel.started_at,
            TaskModel.finished_at,
            TaskModel.title,
            TaskModel.url,
            created_at_col,
        )
        .select_from(TaskModel.__table__)
        .where(created_at_col >= cutoff)
        .where(TaskModel.status.in_(TERMINAL_STATUSES))
    )

    stmt = (
        insert(TaskRunModel)
        .from_select(
            [
                "run_id",
                "task_run_type",
                "status",
                "started_at",
                "finished_at",
                "title",
                "url",
                "created_at",
            ],
            source,
        )
        .prefix_with("OR IGNORE")
    )
    return stmt


def _build_sync_task_v2_stmt(cutoff: datetime) -> Any:
    """Build an INSERT ... SELECT that copies qualifying observer_cruises into task_runs."""
    created_at_col = column("created_at")
    source = (
        select(
            TaskV2Model.observer_cruise_id.label("run_id"),
            literal_column("'task_v2'").label("task_run_type"),
            TaskV2Model.status,
            TaskV2Model.started_at,
            TaskV2Model.finished_at,
            TaskV2Model.workflow_run_id.label("parent_workflow_run_id"),
            created_at_col,
        )
        .select_from(TaskV2Model.__table__)
        .where(created_at_col >= cutoff)
        .where(TaskV2Model.status.in_(TERMINAL_STATUSES))
    )

    stmt = (
        insert(TaskRunModel)
        .from_select(
            [
                "run_id",
                "task_run_type",
                "status",
                "started_at",
                "finished_at",
                "parent_workflow_run_id",
                "created_at",
            ],
            source,
        )
        .prefix_with("OR IGNORE")
    )
    return stmt


@temporalio.activity.defn  # type: ignore[attr-defined]
async def task_runs_sync_activity() -> dict[str, Any]:
    """Sync terminal runs from source tables into the unified task_runs table."""
    cutoff = datetime.now(timezone.utc) - _DEFAULT_LOOKBACK
    errors: list[str] = []

    sync_steps: list[tuple[str, str, Any]] = [
        ("workflow_runs", "workflow_runs_synced", _build_sync_workflow_runs_stmt(cutoff)),
        ("tasks", "tasks_synced", _build_sync_tasks_stmt(cutoff)),
        ("task_v2", "task_v2_synced", _build_sync_task_v2_stmt(cutoff)),
    ]

    results: dict[str, Any] = {}

    for label, result_key, stmt in sync_steps:
        try:
            async with cloud_db.Session() as session:
                result = await session.execute(stmt)
                await session.commit()
                results[result_key] = result.rowcount
                LOG.info("task_runs_sync: %s synced %d rows", label, result.rowcount)
        except Exception as exc:
            LOG.exception("task_runs_sync: %s failed", label)
            results[result_key] = 0
            errors.append(f"{label}: {exc}")

    results["errors"] = errors
    return results
