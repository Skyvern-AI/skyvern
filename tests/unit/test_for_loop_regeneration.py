"""Tests for FOR_LOOP exclusion from continue_on_failure cache invalidation (SKY-8554).

Validates that:
1. FOR_LOOP blocks are in WRAPPER_BLOCK_TYPES (excluded from invalidation)
2. FOR_LOOP blocks remain in BLOCK_TYPES_THAT_SHOULD_BE_CACHED (still cacheable)
3. TASK blocks are NOT in WRAPPER_BLOCK_TYPES (still invalidated on failure)
"""

from skyvern.forge.sdk.workflow.models.block import BlockType


class TestWrapperBlockTypes:
    """Tests for WRAPPER_BLOCK_TYPES constant."""

    def test_for_loop_in_wrapper_block_types(self) -> None:
        """FOR_LOOP should be excluded from continue_on_failure invalidation."""
        from skyvern.forge.sdk.workflow.service import WRAPPER_BLOCK_TYPES

        assert BlockType.FOR_LOOP in WRAPPER_BLOCK_TYPES

    def test_for_loop_still_cacheable(self) -> None:
        """FOR_LOOP should remain in BLOCK_TYPES_THAT_SHOULD_BE_CACHED."""
        from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED

        assert BlockType.FOR_LOOP in BLOCK_TYPES_THAT_SHOULD_BE_CACHED

    def test_task_not_in_wrapper_block_types(self) -> None:
        """TASK blocks should NOT be in WRAPPER_BLOCK_TYPES — they need invalidation."""
        from skyvern.forge.sdk.workflow.service import WRAPPER_BLOCK_TYPES

        assert BlockType.TASK not in WRAPPER_BLOCK_TYPES

    def test_task_v2_not_in_wrapper_block_types(self) -> None:
        """TaskV2 blocks should NOT be in WRAPPER_BLOCK_TYPES."""
        from skyvern.forge.sdk.workflow.service import WRAPPER_BLOCK_TYPES

        assert BlockType.TaskV2 not in WRAPPER_BLOCK_TYPES
