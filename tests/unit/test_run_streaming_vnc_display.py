import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import run_streaming
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class _StopAfterOneIteration(RuntimeError):
    pass


async def _run_one_iteration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    browser_session: object | None = None,
    browser_session_error: Exception | None = None,
    use_workflow_run: bool = False,
) -> tuple[SimpleNamespace, AsyncMock, MagicMock, MagicMock]:
    sleep_calls = 0

    async def stop_after_one_iteration(_interval: int) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise _StopAfterOneIteration

    runnable_id = "workflow-run-1" if use_workflow_run else "task-1"
    state = {
        "task_id": None if use_workflow_run else runnable_id,
        "workflow_run_id": runnable_id if use_workflow_run else None,
        "organization_id": "org-1",
    }
    get_browser_session = AsyncMock(return_value=browser_session)
    if browser_session_error is not None:
        get_browser_session.side_effect = browser_session_error

    database = SimpleNamespace(
        browser_sessions=SimpleNamespace(
            get_persistent_browser_session_by_runnable_id=get_browser_session,
        ),
        tasks=SimpleNamespace(
            get_task=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(is_final=lambda: False))),
        ),
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=SimpleNamespace(status=WorkflowRunStatus.running)),
        ),
    )
    save_streaming_file = AsyncMock()
    storage = SimpleNamespace(save_streaming_file=save_streaming_file)
    run_subprocess = MagicMock()
    log = MagicMock()

    monkeypatch.setattr(run_streaming, "start_forge_app", lambda: None)
    monkeypatch.setattr(run_streaming, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(run_streaming, "get_json_from_file", lambda _path: state)
    monkeypatch.setattr(run_streaming, "get_skyvern_state_file_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(run_streaming, "get_skyvern_temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(run_streaming.asyncio, "sleep", stop_after_one_iteration)
    monkeypatch.setattr(run_streaming.subprocess, "run", run_subprocess)
    monkeypatch.setattr(run_streaming.app, "DATABASE", database)
    monkeypatch.setattr(run_streaming.app, "STORAGE", storage)
    monkeypatch.setattr(run_streaming, "settings", SimpleNamespace(SKYVERN_DEFAULT_DISPLAY=123), raising=False)
    monkeypatch.setattr(run_streaming, "LOG", log)

    with pytest.raises(_StopAfterOneIteration):
        await run_streaming.run()

    return database, save_streaming_file, run_subprocess, log


@pytest.mark.asyncio
async def test_screenshot_uses_persisted_workflow_display_in_copied_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SKYVERN_TEST_PARENT_ENV", "kept")
    original_environment = os.environ.copy()

    database, save_streaming_file, run_subprocess, _log = await _run_one_iteration(
        monkeypatch,
        tmp_path,
        browser_session=SimpleNamespace(display_number=107),
        use_workflow_run=True,
    )

    database.browser_sessions.get_persistent_browser_session_by_runnable_id.assert_awaited_once_with(
        runnable_id="workflow-run-1",
        organization_id="org-1",
    )
    command = run_subprocess.call_args.args[0]
    child_environment = run_subprocess.call_args.kwargs["env"]
    assert command == (f"xwd -root | xwdtopnm 2>/dev/null | pnmtopng > {tmp_path}/org-1/workflow-run-1.png")
    assert run_subprocess.call_args.kwargs["shell"] is True
    assert child_environment["DISPLAY"] == ":107"
    assert child_environment["SKYVERN_TEST_PARENT_ENV"] == "kept"
    assert child_environment is not os.environ
    assert os.environ == original_environment
    save_streaming_file.assert_awaited_once_with("org-1", "workflow-run-1.png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "browser_session",
    [None, SimpleNamespace(display_number=None)],
    ids=["missing-session", "null-display"],
)
async def test_screenshot_falls_back_to_configured_default_display(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser_session: object | None,
) -> None:
    database, _save_streaming_file, run_subprocess, _log = await _run_one_iteration(
        monkeypatch,
        tmp_path,
        browser_session=browser_session,
    )

    database.browser_sessions.get_persistent_browser_session_by_runnable_id.assert_awaited_once_with(
        runnable_id="task-1",
        organization_id="org-1",
    )
    assert run_subprocess.call_args.kwargs["env"]["DISPLAY"] == ":123"


@pytest.mark.asyncio
async def test_browser_session_lookup_failure_skips_screenshot_and_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database, save_streaming_file, run_subprocess, log = await _run_one_iteration(
        monkeypatch,
        tmp_path,
        browser_session_error=RuntimeError("database unavailable"),
    )

    database.browser_sessions.get_persistent_browser_session_by_runnable_id.assert_awaited_once_with(
        runnable_id="task-1",
        organization_id="org-1",
    )
    run_subprocess.assert_not_called()
    save_streaming_file.assert_not_awaited()
    log.exception.assert_called_once()
