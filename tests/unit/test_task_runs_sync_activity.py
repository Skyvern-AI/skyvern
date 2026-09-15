import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine, insert, select
from sqlalchemy.orm import declarative_base


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise AssertionError("Could not locate repository root")


_SOURCE_FILE = _repo_root() / "workers" / "cron_worker" / "task_runs_sync_activity.py"
pytestmark = pytest.mark.skipif(not _SOURCE_FILE.exists(), reason="cloud-only: workers/cron_worker/ not present")


def _load_task_runs_sync_activity_module(monkeypatch: pytest.MonkeyPatch):
    base = declarative_base()

    class TaskRunModel(base):
        __tablename__ = "task_runs"

        id = Column(Integer, primary_key=True)
        run_id = Column(String)
        organization_id = Column(String)
        task_run_type = Column(String)
        status = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        script_run = Column(Boolean)
        workflow_permanent_id = Column(String)
        parent_workflow_run_id = Column(String)
        debug_session_id = Column(String)
        searchable_text = Column(Text)
        modified_at = Column(DateTime)
        title = Column(Text)
        url = Column(Text)
        created_at = Column(DateTime)

    class WorkflowRunModel(base):
        __tablename__ = "workflow_runs"

        id = Column(Integer, primary_key=True)
        workflow_run_id = Column(String)
        organization_id = Column(String)
        status = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        script_run = Column(Boolean)
        workflow_permanent_id = Column(String)
        parent_workflow_run_id = Column(String)
        debug_session_id = Column(String)

    class WorkflowRunAttemptModel(base):
        __tablename__ = "workflow_run_attempts"

        id = Column(Integer, primary_key=True)
        workflow_run_id = Column(String)
        organization_id = Column(String)
        attempt_number = Column(Integer)
        retry_decision = Column(String)
        next_attempt_prepared_at = Column(DateTime)

    class WorkflowRunParameterModel(base):
        __tablename__ = "workflow_run_parameters"

        id = Column(Integer, primary_key=True)
        workflow_run_id = Column(String)
        value = Column(Text)

    class TaskModel(base):
        __tablename__ = "tasks"

        id = Column(Integer, primary_key=True)
        task_id = Column(String)
        status = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        title = Column(Text)
        url = Column(Text)

    class TaskV2Model(base):
        __tablename__ = "observer_cruises"

        id = Column(Integer, primary_key=True)
        observer_cruise_id = Column(String)
        status = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        workflow_run_id = Column(String)
        prompt = Column(Text)

    models_module = ModuleType("skyvern.forge.sdk.db.models")
    models_module.TaskModel = TaskModel
    models_module.TaskRunModel = TaskRunModel
    models_module.TaskV2Model = TaskV2Model
    models_module.WorkflowRunModel = WorkflowRunModel
    models_module.WorkflowRunAttemptModel = WorkflowRunAttemptModel
    models_module.WorkflowRunParameterModel = WorkflowRunParameterModel

    cloud_db_stub = SimpleNamespace(Session=MagicMock())
    cloud_agent_db_module = ModuleType("cloud.db.cloud_agent_db")
    cloud_agent_db_module.cloud_db = cloud_db_stub

    temporalio_module = ModuleType("temporalio")
    temporalio_module.activity = SimpleNamespace(defn=lambda func: func)

    structlog_module = ModuleType("structlog")
    structlog_module.get_logger = lambda: SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None)

    runs_module = ModuleType("skyvern.forge.sdk.schemas.runs")
    runs_module.TERMINAL_STATUSES = ("completed", "failed", "terminated", "canceled", "timed_out")

    monkeypatch.setitem(sys.modules, "cloud", ModuleType("cloud"))
    monkeypatch.setitem(sys.modules, "cloud.db", ModuleType("cloud.db"))
    monkeypatch.setitem(sys.modules, "cloud.db.cloud_agent_db", cloud_agent_db_module)
    monkeypatch.setitem(sys.modules, "skyvern", ModuleType("skyvern"))
    monkeypatch.setitem(sys.modules, "skyvern.forge", ModuleType("skyvern.forge"))
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk", ModuleType("skyvern.forge.sdk"))
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.db", ModuleType("skyvern.forge.sdk.db"))
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.db.models", models_module)
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.schemas", ModuleType("skyvern.forge.sdk.schemas"))
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.schemas.runs", runs_module)
    monkeypatch.setitem(sys.modules, "temporalio", temporalio_module)
    monkeypatch.setitem(sys.modules, "structlog", structlog_module)

    module_path = _repo_root() / "workers" / "cron_worker" / "task_runs_sync_activity.py"
    spec = importlib.util.spec_from_file_location("test_task_runs_sync_activity_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _mock_session(rowcount: int) -> AsyncMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=rowcount))
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_task_runs_sync_activity_commits_each_successful_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    task_runs_sync_activity = _load_task_runs_sync_activity_module(monkeypatch)

    workflow_session = _mock_session(2)
    reopened_session = _mock_session(1)
    task_session = _mock_session(3)
    task_v2_session = _mock_session(5)

    monkeypatch.setattr(
        task_runs_sync_activity.cloud_db,
        "Session",
        MagicMock(side_effect=[workflow_session, reopened_session, task_session, task_v2_session]),
    )

    results = await task_runs_sync_activity.task_runs_sync_activity()

    assert results == {
        "workflow_runs_synced": 2,
        "workflow_runs_reopened": 1,
        "tasks_synced": 3,
        "task_v2_synced": 5,
        "errors": [],
    }
    workflow_session.commit.assert_awaited_once()
    reopened_session.commit.assert_awaited_once()
    task_session.commit.assert_awaited_once()
    task_v2_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_runs_sync_activity_handles_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If one sync step fails, the others should still succeed."""
    task_runs_sync_activity = _load_task_runs_sync_activity_module(monkeypatch)

    workflow_session = _mock_session(2)
    reopened_session = _mock_session(1)
    failing_session = _mock_session(0)
    failing_session.execute = AsyncMock(side_effect=Exception("DB error"))
    task_v2_session = _mock_session(5)

    monkeypatch.setattr(
        task_runs_sync_activity.cloud_db,
        "Session",
        MagicMock(side_effect=[workflow_session, reopened_session, failing_session, task_v2_session]),
    )

    results = await task_runs_sync_activity.task_runs_sync_activity()

    assert results["workflow_runs_synced"] == 2
    assert results["task_v2_synced"] == 5
    assert len(results["errors"]) == 1
    assert "tasks" in results["errors"][0].lower()


@pytest.mark.asyncio
async def test_sync_statements_include_created_at_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that all three sync statements include created_at >= cutoff."""
    mod = _load_task_runs_sync_activity_module(monkeypatch)

    cutoff = datetime.now(UTC)

    for builder_name in ("_build_sync_workflow_runs_stmt", "_build_sync_tasks_stmt", "_build_sync_task_v2_stmt"):
        builder = getattr(mod, builder_name)
        stmt = builder(cutoff)
        sql_text = str(stmt)
        assert "created_at" in sql_text, f"{builder_name} should filter by created_at"


@pytest.mark.asyncio
async def test_tasks_sync_includes_task_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task_v3 row must be repaired by the catch-up sync like task_v1/CUA rows, or a v3 run whose
    write-through failed would sit non-terminal in /runs history forever."""
    mod = _load_task_runs_sync_activity_module(monkeypatch)

    stmt = mod._build_sync_tasks_stmt(datetime.now(UTC))
    bound_values: list = []
    for value in stmt.compile().params.values():
        if isinstance(value, (list, tuple)):
            bound_values.extend(value)
        else:
            bound_values.append(value)
    assert "task_v3" in bound_values
    assert "task_v1" in bound_values  # existing types still covered


def test_reopened_run_sync_is_bounded_and_preserves_other_organizations(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_task_runs_sync_activity_module(monkeypatch)
    engine = create_engine("sqlite:///:memory:")
    mod.TaskRunModel.metadata.create_all(engine)
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=168)
    count = mod.REOPENED_RUN_BATCH_SIZE + 5
    with engine.begin() as conn:
        conn.execute(
            insert(mod.WorkflowRunModel),
            [
                {"id": i, "workflow_run_id": f"wr_{i:04}", "organization_id": "o_retry", "status": "running"}
                for i in range(count)
            ],
        )
        conn.execute(
            insert(mod.WorkflowRunAttemptModel),
            [
                {
                    "id": i,
                    "workflow_run_id": f"wr_{i:04}",
                    "organization_id": "o_retry",
                    "attempt_number": 1,
                    "retry_decision": "retry",
                    "next_attempt_prepared_at": now,
                }
                for i in range(count)
            ],
        )
        conn.execute(
            insert(mod.TaskRunModel),
            [
                {
                    "id": i,
                    "run_id": f"wr_{i:04}",
                    "organization_id": "o_retry",
                    "status": "failed",
                    "task_run_type": "workflow_run",
                    "finished_at": now,
                }
                for i in range(count)
            ],
        )
        conn.execute(
            insert(mod.TaskRunModel),
            {
                "id": count,
                "run_id": "wr_0000",
                "organization_id": "o_other",
                "status": "failed",
                "task_run_type": "workflow_run",
                "finished_at": now,
            },
        )
        stmt = mod._build_sync_reopened_workflow_runs_stmt(cutoff)
        conn.execute(stmt)
        statuses = conn.execute(select(mod.TaskRunModel.status)).scalars().all()
        assert statuses.count("running") == mod.REOPENED_RUN_BATCH_SIZE
        conn.execute(stmt)
        statuses = conn.execute(select(mod.TaskRunModel.status)).scalars().all()
        assert statuses.count("running") == count
        assert statuses.count("failed") == 1
        assert (
            conn.execute(select(mod.TaskRunModel.finished_at).where(mod.TaskRunModel.status == "running"))
            .scalars()
            .all()
            == [None] * count
        )
    engine.dispose()
