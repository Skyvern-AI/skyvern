"""Unit tests for the screenshot streaming worker (``run_streaming.py``).

These lock in two guarantees for the minimal-runtime rewrite:
  1. the screenshot loop still retrieves the same run data and emits the same
     ``save_streaming_file`` payload as before, and
  2. the database engine the worker owns is deterministically disposed when the
     loop exits (deterministic shutdown of the only heavyweight resource it now
     initializes).
"""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

_RUN_STREAMING_PATH = Path(__file__).resolve().parents[2] / "run_streaming.py"
_spec = importlib.util.spec_from_file_location("run_streaming", _RUN_STREAMING_PATH)
assert _spec and _spec.loader
run_streaming = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_streaming)

_UNSET = object()


def _install_worker_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_payloads: list[object],
    *,
    workflow_run: object = _UNSET,
    task: object = _UNSET,
) -> SimpleNamespace:
    """Wire ``run_streaming`` with in-memory fakes and a scripted state-file reader.

    ``state_payloads`` is consumed one entry per loop iteration: a dict is
    returned from ``get_json_from_file``; an exception instance is raised (use
    ``asyncio.CancelledError`` to break the otherwise-infinite loop).

    ``workflow_run`` / ``task`` override the row returned by the DB reads (default:
    an active run and a non-final task); pass ``None`` to simulate a missing row.
    """
    dispose = AsyncMock()
    wr_result = SimpleNamespace(status=WorkflowRunStatus.running) if workflow_run is _UNSET else workflow_run
    task_result = SimpleNamespace(status=SimpleNamespace(is_final=lambda: False)) if task is _UNSET else task
    get_workflow_run = AsyncMock(return_value=wr_result)
    get_task = AsyncMock(return_value=task_result)
    save_streaming_file = AsyncMock()

    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
            tasks=SimpleNamespace(get_task=get_task),
            engine=SimpleNamespace(dispose=dispose),
        ),
        STORAGE=SimpleNamespace(save_streaming_file=save_streaming_file),
    )

    monkeypatch.setattr(run_streaming, "start_streaming_worker_app", lambda: None)
    monkeypatch.setattr(run_streaming, "app", fake_app)
    monkeypatch.setattr(run_streaming, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(run_streaming, "INTERVAL", 0)
    monkeypatch.setattr(run_streaming, "get_skyvern_state_file_path", lambda: str(tmp_path / "state.json"))
    monkeypatch.setattr(run_streaming, "get_skyvern_temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(run_streaming, "subprocess", MagicMock())
    monkeypatch.setattr(run_streaming, "os", MagicMock())

    payloads = iter(state_payloads)

    def _next_state(_path: str) -> object:
        value = next(payloads)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(run_streaming, "get_json_from_file", _next_state)

    return SimpleNamespace(
        app=fake_app,
        dispose=dispose,
        get_workflow_run=get_workflow_run,
        get_task=get_task,
        save_streaming_file=save_streaming_file,
    )


@pytest.mark.asyncio
async def test_run_streams_screenshot_for_active_workflow_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fakes = _install_worker_fakes(
        monkeypatch,
        tmp_path,
        state_payloads=[
            {"task_id": None, "workflow_run_id": "wr_1", "organization_id": "o_1"},
            asyncio.CancelledError(),
        ],
    )

    with pytest.raises(asyncio.CancelledError):
        await run_streaming.run()

    fakes.get_workflow_run.assert_awaited_once_with(workflow_run_id="wr_1")
    fakes.save_streaming_file.assert_awaited_once_with("o_1", "wr_1.png")


@pytest.mark.asyncio
async def test_run_streams_screenshot_for_active_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The task path uses a different DB call (get_task, scoped by organization_id)
    # and a different file name ({task_id}.png) than the workflow-run path.
    fakes = _install_worker_fakes(
        monkeypatch,
        tmp_path,
        state_payloads=[
            {"task_id": "t_1", "workflow_run_id": None, "organization_id": "o_1"},
            asyncio.CancelledError(),
        ],
    )

    with pytest.raises(asyncio.CancelledError):
        await run_streaming.run()

    fakes.get_task.assert_awaited_once_with(task_id="t_1", organization_id="o_1")
    fakes.save_streaming_file.assert_awaited_once_with("o_1", "t_1.png")


@pytest.mark.asyncio
async def test_run_skips_screenshot_when_workflow_run_finalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A finalized run must short-circuit (continue) before any screenshot is saved.
    fakes = _install_worker_fakes(
        monkeypatch,
        tmp_path,
        state_payloads=[
            {"task_id": None, "workflow_run_id": "wr_1", "organization_id": "o_1"},
            asyncio.CancelledError(),
        ],
        workflow_run=SimpleNamespace(status=WorkflowRunStatus.completed),
    )

    with pytest.raises(asyncio.CancelledError):
        await run_streaming.run()

    fakes.get_workflow_run.assert_awaited_once_with(workflow_run_id="wr_1")
    fakes.save_streaming_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_disposes_database_engine_on_shutdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fakes = _install_worker_fakes(
        monkeypatch,
        tmp_path,
        state_payloads=[asyncio.CancelledError()],
    )

    with pytest.raises(asyncio.CancelledError):
        await run_streaming.run()

    fakes.dispose.assert_awaited_once()
