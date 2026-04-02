"""Tests that for-loop child blocks are tracked in blocks_to_update.

When a ForLoopBlock completes and its child blocks (e.g., file_download)
are not yet in script_blocks_by_label, they should be added to
blocks_to_update so the script generator produces cached functions for them.

This was the root cause of file_download blocks inside for-loops never
getting cached: they executed via block.py's execute_loop_helper() which
bypasses the blocks_to_update tracking in service.py's _execute_single_block().
"""

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import (
    BlockType,
    FileDownloadBlock,
    ForLoopBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED


def _make_output_param(label: str) -> OutputParameter:
    now = datetime.now(tz=timezone.utc)
    return OutputParameter(
        key=f"{label}_output",
        parameter_type="output",
        output_parameter_id=f"op_{label}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def test_file_download_is_cacheable():
    """FILE_DOWNLOAD must be in BLOCK_TYPES_THAT_SHOULD_BE_CACHED."""
    assert BlockType.FILE_DOWNLOAD in BLOCK_TYPES_THAT_SHOULD_BE_CACHED


def test_forloop_child_labels_can_be_collected():
    """Verify we can iterate ForLoopBlock.loop_blocks to find uncached children."""
    download_block = FileDownloadBlock(
        label="download_file",
        output_parameter=_make_output_param("download_file"),
        url="http://example.com",
        navigation_goal="Download the file",
    )
    loop_block = ForLoopBlock(
        label="download_loop",
        output_parameter=_make_output_param("download_loop"),
        loop_blocks=[download_block],
    )

    # Simulate the tracking logic from service.py
    script_blocks_by_label: dict[str, object] = {}  # empty = nothing cached
    blocks_to_update: set[str] = set()

    for loop_child in loop_block.loop_blocks:
        if (
            loop_child.label
            and loop_child.label not in script_blocks_by_label
            and loop_child.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        ):
            blocks_to_update.add(loop_child.label)

    assert "download_file" in blocks_to_update


def test_forloop_child_already_cached_not_tracked():
    """If the child block is already cached, it should NOT be added to blocks_to_update."""
    download_block = FileDownloadBlock(
        label="download_file",
        output_parameter=_make_output_param("download_file"),
        url="http://example.com",
        navigation_goal="Download the file",
    )
    loop_block = ForLoopBlock(
        label="download_loop",
        output_parameter=_make_output_param("download_loop"),
        loop_blocks=[download_block],
    )

    # Simulate: download_file is already in script_blocks_by_label
    script_blocks_by_label = {"download_file": object()}
    blocks_to_update: set[str] = set()

    for loop_child in loop_block.loop_blocks:
        if (
            loop_child.label
            and loop_child.label not in script_blocks_by_label
            and loop_child.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        ):
            blocks_to_update.add(loop_child.label)

    assert "download_file" not in blocks_to_update


class TestDownloadSelectorCascade:
    """Test the download_selector cascade strategies."""

    def test_url_value_returns_href_selector(self):
        """URL values should produce a[href*=filename] selector."""
        from unittest.mock import patch

        from skyvern.core.script_generations.skyvern_page import RunContext

        rc = RunContext.__new__(RunContext)
        loop_val = {"title": "Report", "url": "https://example.com/files/report-2025.pdf"}
        with patch.object(type(rc), "loop_value", new_callable=lambda: property(lambda self: loop_val)):
            result = rc.download_selector()
        assert result == 'a[href*="report-2025.pdf"]'

    def test_text_value_returns_has_text_selector(self):
        """Non-URL text values should produce a:has-text selector with the longest text."""
        from unittest.mock import patch

        from skyvern.core.script_generations.skyvern_page import RunContext

        rc = RunContext.__new__(RunContext)
        loop_val = {"title": "Annual Report 2025", "unique_identifier": "DOC-2025-001"}
        with patch.object(type(rc), "loop_value", new_callable=lambda: property(lambda self: loop_val)):
            result = rc.download_selector()
        assert result == 'a:has-text("Annual Report 2025")'

    def test_no_value_returns_none(self):
        """Empty or None loop_value should return None."""
        from unittest.mock import patch

        from skyvern.core.script_generations.skyvern_page import RunContext

        rc = RunContext.__new__(RunContext)
        with patch.object(type(rc), "loop_value", new_callable=lambda: property(lambda self: None)):
            result = rc.download_selector()
        assert result is None

    def test_short_text_skipped(self):
        """Very short text values (< 3 chars) should be skipped."""
        from unittest.mock import patch

        from skyvern.core.script_generations.skyvern_page import RunContext

        rc = RunContext.__new__(RunContext)
        loop_val = {"id": "AB"}
        with patch.object(type(rc), "loop_value", new_callable=lambda: property(lambda self: loop_val)):
            result = rc.download_selector()
        assert result is None

    def test_url_takes_priority_over_text(self):
        """When both URL and text values exist, URL-based selector wins."""
        from unittest.mock import patch

        from skyvern.core.script_generations.skyvern_page import RunContext

        rc = RunContext.__new__(RunContext)
        loop_val = {"title": "Annual Report", "download_url": "https://example.com/report.pdf"}
        with patch.object(type(rc), "loop_value", new_callable=lambda: property(lambda self: loop_val)):
            result = rc.download_selector()
        assert result == 'a[href*="report.pdf"]'
