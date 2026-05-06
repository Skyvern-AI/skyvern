from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.mcp_tools._validation import validate_schedule_id, validate_workflow_id
from skyvern.cli.mcp_tools.schedule import (
    _serialize_org_schedule_item,
    _serialize_schedule,
    _serialize_schedule_response,
    skyvern_schedule_create,
    skyvern_schedule_delete,
    skyvern_schedule_list,
    skyvern_schedule_update,
)
from skyvern.client.types.organization_schedule_item import OrganizationScheduleItem
from skyvern.client.types.workflow_schedule import WorkflowSchedule
from skyvern.client.types.workflow_schedule_response import WorkflowScheduleResponse

# -- Test fixtures (Fern types — NOT the backend Pydantic schema) --


def _make_fern_schedule(
    *,
    workflow_schedule_id: str = "wfs_test_1",
    organization_id: str = "o_test",
    workflow_permanent_id: str = "wpid_test_1",
    cron_expression: str = "0 9 * * *",
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


# -- ID validation --


class TestValidation:
    def test_validate_schedule_id_accepts_wfs_prefix_only(self) -> None:
        assert validate_schedule_id("wfs_123abc", "test") is None

    def test_validate_schedule_id_rejects_wsched_prefix(self) -> None:
        # The original plan had wsched_; the real prefix is wfs_. Locks in CORR-1.
        result = validate_schedule_id("wsched_123", "test")
        assert result is not None
        assert not result["ok"]
        assert "wfs_" in result["error"]["hint"]

    def test_validate_schedule_id_rejects_wpid_prefix(self) -> None:
        result = validate_schedule_id("wpid_123", "test")
        assert result is not None
        assert not result["ok"]

    def test_validate_schedule_id_rejects_path_separators(self) -> None:
        result = validate_schedule_id("wfs_../bad", "test")
        assert result is not None
        assert not result["ok"]

    def test_validate_schedule_id_rejects_empty(self) -> None:
        result = validate_schedule_id("", "test")
        assert result is not None

    def test_validate_workflow_id_accepts_wpid_prefix_only(self) -> None:
        assert validate_workflow_id("wpid_123", "test") is None
        bad = validate_workflow_id("wfs_123", "test")
        assert bad is not None and not bad["ok"]


# -- Serializers --


class TestSerializeSchedule:
    def test_serialize_schedule_includes_backend_id(self) -> None:
        # Fern type's attribute is literally `temporal_schedule_id`. We re-project it.
        s = _make_fern_schedule(temporal_schedule_id="ts_abc")
        out = _serialize_schedule(s)
        assert out["backend_schedule_id"] == "ts_abc"
        assert "temporal_schedule_id" not in out

    def test_serialize_schedule_handles_missing_temporal_id(self) -> None:
        s = _make_fern_schedule(temporal_schedule_id=None)
        out = _serialize_schedule(s)
        assert out["backend_schedule_id"] is None

    def test_serialize_schedule_returns_iso_datetimes(self) -> None:
        s = _make_fern_schedule()
        out = _serialize_schedule(s)
        assert isinstance(out["created_at"], str)
        assert "T" in out["created_at"]  # ISO format
        assert isinstance(out["modified_at"], str)

    def test_serialize_schedule_response_emits_iso_next_runs(self) -> None:
        resp = _make_fern_schedule_response()
        out = _serialize_schedule_response(resp)
        assert "schedule" in out and "next_runs" in out
        assert all(isinstance(r, str) and "T" in r for r in out["next_runs"])

    def test_serialize_org_schedule_item_omits_backend_id(self) -> None:
        item = OrganizationScheduleItem(
            workflow_schedule_id="wfs_test_1",
            organization_id="o_test",
            workflow_permanent_id="wpid_test_1",
            workflow_title="Probe Workflow",
            cron_expression="0 9 * * *",
            timezone="UTC",
            enabled=True,
            parameters=None,
            name="probe",
            description="test",
            next_run=dt.datetime(2026, 4, 28, 9, 0, 0, tzinfo=dt.timezone.utc),
            created_at=dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
            modified_at=dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc),
        )
        out = _serialize_org_schedule_item(item)
        assert "backend_schedule_id" not in out
        assert "workflow_title" in out
        assert isinstance(out["next_run"], str) and "T" in out["next_run"]


# -- Create --


