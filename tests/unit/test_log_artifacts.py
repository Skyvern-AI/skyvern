"""Tests for skyvern.forge.sdk.log_artifacts."""

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.log_artifacts import save_workflow_run_logs


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

    with patch("skyvern.forge.sdk.log_artifacts._save_log_artifacts", new_callable=AsyncMock) as mock_save:
        # Must not raise RuntimeError("No skyvern context")
        await save_workflow_run_logs("wr_test_no_context")
        # And must not attempt to persist anything when there's nothing to flush.
        mock_save.assert_not_called()


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
