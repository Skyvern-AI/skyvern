"""Tests for skyvern.forge.sdk.log_artifacts."""

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.log_artifacts import (
    save_step_logs,
    save_task_logs,
    save_workflow_run_block_logs,
    save_workflow_run_logs,
)


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