class TestScheduleCreate:
    def test_rejects_invalid_workflow_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        result = asyncio.run(
            skyvern_schedule_create(
                workflow_permanent_id="not_a_wpid",
                cron_expression="0 9 * * *",
                timezone="UTC",
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
        sched_client.create.assert_not_called()

    def test_404_maps_to_invalid_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.client.core.api_error import ApiError

        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.create.side_effect = ApiError(status_code=404, body={"detail": "workflow not found"})
        result = asyncio.run(
            skyvern_schedule_create(
                workflow_permanent_id="wpid_test_1",
                cron_expression="0 9 * * *",
                timezone="UTC",
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
        assert "wpid_test_1" in result["error"]["message"]

    def test_happy_path_serializes_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.create.return_value = _make_fern_schedule_response()
        result = asyncio.run(
            skyvern_schedule_create(
                workflow_permanent_id="wpid_test_1",
                cron_expression="0 9 * * *",
                timezone="UTC",
                parameters={"k": "v"},
                name="probe",
                description="test",
            )
        )
        assert result["ok"]
        assert result["data"]["schedule"]["workflow_schedule_id"] == "wfs_test_1"
        sched_client.create.assert_called_once()


# -- List --


class TestScheduleList:
    def test_paging_metadata_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        resp = MagicMock()
        resp.schedules = []
        resp.total_count = 7
        resp.page = 2
        resp.page_size = 25
        sched_client.list_all.return_value = resp

        result = asyncio.run(skyvern_schedule_list(page=2, page_size=25))
        assert result["ok"]
        assert result["data"]["total_count"] == 7
        assert result["data"]["page"] == 2
        assert result["data"]["page_size"] == 25

    def test_status_filter_passed_through_to_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Validation is server-side; the client just forwards the value.
        sched_client = _patch_schedules_client(monkeypatch)
        resp = MagicMock()
        resp.schedules = []
        resp.total_count = 0
        resp.page = 1
        resp.page_size = 10
        sched_client.list_all.return_value = resp
        asyncio.run(skyvern_schedule_list(status="active"))
        sched_client.list_all.assert_called_once()
        kwargs = sched_client.list_all.call_args.kwargs
        assert kwargs["status"] == "active"


# -- Update — mutex / empty-patch / partial fetch+merge --


class TestScheduleUpdateGuards:
    def test_rejects_empty_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        result = asyncio.run(
            skyvern_schedule_update(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
        # No I/O: neither GET nor PUT should fire.
        sched_client.get.assert_not_called()
        sched_client.update.assert_not_called()

    def test_mutex_name_and_clear_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        result = asyncio.run(
            skyvern_schedule_update(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
                name="x",
                clear_name=True,
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
        sched_client.update.assert_not_called()


class TestScheduleUpdatePartial:
    def test_partial_preserves_existing_cron_and_timezone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        existing = _make_fern_schedule(
            cron_expression="*/15 * * * *",
            timezone="America/New_York",
            enabled=True,
            parameters={"old": "params"},
            name="old_name",
            description="old_desc",
        )
        sched_client.get.return_value = _make_fern_schedule_response(schedule=existing)
        sched_client.update.return_value = _make_fern_schedule_response()

        # Only update the name; everything else must come from the snapshot.
        asyncio.run(
            skyvern_schedule_update(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
                name="new_name",
            )
        )

        sched_client.get.assert_called_once_with("wpid_test_1", "wfs_test_1")
        kwargs = sched_client.update.call_args.kwargs
        assert kwargs["cron_expression"] == "*/15 * * * *"
        assert kwargs["timezone"] == "America/New_York"
        assert kwargs["enabled"] is True
        assert kwargs["parameters"] == {"old": "params"}
        assert kwargs["name"] == "new_name"
        assert kwargs["description"] == "old_desc"

    def test_clear_name_sends_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.get.return_value = _make_fern_schedule_response()
        sched_client.update.return_value = _make_fern_schedule_response()

        asyncio.run(
            skyvern_schedule_update(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
                clear_name=True,
            )
        )
        kwargs = sched_client.update.call_args.kwargs
        assert kwargs["name"] is None


# -- Update --exact path: completeness guards --


class TestScheduleUpdateExact:
    @pytest.fixture
    def base_kwargs(self) -> dict[str, Any]:
        return {
            "workflow_permanent_id": "wpid_test_1",
            "workflow_schedule_id": "wfs_test_1",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "enabled": False,
            "parameters": {"k": "v"},
            "name": "n",
            "description": "d",
            "exact": True,
        }

    def test_exact_skips_fetch(self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.update.return_value = _make_fern_schedule_response()
        asyncio.run(skyvern_schedule_update(**base_kwargs))
        sched_client.get.assert_not_called()
        sched_client.update.assert_called_once()

    def test_exact_full_body_includes_all_six_fields_in_put(
        self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]
    ) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.update.return_value = _make_fern_schedule_response()
        asyncio.run(skyvern_schedule_update(**base_kwargs))
        kwargs = sched_client.update.call_args.kwargs
        # Every replacement field must be in the SDK call (no OMIT slipping through).
        for field in ("cron_expression", "timezone", "enabled", "parameters", "name", "description"):
            assert field in kwargs, f"exact mode dropped {field}"
        assert kwargs["enabled"] is False
        assert kwargs["parameters"] == {"k": "v"}

    def test_exact_rejects_missing_enabled(self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        base_kwargs["enabled"] = None
        result = asyncio.run(skyvern_schedule_update(**base_kwargs))
        assert not result["ok"]
        assert "enabled" in result["error"]["message"]
        sched_client.update.assert_not_called()

    def test_exact_rejects_missing_parameters(
        self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]
    ) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        base_kwargs["parameters"] = None
        # clear_parameters defaults False, so this should fail.
        result = asyncio.run(skyvern_schedule_update(**base_kwargs))
        assert not result["ok"]
        assert "parameters" in result["error"]["message"]
        sched_client.update.assert_not_called()

    def test_exact_rejects_missing_name(self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        base_kwargs["name"] = None
        result = asyncio.run(skyvern_schedule_update(**base_kwargs))
        assert not result["ok"]
        assert "name" in result["error"]["message"]
        sched_client.update.assert_not_called()

    def test_exact_rejects_missing_description(
        self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]
    ) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        base_kwargs["description"] = None
        result = asyncio.run(skyvern_schedule_update(**base_kwargs))
        assert not result["ok"]
        assert "description" in result["error"]["message"]
        sched_client.update.assert_not_called()

    def test_exact_with_clear_flags_for_nullable_fields_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, base_kwargs: dict[str, Any]
    ) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        sched_client.update.return_value = _make_fern_schedule_response()
        # Drop the explicit values for parameters/name/description; pass clear flags instead.
        base_kwargs.pop("parameters")
        base_kwargs.pop("name")
        base_kwargs.pop("description")
        base_kwargs["clear_parameters"] = True
        base_kwargs["clear_name"] = True
        base_kwargs["clear_description"] = True
        result = asyncio.run(skyvern_schedule_update(**base_kwargs))
        assert result["ok"]
        kwargs = sched_client.update.call_args.kwargs
        assert kwargs["parameters"] is None
        assert kwargs["name"] is None
        assert kwargs["description"] is None


# -- Delete --


class TestScheduleDelete:
    def test_requires_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        result = asyncio.run(
            skyvern_schedule_delete(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
            )
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
        sched_client.delete.assert_not_called()

    def test_with_force_calls_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sched_client = _patch_schedules_client(monkeypatch)
        delete_resp = MagicMock()
        delete_resp.ok = True
        sched_client.delete.return_value = delete_resp
        result = asyncio.run(
            skyvern_schedule_delete(
                workflow_permanent_id="wpid_test_1",
                workflow_schedule_id="wfs_test_1",
                force=True,
            )
        )
        assert result["ok"]
        assert result["data"]["deleted"] is True


# -- 404 → INVALID_INPUT mapping for write tools --


class TestSchedule404Mapping:
    @staticmethod
    def _setup_404(monkeypatch: pytest.MonkeyPatch, sdk_attr: str) -> MagicMock:
        from skyvern.client.core.api_error import ApiError

        sched_client = _patch_schedules_client(monkeypatch)
        getattr(sched_client, sdk_attr).side_effect = ApiError(status_code=404, body={"detail": "not found"})
        return sched_client

    def test_enable_404_maps_to_invalid_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools.schedule import skyvern_schedule_enable

        self._setup_404(monkeypatch, "enable")
        result = asyncio.run(
            skyvern_schedule_enable(workflow_permanent_id="wpid_test_1", workflow_schedule_id="wfs_test_1")
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"

    def test_disable_404_maps_to_invalid_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools.schedule import skyvern_schedule_disable

        self._setup_404(monkeypatch, "disable")
        result = asyncio.run(
            skyvern_schedule_disable(workflow_permanent_id="wpid_test_1", workflow_schedule_id="wfs_test_1")
        )
        assert not result["ok"]
        assert result["error"]["code"] == "INVALID_INPUT"
