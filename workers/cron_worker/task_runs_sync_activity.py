"""Temporal activity that synchronises the task_runs table from the source-of-truth tables.

workflow_runs, tasks, and observer_cruises each get their own session so that a
failure in one sync step does not block the others.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from cloud.db.cloud_agent_db import cloud_db
from sqlalchemy import TextClause, text
from temporalio import activity

from skyvern.forge.sdk.schemas.runs import TERMINAL_STATUSES

LOG = structlog.get_logger()

# How far back to look for rows that may need syncing.
_SYNC_WINDOW = timedelta(hours=6)

_TERMINAL_LIST = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)


def _build_sync_workflow_runs_stmt(cutoff: datetime) -> TextClause:
    """Build an upsert statement that syncs workflow_runs -> task_runs."""
    return text(
        """
        INSERT INTO task_runs (
            run_id, task_run_type, organization_id,
            status, started_at, finished_at,
            workflow_permanent_id, script_run,
            parent_workflow_run_id, debug_session_id,
            created_at, modified_at
        )
        SELECT
            workflow_run_id, 'workflow_run', organization_id,
            status, started_at, finished_at,
            workflow_permanent_id, script_run,
            parent_workflow_run_id, debug_session_id,
            created_at, modified_at
        FROM workflow_runs
        WHERE created_at >= :cutoff
        ON CONFLICT (run_id, task_run_type) DO UPDATE SET
            status     = EXCLUDED.status,
            started_at = EXCLUDED.started_at,
            finished_at = EXCLUDED.finished_at,
            modified_at = EXCLUDED.modified_at
        WHERE task_runs.status NOT IN ("""
        + _TERMINAL_LIST
        + """)
        """
    ).bindparams(cutoff=cutoff)


def _build_sync_tasks_stmt(cutoff: datetime) -> TextClause:
    """Build an upsert statement that syncs tasks -> task_runs."""
    return text(
        """
        INSERT INTO task_runs (
            run_id, task_run_type, organization_id,
            status, started_at, finished_at,
            title, url, searchable_text,
            created_at, modified_at
        )
        SELECT
            task_id, 'task_v1', organization_id,
            status, started_at, finished_at,
            title, url, concat_ws(' ', title, url),
            created_at, modified_at
        FROM tasks
        WHERE created_at >= :cutoff
        ON CONFLICT (run_id, task_run_type) DO UPDATE SET
            status     = EXCLUDED.status,
            started_at = EXCLUDED.started_at,
            finished_at = EXCLUDED.finished_at,
            title      = EXCLUDED.title,
            url        = EXCLUDED.url,
            searchable_text = EXCLUDED.searchable_text,
            modified_at = EXCLUDED.modified_at
        WHERE task_runs.status NOT IN ("""
        + _TERMINAL_LIST
        + """)
        """
    ).bindparams(cutoff=cutoff)


def _build_sync_task_v2_stmt(cutoff: datetime) -> TextClause:
    """Build an upsert statement that syncs observer_cruises -> task_runs."""
    return text(
        """
        INSERT INTO task_runs (
            run_id, task_run_type, organization_id,
            status, started_at, finished_at,
            workflow_permanent_id, searchable_text, url,
            created_at, modified_at
        )
        SELECT
            observer_cruise_id, 'task_v2', organization_id,
            status, started_at, finished_at,
            workflow_permanent_id, prompt, url,
            created_at, modified_at
        FROM observer_cruises
        WHERE created_at >= :cutoff
        ON CONFLICT (run_id, task_run_type) DO UPDATE SET
            status     = EXCLUDED.status,
            started_at = EXCLUDED.started_at,
            finished_at = EXCLUDED.finished_at,
            searchable_text = EXCLUDED.searchable_text,
            modified_at = EXCLUDED.modified_at
        WHERE task_runs.status NOT IN ("""
        + _TERMINAL_LIST
        + """)
        """
    ).bindparams(cutoff=cutoff)


@activity.defn
async def task_runs_sync_activity() -> dict:
    """Sync task_runs from workflow_runs, tasks, and observer_cruises."""
    cutoff = datetime.now(timezone.utc) - _SYNC_WINDOW
    errors: list[str] = []

    workflow_runs_synced = 0
    tasks_synced = 0
    task_v2_synced = 0

    # -- Workflow runs --
    try:
        async with cloud_db.Session() as session:
            result = await session.execute(_build_sync_workflow_runs_stmt(cutoff))
            workflow_runs_synced = result.rowcount
            await session.commit()
    except Exception:
        LOG.exception("Failed to sync workflow_runs -> task_runs")
        errors.append("Workflow runs sync failed")

    # -- Tasks (v1) --
    try:
        async with cloud_db.Session() as session:
            result = await session.execute(_build_sync_tasks_stmt(cutoff))
            tasks_synced = result.rowcount
            await session.commit()
    except Exception:
        LOG.exception("Failed to sync tasks -> task_runs")
        errors.append("Tasks sync failed")

    # -- Observer cruises (task v2) --
    try:
        async with cloud_db.Session() as session:
            result = await session.execute(_build_sync_task_v2_stmt(cutoff))
            task_v2_synced = result.rowcount
            await session.commit()
    except Exception:
        LOG.exception("Failed to sync observer_cruises -> task_runs")
        errors.append("Task v2 sync failed")

    return {
        "workflow_runs_synced": workflow_runs_synced,
        "tasks_synced": tasks_synced,
        "task_v2_synced": task_v2_synced,
        "errors": errors,
    }
