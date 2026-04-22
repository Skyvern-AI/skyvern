"""Unit tests for SKY-8818 observability warning on under-configured file_download blocks.

When a FileDownloadBlock is configured with `max_steps_per_run < MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD`,
the block must emit a structured LOG.warning with `log_code='file_download_low_max_steps'` at block
start, so Datadog can surface under-configured workflows.
"""

from unittest.mock import patch

import pytest

from skyvern.forge.sdk.workflow.models.block import (
    MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD,
    FileDownloadBlock,
    warn_if_file_download_max_steps_low,
)


def _make_block(max_steps: int | None) -> FileDownloadBlock:
    return FileDownloadBlock.model_construct(
        label="download_files",
        max_steps_per_run=max_steps,
    )


def test_threshold_constant_is_five() -> None:
    """Anchor the threshold so tuning is a deliberate change."""
    assert MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD == 5


@pytest.mark.parametrize("configured", [1, 2, 3, 4])
def test_warning_fires_below_threshold(configured: int) -> None:
    block = _make_block(configured)
    with patch("skyvern.forge.sdk.workflow.models.block.LOG") as mock_log:
        warn_if_file_download_max_steps_low(block, workflow_run_id="wr_test")
    assert mock_log.warning.call_count == 1
    _, kwargs = mock_log.warning.call_args
    assert kwargs.get("log_code") == "file_download_low_max_steps"
    assert kwargs.get("max_steps_per_run") == configured
    assert kwargs.get("recommended_minimum") == MAX_STEPS_DOWNLOAD_WARNING_THRESHOLD
    assert kwargs.get("workflow_run_id") == "wr_test"


@pytest.mark.parametrize("configured", [5, 6, 10, 25])
def test_warning_does_not_fire_at_or_above_threshold(configured: int) -> None:
    block = _make_block(configured)
    with patch("skyvern.forge.sdk.workflow.models.block.LOG") as mock_log:
        warn_if_file_download_max_steps_low(block, workflow_run_id="wr_test")
    assert mock_log.warning.call_count == 0


def test_warning_does_not_fire_when_max_steps_unset() -> None:
    """max_steps_per_run=None means the org-level default applies — not a misconfiguration signal."""
    block = _make_block(None)
    with patch("skyvern.forge.sdk.workflow.models.block.LOG") as mock_log:
        warn_if_file_download_max_steps_low(block, workflow_run_id="wr_test")
    assert mock_log.warning.call_count == 0
