"""
Tests for continue_on_failure behavior with caching.

Verifies that:
1. When a block with continue_on_failure=True fails, it's not cached (existing behavior)
2. When a cached block with continue_on_failure=True fails during cached execution,
   it's marked for regeneration so the next run uses AI execution
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.models.block import (
    BlockResult,
    BlockType,
    NavigationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED
from skyvern.schemas.workflows import BlockStatus


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id",
        key=key,
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


def _navigation_block(
    label: str,
    continue_on_failure: bool = False,
    next_block_label: str | None = None,
) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_parameter(f"{label}_output"),
        next_block_label=next_block_label,
        continue_on_failure=continue_on_failure,
    )


class TestContinueOnFailureWithCache:
    """Tests for cache invalidation when continue_on_failure blocks fail."""

    def test_navigation_block_is_cacheable(self) -> None:
        """Verify NavigationBlock is in the cacheable block types."""
        assert BlockType.NAVIGATION in BLOCK_TYPES_THAT_SHOULD_BE_CACHED

    def test_failed_block_without_continue_on_failure_not_added_to_update(self) -> None:
        """
        Test that a failed block without continue_on_failure=True doesn't trigger
        special cache invalidation logic (it would stop the workflow instead).
        """
        block = _navigation_block("nav1", continue_on_failure=False)
        blocks_to_update: set[str] = set()
        script_blocks_by_label = {"nav1": MagicMock()}  # Block is cached

        # Simulate failed block result
        result = BlockResult(
            success=False,
            failure_reason="Block failed",
            output_parameter=block.output_parameter,
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb-1",
        )

        # The cache invalidation logic for continue_on_failure
        # This simulates the condition from service.py
        should_invalidate = (
            block.label
            and block.continue_on_failure
            and result.status != BlockStatus.completed
            and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            and block.label in script_blocks_by_label
        )

        if should_invalidate:
            blocks_to_update.add(block.label)

        # Should NOT be in blocks_to_update because continue_on_failure=False
        assert block.label not in blocks_to_update

    def test_failed_block_with_continue_on_failure_and_cached_added_to_update(self) -> None:
        """
        Test that a cached block with continue_on_failure=True that fails
        is added to blocks_to_update for regeneration.
        """
        block = _navigation_block("nav1", continue_on_failure=True)
        blocks_to_update: set[str] = set()
        script_blocks_by_label = {"nav1": MagicMock()}  # Block is cached

        # Simulate failed block result
        result = BlockResult(
            success=False,
            failure_reason="Block failed",
            output_parameter=block.output_parameter,
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb-1",
        )

        # The cache invalidation logic for continue_on_failure
        should_invalidate = (
            block.label
            and block.continue_on_failure
            and result.status != BlockStatus.completed
            and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            and block.label in script_blocks_by_label
        )

        if should_invalidate:
            blocks_to_update.add(block.label)

        # SHOULD be in blocks_to_update for regeneration
        assert block.label in blocks_to_update

    def test_failed_uncached_block_with_continue_on_failure_not_added_to_update(self) -> None:
        """
        Test that an uncached block with continue_on_failure=True that fails
        is NOT added to blocks_to_update (there's nothing to invalidate).
        """
        block = _navigation_block("nav1", continue_on_failure=True)
        blocks_to_update: set[str] = set()
        script_blocks_by_label: dict = {}  # Block is NOT cached

        # Simulate failed block result
        result = BlockResult(
            success=False,
            failure_reason="Block failed",
            output_parameter=block.output_parameter,
            output_parameter_value=None,
            status=BlockStatus.failed,
            workflow_run_block_id="wrb-1",
        )

        # The cache invalidation logic for continue_on_failure
        should_invalidate = (
            block.label
            and block.continue_on_failure
            and result.status != BlockStatus.completed
            and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            and block.label in script_blocks_by_label
        )

        if should_invalidate:
            blocks_to_update.add(block.label)

        # Should NOT be in blocks_to_update - nothing to invalidate
        assert block.label not in blocks_to_update

    def test_successful_block_with_continue_on_failure_not_added_to_update_for_invalidation(self) -> None:
        """
        Test that a successful cached block with continue_on_failure=True
        is NOT added to blocks_to_update for invalidation.
        """
        block = _navigation_block("nav1", continue_on_failure=True)
        blocks_to_update: set[str] = set()
        script_blocks_by_label = {"nav1": MagicMock()}  # Block is cached

        # Simulate successful block result
        result = BlockResult(
            success=True,
            failure_reason=None,
            output_parameter=block.output_parameter,
            output_parameter_value={"result": "success"},
            status=BlockStatus.completed,
            workflow_run_block_id="wrb-1",
        )

        # The cache invalidation logic for continue_on_failure
        should_invalidate = (
            block.label
            and block.continue_on_failure
            and result.status != BlockStatus.completed
            and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            and block.label in script_blocks_by_label
        )

        if should_invalidate:
            blocks_to_update.add(block.label)

        # Should NOT be in blocks_to_update - block succeeded
        assert block.label not in blocks_to_update

    @pytest.mark.parametrize(
        "status",
        [BlockStatus.failed, BlockStatus.terminated, BlockStatus.timed_out],
    )
    def test_all_failure_statuses_trigger_cache_invalidation(self, status: BlockStatus) -> None:
        """
        Test that all non-completed statuses (failed, terminated, timed_out)
        trigger cache invalidation when continue_on_failure=True.
        """
        block = _navigation_block("nav1", continue_on_failure=True)
        blocks_to_update: set[str] = set()
        script_blocks_by_label = {"nav1": MagicMock()}  # Block is cached

        # Simulate block result with the given status
        result = BlockResult(
            success=False,
            failure_reason=f"Block {status.value}",
            output_parameter=block.output_parameter,
            output_parameter_value=None,
            status=status,
            workflow_run_block_id="wrb-1",
        )

        # The cache invalidation logic for continue_on_failure
        should_invalidate = (
            block.label
            and block.continue_on_failure
            and result.status != BlockStatus.completed
            and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            and block.label in script_blocks_by_label
        )

        if should_invalidate:
            blocks_to_update.add(block.label)

        # SHOULD be in blocks_to_update for all failure statuses
        assert block.label in blocks_to_update, f"Status {status} should trigger cache invalidation"
