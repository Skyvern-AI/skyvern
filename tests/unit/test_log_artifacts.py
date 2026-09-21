"""Tests for skyvern.forge.sdk.log_artifacts."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge import app
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base, WorkflowRunAttemptModel
from skyvern.forge.sdk.forge_log import skyvern_logs_processor
from skyvern.forge.sdk.log_artifacts import (
    save_step_logs,
    save_task_logs,
    save_workflow_run_block_logs,
    save_workflow_run_logs,
)
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import RetryDecision
from skyvern.forge.sdk.workflow.service import WorkflowService


@pytest.mark.asyncio
async def test_save_workflow_run_logs_no_context_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test for timeout activity crash.

    When ``save_workflow_run_logs`` is called from a code path that lacks a
    ``skyvern_context`` (e.g. the Temporal timeout activity), it must not raise.
    The function's purpose is to flush the in-memory log buffer that lives on
    the current context. With no context there is no buffer to flush, so the
    call must degrade to a no-op rather than crash the surrounding DB update.
    """
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)
    skyvern_context.reset()
    assert skyvern_context.current() is None

    with (
        patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save,
        patch("skyvern.forge.sdk.log_artifacts.LOG") as mock_log,
    ):
        # Must not raise RuntimeError("No skyvern context")
        await save_workflow_run_logs("wr_test_no_context")
        # And must not attempt to persist anything when there's nothing to flush.
        mock_save.assert_not_called()
        # Logged at debug — Temporal cleanup is a known routine no-context caller,
        # so anything higher would be steady-state noise.
        mock_log.debug.assert_called_once()


