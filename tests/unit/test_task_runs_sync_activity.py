import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise AssertionError("Could not locate repository root")


def _load_task_runs_sync_activity_module(monkeypatch: pytest.MonkeyPatch):
    base = declarative_base()

    class TaskRunModel(base):
        __tablename__ = "task_runs"

        id = Column(Integer, primary_key=True)
        run_id = Column(String)
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
        status = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        script_run = Column(Boolean)
        workflow_permanent_id = Column(String)
        parent_workflow_run_id = Column(String)
        debug_session_id = Column(String)

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
    if not module_path.exists():
        pytest.skip(f"Cloud-only module not found: {module_path}")
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
    task_session = _mock_session(3)
    task_v2_session = _mock_session(5)

    monkeypatch.setattr(
        task_runs_sync_activity.cloud_db,
        "Session",
        MagicMock(side_effect=[workflow_session, task_session, task_v2_session]),
    )

    results = await task_runs_sync_activity.task_runs_sync_activity()

    assert results == {
        "workflow_runs_synced": 2,
        "tasks_synced": 3,
        "task_v2_synced": 5,
        "errors": [],
    }
    workflow_session.commit.assert_awaited_once()
    task_session.commit.assert_awaited_once()
    task_v2_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_runs_sync_activity_handles_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If one sync step fails, the others should still succeed."""
    task_runs_sync_activity = _load_task_runs_sync_activity_module(monkeypatch)

    workflow_session = _mock_session(2)
    failing_session = _mock_session(0)
    failing_session.execute = AsyncMock(side_effect=Exception("DB error"))
    task_v2_session = _mock_session(5)

    monkeypatch.setattr(
        task_runs_sync_activity.cloud_db,
        "Session",
        MagicMock(side_effect=[workflow_session, failing_session, task_v2_session]),
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
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc)

    for builder_name in ("_build_sync_workflow_runs_stmt", "_build_sync_tasks_stmt", "_build_sync_task_v2_stmt"):
        builder = getattr(mod, builder_name)
        stmt = builder(cutoff)
        sql_text = str(stmt)
        assert "created_at" in sql_text, f"{builder_name} should filter by created_at"
