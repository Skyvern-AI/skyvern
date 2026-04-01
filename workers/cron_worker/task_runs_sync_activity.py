"""Temporal activity: bulk-sync task_runs from source tables.

The write-through in sync_task_run_status covers the happy path, but rows
can be missed (race at creation, transient errors).  This cron activity
catches up by copying status/timestamps from the authoritative source tables
(workflow_runs, tasks, observer_cruises) into task_runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from cloud.db.cloud_agent_db import cloud_db
from sqlalchemy import Update, select, update
from temporalio import activity

from skyvern.forge.sdk.db.models import (
    TaskModel,
    TaskRunModel,
    TaskV2Model,
    WorkflowRunModel,
)

LOG = structlog.get_logger()

# Only look at rows created in the last N days to keep the query cheap.
_LOOKBACK_DAYS = 7


def _build_sync_workflow_runs_stmt(cutoff: datetime) -> Update:
    """Build an UPDATE that syncs workflow_run status into matching task_runs rows."""
    wr = WorkflowRunModel.__table__
    tr = TaskRunModel.__table__

    sub = (
        select(
            wr.c.workflow_run_id,
            wr.c.status,
            wr.c.started_at,
            wr.c.finished_at,
        )
        .correlate(None)
        .subquery("src")
    )

    stmt = (
        update(tr)
        .where(tr.c.run_id == sub.c.workflow_run_id)
        .where(tr.c.created_at >= cutoff)
        .values(
            status=sub.c.status,
            started_at=sub.c.started_at,
            finished_at=sub.c.finished_at,
        )
    )
    return stmt


def _build_sync_tasks_stmt(cutoff: datetime) -> Update:
    """Build an UPDATE that syncs task status into matching task_runs rows."""
    t = TaskModel.__table__
    tr = TaskRunModel.__table__

    sub = (
        select(
            t.c.task_id,
            t.c.status,
            t.c.started_at,
            t.c.finished_at,
        )
        .correlate(None)
        .subquery("src")
    )

    stmt = (
        update(tr)
        .where(tr.c.run_id == sub.c.task_id)
        .where(tr.c.created_at >= cutoff)
        .values(
            status=sub.c.status,
            started_at=sub.c.started_at,
            finished_at=sub.c.finished_at,
        )
    )
    return stmt


def _build_sync_task_v2_stmt(cutoff: datetime) -> Update:
    """Build an UPDATE that syncs observer_cruise status into matching task_runs rows."""
    oc = TaskV2Model.__table__
    tr = TaskRunModel.__table__

    sub = (
        select(
            oc.c.observer_cruise_id,
            oc.c.status,
            oc.c.started_at,
            oc.c.finished_at,
        )
        .correlate(None)
        .subquery("src")
    )

    stmt = (
        update(tr)
        .where(tr.c.run_id == sub.c.observer_cruise_id)
        .where(tr.c.created_at >= cutoff)
        .values(
            status=sub.c.status,
            started_at=sub.c.started_at,
            finished_at=sub.c.finished_at,
        )
    )
    return stmt


@activity.defn
async def task_runs_sync_activity() -> dict:
    """Bulk-sync status from source tables into task_runs.

    Each source table is synced independently so a failure in one does not
    block the others.  Returns a summary dict with row counts and errors.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

    results: dict = {
        "workflow_runs_synced": 0,
        "tasks_synced": 0,
        "task_v2_synced": 0,
        "errors": [],
    }

    sync_steps = [
        ("workflow_runs", _build_sync_workflow_runs_stmt, "workflow_runs_synced"),
        ("tasks", _build_sync_tasks_stmt, "tasks_synced"),
        ("task_v2", _build_sync_task_v2_stmt, "task_v2_synced"),
    ]

    for label, builder, result_key in sync_steps:
        try:
            async with cloud_db.Session() as session:
                stmt = builder(cutoff)
                result = await session.execute(stmt)
                results[result_key] = result.rowcount
                await session.commit()
        except Exception as exc:
            LOG.exception("task_runs_sync_activity failed for %s", label)
            results["errors"].append(f"{label}: {exc}")

    return results