@pytest.mark.asyncio
async def test_save_workflow_run_logs_with_context_filters_by_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """When context is present, we still filter context.log by workflow_run_id."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)

    context = skyvern_context.SkyvernContext(
        organization_id="o_test",
        workflow_run_id="wr_match",
        log=[
            {"workflow_run_id": "wr_match", "msg": "keep"},
            {"workflow_run_id": "wr_other", "msg": "drop"},
        ],
    )
    skyvern_context.reset()
    skyvern_context.set(context)

    try:
        with patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save:
            await save_workflow_run_logs("wr_match")

        mock_save.assert_awaited_once()
        kwargs = mock_save.await_args.kwargs
        assert kwargs["organization_id"] == "o_test"
        assert kwargs["workflow_run_id"] == "wr_match"
        assert kwargs["log"] == [{"workflow_run_id": "wr_match", "msg": "keep"}]
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_save_step_logs_no_context_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_step_logs must tolerate a missing skyvern_context."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)
    skyvern_context.reset()
    assert skyvern_context.current() is None

    with (
        patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save,
        patch("skyvern.forge.sdk.log_artifacts.LOG") as mock_log,
    ):
        await save_step_logs("step_test_no_context")
        mock_save.assert_not_called()
        mock_log.debug.assert_called_once()


@pytest.mark.asyncio
async def test_save_step_logs_with_context_filters_by_step_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_step_logs still filters context.log by step_id when context is present."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)

    context = skyvern_context.SkyvernContext(
        organization_id="o_test",
        step_id="step_match",
        log=[
            {"step_id": "step_match", "msg": "keep"},
            {"step_id": "step_other", "msg": "drop"},
        ],
    )
    skyvern_context.reset()
    skyvern_context.set(context)

    try:
        with patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save:
            await save_step_logs("step_match")

        mock_save.assert_awaited_once()
        kwargs = mock_save.await_args.kwargs
        assert kwargs["organization_id"] == "o_test"
        assert kwargs["step_id"] == "step_match"
        assert kwargs["log"] == [{"step_id": "step_match", "msg": "keep"}]
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_save_task_logs_no_context_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_task_logs must tolerate a missing skyvern_context."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)
    skyvern_context.reset()
    assert skyvern_context.current() is None

    with (
        patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save,
        patch("skyvern.forge.sdk.log_artifacts.LOG") as mock_log,
    ):
        await save_task_logs("tsk_test_no_context")
        mock_save.assert_not_called()
        mock_log.debug.assert_called_once()


@pytest.mark.asyncio
async def test_save_task_logs_with_context_filters_by_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_task_logs still filters context.log by task_id when context is present."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)

    context = skyvern_context.SkyvernContext(
        organization_id="o_test",
        task_id="tsk_match",
        log=[
            {"task_id": "tsk_match", "msg": "keep"},
            {"task_id": "tsk_other", "msg": "drop"},
        ],
    )
    skyvern_context.reset()
    skyvern_context.set(context)

    try:
        with patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save:
            await save_task_logs("tsk_match")

        mock_save.assert_awaited_once()
        kwargs = mock_save.await_args.kwargs
        assert kwargs["organization_id"] == "o_test"
        assert kwargs["task_id"] == "tsk_match"
        assert kwargs["log"] == [{"task_id": "tsk_match", "msg": "keep"}]
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_save_workflow_run_block_logs_no_context_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_workflow_run_block_logs must tolerate a missing skyvern_context."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)
    skyvern_context.reset()
    assert skyvern_context.current() is None

    with (
        patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save,
        patch("skyvern.forge.sdk.log_artifacts.LOG") as mock_log,
    ):
        await save_workflow_run_block_logs("wrb_test_no_context")
        mock_save.assert_not_called()
        mock_log.debug.assert_called_once()


@pytest.mark.asyncio
async def test_save_workflow_run_block_logs_with_context_filters_by_block_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_workflow_run_block_logs still filters context.log by workflow_run_block_id."""
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)

    context = skyvern_context.SkyvernContext(
        organization_id="o_test",
        workflow_run_block_id="wrb_match",
        log=[
            {"workflow_run_block_id": "wrb_match", "msg": "keep"},
            {"workflow_run_block_id": "wrb_other", "msg": "drop"},
        ],
    )
    skyvern_context.reset()
    skyvern_context.set(context)

    try:
        with patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save:
            await save_workflow_run_block_logs("wrb_match")

        mock_save.assert_awaited_once()
        kwargs = mock_save.await_args.kwargs
        assert kwargs["organization_id"] == "o_test"
        assert kwargs["workflow_run_block_id"] == "wrb_match"
        assert kwargs["log"] == [{"workflow_run_block_id": "wrb_match", "msg": "keep"}]
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_context_during_lookup", [False, True])
@pytest.mark.parametrize("attempt_raises", [False, True])
async def test_retry_logs_keep_both_attempts_and_update_only_current_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory,
    replace_context_during_lookup: bool,
    attempt_raises: bool,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=engine)
    manager = ArtifactManager()
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", manager)
    monkeypatch.setattr(app, "STORAGE", LocalStorage(str(tmp_path)))
    monkeypatch.setattr("skyvern.forge.sdk.log_artifacts.settings.ENABLE_LOG_ARTIFACTS", True)
    context = skyvern_context.SkyvernContext(
        organization_id="o_logs",
        workflow_run_id="wr_logs",
        log=[{"workflow_run_id": "wr_logs", "event": "legacy entry"}],
    )
    skyvern_context.set(context)
    original_get_attempts = database.workflow_run_attempts.get_attempts
    lookup_entered = asyncio.Event()
    release_lookup = asyncio.Event()
    attempt_one_contents: dict[str, bytes] = {}
    attempt_two_contents: dict[str, bytes] = {}

    async def delayed_get_attempts(workflow_run_id: str) -> list[WorkflowRunAttemptModel]:
        lookup_entered.set()
        await release_lookup.wait()
        return await original_get_attempts(workflow_run_id)

    service = WorkflowService()
    run = SimpleNamespace(
        workflow_run_id="wr_logs",
        organization_id="o_logs",
        status=WorkflowRunStatus.failed,
        depends_on_workflow_run_id=None,
    )
    release_late_log = asyncio.Event()
    late_log_task: asyncio.Task[None] | None = None

    def set_workflow_context(attempt: int) -> None:
        monkeypatch.setattr(
            app,
            "WORKFLOW_CONTEXT_MANAGER",
            workflow_context_manager_factory(workflow_run_id="wr_logs", attempt_number=attempt, mask_secrets=False),
        )

    async def log_and_save(message: str) -> None:
        skyvern_logs_processor(logging.getLogger(__name__), "info", {"workflow_run_id": "wr_logs", "event": message})
        await save_workflow_run_logs("wr_logs")
        await manager.wait_for_upload_aiotasks(["wr_logs"])

    async def save_late_log() -> None:
        await release_late_log.wait()
        await log_and_save("late first")

    async def execute_attempt(*, attempt_number: int, **_kwargs: Any) -> Any:
        nonlocal attempt_one_contents, attempt_two_contents, late_log_task
        set_workflow_context(attempt_number)
        if attempt_number == 1:
            await log_and_save("first failed")
            attempt_one_contents = {
                artifact.uri: Path(artifact.uri.removeprefix("file://")).read_bytes()
                for artifact in await database.artifacts.get_artifacts_by_entity_id(
                    workflow_run_id="wr_logs", organization_id="o_logs"
                )
            }
            late_log_task = asyncio.create_task(save_late_log())
            if attempt_raises:
                raise RuntimeError("attempt failed")
        else:
            assert attempt_number == 2
            await log_and_save("second started")
            await log_and_save("second completed")
            app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts.pop("wr_logs")
            await save_workflow_run_logs("wr_logs")
            await manager.wait_for_upload_aiotasks(["wr_logs"])
            for uri, content in attempt_one_contents.items():
                assert Path(uri.removeprefix("file://")).read_bytes() == content
            attempt_two_contents = {
                artifact.uri: Path(artifact.uri.removeprefix("file://")).read_bytes()
                for artifact in await database.artifacts.get_artifacts_by_entity_id(
                    workflow_run_id="wr_logs", organization_id="o_logs"
                )
                if "/attempts/2/" in artifact.uri
            }
            run.status = WorkflowRunStatus.completed
        return run

    async def prepare_attempt(**_kwargs: Any) -> SimpleNamespace:
        async with database.Session() as session:
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_logs", organization_id="o_logs", attempt_number=2, status="running"
                )
            )
            await session.commit()
        return SimpleNamespace(status="inserted", pinned_browser_session_id=None, serialized_identity=True)

    async def wait_for_clearance(_run: Any, _attempt_number: int) -> None:
        assert skyvern_context.current_workflow_log_attempt("wr_logs") is None

    monkeypatch.setattr(service, "execute_workflow", execute_attempt)
    monkeypatch.setattr(service, "_wait_for_retry_sequential_clearance", wait_for_clearance)
    monkeypatch.setattr(service, "_run_interim_side_effects_with_retries", AsyncMock(return_value="released"))
    monkeypatch.setattr(service, "_run_terminal_side_effects_with_retries", AsyncMock(return_value="released"))
    monkeypatch.setattr(database.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(
        database.workflow_run_attempts, "claim_prepared_attempt_execution", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(service_module, "prepare_next_attempt_result", prepare_attempt)
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(
            side_effect=[
                RetryDecision(True, 1, 0, True, "matched"),
                RetryDecision(True, 1, 0, True, "matched"),
                RetryDecision(False, 2, 0, True, "budget_exhausted"),
            ]
        ),
    )

    try:
        async with database.Session() as session:
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_logs", organization_id="o_logs", attempt_number=1, status="running"
                )
            )
            await session.commit()
        await service.execute_workflow_with_retries(
            workflow_run_id="wr_logs", api_key=None, organization=SimpleNamespace(organization_id="o_logs")
        )
        assert attempt_one_contents
        assert attempt_two_contents
        assert skyvern_context.current_workflow_log_attempt("wr_logs") is None
        assert late_log_task is not None
        set_workflow_context(1)
        if replace_context_during_lookup:
            monkeypatch.setattr(database.workflow_run_attempts, "get_attempts", delayed_get_attempts)
        release_late_log.set()
        if replace_context_during_lookup:
            await asyncio.wait_for(lookup_entered.wait(), timeout=5)
            set_workflow_context(2)
            release_lookup.set()
        await late_log_task
        artifacts = await database.artifacts.get_artifacts_by_entity_id(
            workflow_run_id="wr_logs", organization_id="o_logs"
        )
        assert len(artifacts) == 4
        for artifact in artifacts:
            assert artifact.artifact_type in (ArtifactType.SKYVERN_LOG, ArtifactType.SKYVERN_LOG_RAW)
            content = Path(artifact.uri.removeprefix("file://")).read_bytes()
            assert b"legacy entry" in content
            if "/attempts/2/" in artifact.uri:
                assert content == attempt_two_contents[artifact.uri]
                assert b"second started" in content
                assert b"second completed" in content
                assert b"first failed" not in content
                assert b"late first" not in content
            else:
                assert "/wr_logs/" in artifact.uri
                assert "/attempts/" not in artifact.uri
                assert b"first failed" in content
                assert b"late first" in content
                assert b"second started" not in content
                assert b"second completed" not in content
    finally:
        if late_log_task is not None and not late_log_task.done():
            late_log_task.cancel()
            await asyncio.gather(late_log_task, return_exceptions=True)
        skyvern_context.reset()
        await engine.dispose()
