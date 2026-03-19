"""Tests for orphan prevention and schedule cascade deletion (SKY-8186)."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.workflow.service import WorkflowService


class _FakeResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[object]:
        return self._values


class _FakeSession:
    def __init__(self, execute_side_effect: list[object]) -> None:
        self.execute = AsyncMock(side_effect=execute_side_effect)
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


@pytest.fixture
def workflow_service() -> WorkflowService:
    return WorkflowService()


@pytest.mark.asyncio
async def test_soft_delete_workflow_and_schedules_commits_once() -> None:
    db = object.__new__(AgentDB)
    fake_session = _FakeSession(
        execute_side_effect=[
            _FakeResult(["wfs_123", "wfs_456"]),
            None,
            None,
        ]
    )
    db.Session = lambda: fake_session  # type: ignore[method-assign]

    deleted_schedule_ids = await db.soft_delete_workflow_and_schedules_by_permanent_id(
        workflow_permanent_id="wpid_abc",
        organization_id="org_xyz",
    )

    assert deleted_schedule_ids == ["wfs_123", "wfs_456"]
    assert fake_session.execute.await_count == 3
    fake_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_workflow_uses_atomic_schedule_delete_path(
    monkeypatch: pytest.MonkeyPatch, workflow_service: WorkflowService
) -> None:
    """Deleting a workflow should use a single DB call for workflow + schedule deletion."""
    from skyvern.forge import app

    mock_atomic_delete = AsyncMock(return_value=["wfs_123", "wfs_456"])
    monkeypatch.setattr(
        app.DATABASE,
        "soft_delete_workflow_and_schedules_by_permanent_id",
        mock_atomic_delete,
        raising=False,
    )

    await workflow_service.delete_workflow_by_permanent_id(
        workflow_permanent_id="wpid_abc",
        organization_id="org_xyz",
    )

    mock_atomic_delete.assert_awaited_once_with(
        workflow_permanent_id="wpid_abc",
        organization_id="org_xyz",
    )


@pytest.mark.asyncio
async def test_delete_workflow_no_schedules(monkeypatch: pytest.MonkeyPatch, workflow_service: WorkflowService) -> None:
    """Deleting a workflow with no schedules should still delete the workflow."""
    from skyvern.forge import app

    mock_atomic_delete = AsyncMock(return_value=[])
    monkeypatch.setattr(
        app.DATABASE,
        "soft_delete_workflow_and_schedules_by_permanent_id",
        mock_atomic_delete,
        raising=False,
    )

    await workflow_service.delete_workflow_by_permanent_id(
        workflow_permanent_id="wpid_no_schedules",
        organization_id="org_xyz",
    )

    mock_atomic_delete.assert_awaited_once_with(
        workflow_permanent_id="wpid_no_schedules",
        organization_id="org_xyz",
    )


@pytest.mark.asyncio
async def test_delete_workflow_atomic_delete_receives_same_inputs(
    monkeypatch: pytest.MonkeyPatch, workflow_service: WorkflowService
) -> None:
    """The service should pass the workflow identity through unchanged."""
    from skyvern.forge import app

    mock_atomic_delete = AsyncMock(return_value=["wfs_1"])
    monkeypatch.setattr(
        app.DATABASE,
        "soft_delete_workflow_and_schedules_by_permanent_id",
        mock_atomic_delete,
        raising=False,
    )

    await workflow_service.delete_workflow_by_permanent_id(
        workflow_permanent_id="wpid_order_test",
        organization_id="org_xyz",
    )

    mock_atomic_delete.assert_awaited_once_with(
        workflow_permanent_id="wpid_order_test",
        organization_id="org_xyz",
    )


@pytest.mark.asyncio
async def test_delete_workflow_without_organization_id(
    monkeypatch: pytest.MonkeyPatch, workflow_service: WorkflowService
) -> None:
    """Atomic deletion should work without organization_id."""
    from skyvern.forge import app

    mock_atomic_delete = AsyncMock(return_value=[])
    monkeypatch.setattr(
        app.DATABASE,
        "soft_delete_workflow_and_schedules_by_permanent_id",
        mock_atomic_delete,
        raising=False,
    )

    await workflow_service.delete_workflow_by_permanent_id(
        workflow_permanent_id="wpid_no_org",
    )

    mock_atomic_delete.assert_awaited_once_with(
        workflow_permanent_id="wpid_no_org",
        organization_id=None,
    )


@pytest.mark.asyncio
async def test_delete_workflow_by_id_remains_version_scoped(
    monkeypatch: pytest.MonkeyPatch, workflow_service: WorkflowService
) -> None:
    """delete_workflow_by_id is rollback-only and should not cascade schedules."""
    from skyvern.forge import app

    mock_delete_by_id = AsyncMock()
    mock_atomic_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE, "soft_delete_workflow_by_id", mock_delete_by_id)
    monkeypatch.setattr(
        app.DATABASE,
        "soft_delete_workflow_and_schedules_by_permanent_id",
        mock_atomic_delete,
        raising=False,
    )

    await workflow_service.delete_workflow_by_id(
        workflow_id="wf_rollback_only",
        organization_id="org_xyz",
    )

    mock_delete_by_id.assert_awaited_once_with(
        workflow_id="wf_rollback_only",
        organization_id="org_xyz",
    )
    mock_atomic_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_soft_delete_orphaned_schedules_uses_single_returning_update() -> None:
    db = object.__new__(AgentDB)
    fake_session = _FakeSession(
        execute_side_effect=[
            _FakeResult([("wfs_123", "wpid_abc")]),
        ]
    )
    db.Session = lambda: fake_session  # type: ignore[method-assign]

    orphaned = await db.soft_delete_orphaned_schedules()

    assert orphaned == [("wfs_123", "wpid_abc")]
    assert fake_session.execute.await_count == 1
    fake_session.commit.assert_awaited_once()
    query = fake_session.execute.await_args.args[0]
    compiled_query = str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LIMIT 500" in compiled_query
    assert (
        "RETURNING workflow_schedules.workflow_schedule_id, workflow_schedules.workflow_permanent_id" in compiled_query
    )
