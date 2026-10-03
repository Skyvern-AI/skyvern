from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.client.types.workflow_schedule import WorkflowSchedule
from skyvern.client.types.workflow_schedule_response import WorkflowScheduleResponse

STORED_ANCHOR = dt.datetime(2026, 10, 30, 15, 0, 0, tzinfo=dt.timezone.utc)
NEW_ANCHOR = dt.datetime(2026, 11, 2, 8, 30, 0, tzinfo=dt.timezone.utc)


def _make_fern_schedule(
    *,
    workflow_schedule_id: str = "wfs_test_1",
    organization_id: str = "o_test",
    workflow_permanent_id: str = "wpid_test_1",
    cron_expression: str | None = "0 9 * * *",
    interval_seconds: int | None = None,
    first_fire_at: dt.datetime | None = None,
    timezone: str = "UTC",
    enabled: bool = True,
    parameters: dict[str, Any] | None = None,
    temporal_schedule_id: str | None = "ts_abc",
    name: str | None = "probe",
    description: str | None = "test",
    created_at: dt.datetime = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
    modified_at: dt.datetime = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc),
) -> WorkflowSchedule:
    return WorkflowSchedule(
        workflow_schedule_id=workflow_schedule_id,
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cron_expression=cron_expression,
        interval_seconds=interval_seconds,
        first_fire_at=first_fire_at,
        timezone=timezone,
        enabled=enabled,
        parameters=parameters,
        temporal_schedule_id=temporal_schedule_id,
        name=name,
        description=description,
        created_at=created_at,
        modified_at=modified_at,
    )


def _make_fern_schedule_response(
    schedule: WorkflowSchedule | None = None,
    next_runs: list[dt.datetime] | None = None,
) -> WorkflowScheduleResponse:
    return WorkflowScheduleResponse(
        schedule=schedule or _make_fern_schedule(),
        next_runs=next_runs
        or [
            dt.datetime(2026, 4, 28, 9, 0, 0, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 29, 9, 0, 0, tzinfo=dt.timezone.utc),
        ],
    )


def _patch_schedules_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch get_skyvern() so each tool sees a fresh Mock with .schedules.*."""
    sched_client = MagicMock()
    sched_client.list_all = AsyncMock()
    sched_client.list = AsyncMock()
    sched_client.get = AsyncMock()
    sched_client.create = AsyncMock()
    sched_client.update = AsyncMock()
    sched_client.delete = AsyncMock()
    sched_client.enable = AsyncMock()
    sched_client.disable = AsyncMock()

    skyvern_mock = MagicMock()
    skyvern_mock.schedules = sched_client

    monkeypatch.setattr(
        "skyvern.cli.mcp_tools.schedule.get_skyvern",
        lambda: skyvern_mock,
    )
    return sched_client
