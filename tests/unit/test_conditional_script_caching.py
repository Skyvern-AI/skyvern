"""
Tests for conditional block script caching support.

This test file verifies that:
1. Workflows with conditional blocks can have scripts generated for cacheable blocks
2. The regeneration logic doesn't trigger unnecessary regeneration for unexecuted branches
3. Progressive caching works correctly across multiple runs
4. Cached blocks from unexecuted branches are preserved during script regeneration (SKY-7815)

Key bugs this tests against:
- Previously, the regeneration check compared cached blocks against ALL blocks in the workflow
  definition, causing "missing" blocks from unexecuted branches to trigger regeneration
  on EVERY run, flooding the database with redundant script operations.
- (SKY-7815) When regeneration was triggered for a legitimate reason, cached blocks from
  unexecuted conditional branches were DROPPED because generate_workflow_script_python_code()
  only iterated blocks from the transform output (executed blocks). This caused a regeneration
  loop where blocks kept getting dropped and re-added.
"""

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.core.script_generations.generate_script import ScriptBlockSource, generate_workflow_script_python_code
from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED
from skyvern.schemas.workflows import BlockType
from skyvern.services.workflow_script_service import workflow_has_conditionals


class TestConditionalBlockDetection:
    """Tests for workflow_has_conditionals() function."""

    def test_workflow_without_conditionals(self) -> None:
        """Workflows without conditional blocks should return False."""

        class MockBlock:
            def __init__(self, block_type: BlockType):
                self.block_type = block_type
                self.label = f"block_{block_type.value}"

        class MockWorkflowDefinition:
            def __init__(self, blocks: list):
                self.blocks = blocks

        class MockWorkflow:
            def __init__(self, blocks: list):
                self.workflow_definition = MockWorkflowDefinition(blocks)
                self.workflow_id = "test_workflow"

        # Workflow with only navigation and extraction blocks
        blocks = [
            MockBlock(BlockType.NAVIGATION),
            MockBlock(BlockType.EXTRACTION),
        ]
        workflow = MockWorkflow(blocks)

        assert workflow_has_conditionals(workflow) is False

    def test_workflow_with_conditionals(self) -> None:
        """Workflows with conditional blocks should return True."""

        class MockBlock:
            def __init__(self, block_type: BlockType):
                self.block_type = block_type
                self.label = f"block_{block_type.value}"

        class MockWorkflowDefinition:
            def __init__(self, blocks: list):
                self.blocks = blocks

        class MockWorkflow:
            def __init__(self, blocks: list):
                self.workflow_definition = MockWorkflowDefinition(blocks)
                self.workflow_id = "test_workflow"

        # Workflow with a conditional block
        blocks = [
            MockBlock(BlockType.NAVIGATION),
            MockBlock(BlockType.CONDITIONAL),
            MockBlock(BlockType.EXTRACTION),
        ]
        workflow = MockWorkflow(blocks)

        assert workflow_has_conditionals(workflow) is True


class TestConditionalBlockNotCached:
    """Tests verifying conditional blocks are not in BLOCK_TYPES_THAT_SHOULD_BE_CACHED."""

    def test_conditional_not_in_cached_types(self) -> None:
        """Conditional blocks should NOT be in the set of cacheable block types."""
        assert BlockType.CONDITIONAL not in BLOCK_TYPES_THAT_SHOULD_BE_CACHED

    def test_cacheable_types_exist(self) -> None:
        """Verify that cacheable block types exist and include expected types."""
        assert BlockType.NAVIGATION in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        assert BlockType.EXTRACTION in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        assert BlockType.TASK in BLOCK_TYPES_THAT_SHOULD_BE_CACHED


class TestRegenerationLogicForConditionals:
    """
    Tests for the regeneration decision logic when conditionals are present.

    The key fix: For workflows WITH conditionals, missing labels from unexecuted
    branches should NOT trigger regeneration. This prevents the database flooding
    bug where every run caused unnecessary script regeneration.
    """

    def test_missing_labels_computation(self) -> None:
        """
        Test that the missing labels computation works correctly.

        For a workflow with branches A and B:
        - should_cache_block_labels = {A, B, START}
        - cached_block_labels = {A, START} (only A executed)
        - missing_labels = {B}

        Without the fix: missing_labels triggers regeneration every time
        With the fix: missing_labels is ignored for workflows with conditionals
        """
        # Simulate the computation
        should_cache_block_labels = {"branch_a_extract", "branch_b_extract", "WORKFLOW_START_BLOCK"}
        cached_block_labels = {"branch_a_extract", "WORKFLOW_START_BLOCK"}

        missing_labels = should_cache_block_labels - cached_block_labels
        assert missing_labels == {"branch_b_extract"}

        # With conditionals, this should NOT trigger regeneration
        has_conditionals = True
        blocks_to_update: set[str] = set()

        if missing_labels and not has_conditionals:
            blocks_to_update.update(missing_labels)

        # blocks_to_update should be empty because we have conditionals
        assert len(blocks_to_update) == 0

    def test_regeneration_triggered_without_conditionals(self) -> None:
        """
        Without conditionals, missing labels SHOULD trigger regeneration.

        This is the expected behavior for regular workflows where all blocks
        should eventually be cached.
        """
        should_cache_block_labels = {"block_1", "block_2", "WORKFLOW_START_BLOCK"}
        cached_block_labels = {"block_1", "WORKFLOW_START_BLOCK"}

        missing_labels = should_cache_block_labels - cached_block_labels
        assert missing_labels == {"block_2"}

        # Without conditionals, this SHOULD trigger regeneration
        has_conditionals = False
        blocks_to_update: set[str] = set()

        if missing_labels and not has_conditionals:
            blocks_to_update.update(missing_labels)

        # blocks_to_update should contain missing labels
        assert "block_2" in blocks_to_update

    def test_explicit_updates_still_work_with_conditionals(self) -> None:
        """
        Even with conditionals, explicit blocks_to_update from the caller
        should still trigger regeneration.

        This ensures that actual changes to executed blocks are still processed.
        """
        blocks_to_update: set[str] = {"explicitly_updated_block"}  # From caller

        # Even with conditionals, explicit updates should trigger regeneration
        should_regenerate = bool(blocks_to_update)
        assert should_regenerate is True


class TestProgressiveCachingConcept:
    """
    Tests documenting the progressive caching concept for conditional workflows.

    Progressive caching means:
    1. Run 1 takes branch A → caches blocks from A
    2. Run 2 takes branch B → caches blocks from B (preserves A's cache)
    3. Eventually all branches have cached blocks

    The key insight is that we DON'T regenerate just because some branches
    haven't executed yet.
    """

    def test_progressive_caching_scenario(self) -> None:
        """
        Simulate multiple runs with different branches.

        Run 1: Branch A executes
        Run 2: Branch A executes (should NOT regenerate - same blocks)
        Run 3: Branch B executes (should cache B, preserve A)
        """
        # Initial state
        cached_blocks: set[str] = set()

        # Run 1: Branch A executes
        executed_blocks_run1 = {"nav_block", "branch_a_extract"}
        cached_blocks.update(executed_blocks_run1)
        assert cached_blocks == {"nav_block", "branch_a_extract"}

        # Run 2: Branch A executes again
        executed_blocks_run2 = {"nav_block", "branch_a_extract"}
        # No new blocks to cache - should NOT trigger regeneration
        new_blocks_run2 = executed_blocks_run2 - cached_blocks
        assert len(new_blocks_run2) == 0  # Nothing new to cache

        # Run 3: Branch B executes
        executed_blocks_run3 = {"nav_block", "branch_b_extract"}
        new_blocks_run3 = executed_blocks_run3 - cached_blocks
        assert new_blocks_run3 == {"branch_b_extract"}  # New block to cache

        # Cache should now have both branches
        cached_blocks.update(executed_blocks_run3)
        assert cached_blocks == {"nav_block", "branch_a_extract", "branch_b_extract"}


class TestConditionalBlockCodeGeneration:
    """Tests for conditional block handling in code generation."""

    def test_conditional_block_type_string(self) -> None:
        """Verify the conditional block type string matches expected value."""
        assert BlockType.CONDITIONAL.value == "conditional"


# ---------------------------------------------------------------------------
# SKY-7815: Tests for cached block preservation during regeneration
# ---------------------------------------------------------------------------


class TestCachedBlockPreservationDuringRegeneration:
    """
    Tests verifying that cached blocks from unexecuted conditional branches
    are preserved when generate_workflow_script_python_code() regenerates a script.

    Bug (SKY-7815):
    When a workflow has conditional branches A and B:
    - Run 1 executes branch A → script has blocks from A
    - Run 2 executes branch B → regeneration triggered → transform only returns B's blocks
    - generate_workflow_script_python_code() only iterates transform output (B's blocks)
    - Cached blocks from A are loaded into cached_blocks dict but NEVER iterated
    - Result: A's blocks are DROPPED from the new script → regeneration loop

    Fix: After processing all blocks from the transform output, iterate remaining
    cached_blocks entries and preserve them in both the DB and script output.
    """

    @pytest.mark.asyncio
    async def test_cached_blocks_from_unexecuted_branch_are_preserved(self) -> None:
        """
        Core test: when only branch B's blocks are in the transform output,
        branch A's cached blocks should still appear in the generated script.
        """
        # Branch A's cached block (from a previous run)
        branch_a_code = (
            "async def branch_a_extract(page: SkyvernPage, context: RunContext) -> None:\n"
            "    await skyvern.extract(page, \"//div[@id='result']\")\n"
        )
        cached_blocks = {
            "branch_a_extract": ScriptBlockSource(
                label="branch_a_extract",
                code=branch_a_code,
                run_signature="await branch_a_extract(page, context)",
                workflow_run_id="wr_run1",
                workflow_run_block_id="wfrb_a",
                input_fields=None,
            ),
        }

        # Transform output only has branch B's block (branch B executed this run)
        blocks = [
            {
                "block_type": "navigation",
                "label": "branch_b_navigate",
                "task_id": "task_b",
                "navigation_goal": "Go to page B",
                "url": "https://example.com/b",
                "workflow_run_id": "wr_run2",
                "workflow_run_block_id": "wfrb_b",
            },
        ]

        actions_by_task = {
            "task_b": [
                {
                    "action_type": "click",
                    "action_id": "action_b1",
                    "xpath": "//button[@id='submit']",
                    "element_id": "submit",
                    "reasoning": "Click submit",
                    "intention": "Submit the form",
                    "confidence_float": 0.95,
                    "has_mini_agent": False,
                },
            ],
        }

        workflow = {
            "workflow_id": "wf_test",
            "workflow_permanent_id": "wpid_test",
            "title": "Test Conditional Workflow",
            "workflow_definition": {
                "parameters": [
                    {"parameter_type": "workflow", "key": "url", "default_value": "https://example.com"},
                ],
            },
        }

        workflow_run_request = {
            "workflow_id": "wpid_test",
            "parameters": {"url": "https://example.com"},
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request=workflow_run_request,
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                cached_blocks=cached_blocks,
                updated_block_labels={"branch_b_navigate", "__start_block__"},
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # The output should contain branch A's cached code
            assert "branch_a_extract" in result, (
                "Cached block from unexecuted branch A should be preserved in the script output"
            )

            # Verify create_or_update_script_block was called for the preserved block
            preserved_calls = [
                call
                for call in mock_create_block.call_args_list
                if call.kwargs.get("block_label") == "branch_a_extract"
            ]
            assert len(preserved_calls) == 1, (
                "create_or_update_script_block should be called for the preserved cached block"
            )
            preserved_call = preserved_calls[0]
            assert preserved_call.kwargs["run_signature"] == "await branch_a_extract(page, context)"
            assert preserved_call.kwargs["workflow_run_id"] == "wr_run1"

    @pytest.mark.asyncio
    async def test_cached_blocks_without_run_signature_are_not_preserved(self) -> None:
        """Cached blocks without a run_signature should NOT be preserved."""
        cached_blocks = {
            "incomplete_block": ScriptBlockSource(
                label="incomplete_block",
                code="async def incomplete_block(): pass\n",
                run_signature=None,  # No run_signature
                workflow_run_id="wr_old",
                workflow_run_block_id="wfrb_old",
                input_fields=None,
            ),
        }

        blocks: list = []
        actions_by_task: dict = {}
        workflow = {
            "workflow_id": "wf_test",
            "title": "Test",
            "workflow_definition": {"parameters": []},
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                cached_blocks=cached_blocks,
                updated_block_labels={"__start_block__"},
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # Incomplete block should NOT appear in the output
            assert "incomplete_block" not in result

            # create_or_update_script_block should NOT be called for incomplete block
            incomplete_calls = [
                call
                for call in mock_create_block.call_args_list
                if call.kwargs.get("block_label") == "incomplete_block"
            ]
            assert len(incomplete_calls) == 0

    @pytest.mark.asyncio
    async def test_cached_blocks_without_code_are_not_preserved(self) -> None:
        """Cached blocks without code should NOT be preserved."""
        cached_blocks = {
            "empty_block": ScriptBlockSource(
                label="empty_block",
                code="",  # Empty code
                run_signature="await empty_block(page, context)",
                workflow_run_id="wr_old",
                workflow_run_block_id="wfrb_old",
                input_fields=None,
            ),
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow={
                    "workflow_id": "wf_test",
                    "title": "Test",
                    "workflow_definition": {"parameters": []},
                },
                blocks=[],
                actions_by_task={},
                cached_blocks=cached_blocks,
                updated_block_labels={"__start_block__"},
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # Empty block should NOT appear
            empty_calls = [
                call for call in mock_create_block.call_args_list if call.kwargs.get("block_label") == "empty_block"
            ]
            assert len(empty_calls) == 0

    @pytest.mark.asyncio
    async def test_already_processed_blocks_are_not_duplicated(self) -> None:
        """
        Blocks that appear in both the transform output AND cached_blocks
        should NOT be duplicated. The transform output processing handles them.
        """
        block_code = (
            "async def shared_block(page: SkyvernPage, context: RunContext) -> None:\n"
            '    await skyvern.click(page, "//button")\n'
        )
        cached_blocks = {
            "shared_block": ScriptBlockSource(
                label="shared_block",
                code=block_code,
                run_signature="await shared_block(page, context)",
                workflow_run_id="wr_run1",
                workflow_run_block_id="wfrb_shared",
                input_fields=None,
            ),
        }

        # Same block also appears in the transform output (it executed this run too)
        blocks = [
            {
                "block_type": "navigation",
                "label": "shared_block",
                "task_id": "task_shared",
                "navigation_goal": "Navigate somewhere",
                "url": "https://example.com",
                "workflow_run_id": "wr_run2",
                "workflow_run_block_id": "wfrb_shared_run2",
            },
        ]

        actions_by_task = {
            "task_shared": [
                {
                    "action_type": "click",
                    "action_id": "action_1",
                    "xpath": "//button",
                    "element_id": "btn",
                    "reasoning": "Click",
                    "intention": "Click",
                    "confidence_float": 0.9,
                    "has_mini_agent": False,
                },
            ],
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow={
                    "workflow_id": "wf_test",
                    "title": "Test",
                    "workflow_definition": {"parameters": []},
                },
                blocks=blocks,
                actions_by_task=actions_by_task,
                cached_blocks=cached_blocks,
                updated_block_labels={"shared_block", "__start_block__"},
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # The block should appear exactly once (from the transform output processing,
            # NOT duplicated by the preservation loop)
            shared_calls = [
                call for call in mock_create_block.call_args_list if call.kwargs.get("block_label") == "shared_block"
            ]
            # Should be called once from the normal task_v1 processing, NOT again from preservation
            assert len(shared_calls) == 1

    @pytest.mark.asyncio
    async def test_multiple_unexecuted_branches_all_preserved(self) -> None:
        """
        When a workflow has 3 conditional branches and only 1 executes,
        cached blocks from the other 2 branches should ALL be preserved.
        """

        def _make_cached_block(label: str) -> ScriptBlockSource:
            return ScriptBlockSource(
                label=label,
                code=f"async def {label}(page: SkyvernPage, context: RunContext) -> None:\n    pass\n",
                run_signature=f"await {label}(page, context)",
                workflow_run_id="wr_old",
                workflow_run_block_id=f"wfrb_{label}",
                input_fields=None,
            )

        cached_blocks = {
            "branch_a_extract": _make_cached_block("branch_a_extract"),
            "branch_b_navigate": _make_cached_block("branch_b_navigate"),
            # branch_c executed this run, so it's also in blocks below
        }

        # Only branch C's block is in the transform output
        blocks = [
            {
                "block_type": "extraction",
                "label": "branch_c_extract",
                "task_id": "task_c",
                "data_extraction_goal": "Extract C data",
                "workflow_run_id": "wr_run3",
                "workflow_run_block_id": "wfrb_c",
            },
        ]

        actions_by_task = {
            "task_c": [
                {
                    "action_type": "extract",
                    "action_id": "action_c1",
                    "xpath": "//div[@class='data']",
                    "element_id": "data",
                    "reasoning": "Extract",
                    "intention": "Extract data",
                    "confidence_float": 0.9,
                    "has_mini_agent": False,
                    "data_extraction_goal": "Extract C data",
                },
            ],
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow={
                    "workflow_id": "wf_test",
                    "title": "Test",
                    "workflow_definition": {"parameters": []},
                },
                blocks=blocks,
                actions_by_task=actions_by_task,
                cached_blocks=cached_blocks,
                updated_block_labels={"branch_c_extract", "__start_block__"},
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # Both branch A and B should be preserved
            assert "branch_a_extract" in result, "Branch A cached block should be preserved"
            assert "branch_b_navigate" in result, "Branch B cached block should be preserved"
            assert "branch_c_extract" in result, "Branch C (executed) block should be present"

            # Verify DB entries were created for all 3 blocks + __start_block__
            all_labels = {call.kwargs.get("block_label") for call in mock_create_block.call_args_list}
            assert "branch_a_extract" in all_labels
            assert "branch_b_navigate" in all_labels
            assert "branch_c_extract" in all_labels
            assert "__start_block__" in all_labels

    @pytest.mark.asyncio
    async def test_preservation_without_script_context(self) -> None:
        """
        When script_id/script_revision_id/organization_id are not provided,
        cached blocks should still be added to the script output (just no DB calls).
        """
        branch_a_code = "async def branch_a(page: SkyvernPage, context: RunContext) -> None:\n    pass\n"
        cached_blocks = {
            "branch_a": ScriptBlockSource(
                label="branch_a",
                code=branch_a_code,
                run_signature="await branch_a(page, context)",
                workflow_run_id="wr_old",
                workflow_run_block_id="wfrb_a",
                input_fields=None,
            ),
        }

        with (
            patch(
                "skyvern.core.script_generations.generate_script.generate_workflow_parameters_schema",
                new_callable=AsyncMock,
                return_value=("", {}),
            ),
            patch(
                "skyvern.core.script_generations.generate_script.create_or_update_script_block",
                new_callable=AsyncMock,
            ) as mock_create_block,
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow={
                    "workflow_id": "wf_test",
                    "title": "Test",
                    "workflow_definition": {"parameters": []},
                },
                blocks=[],
                actions_by_task={},
                cached_blocks=cached_blocks,
                updated_block_labels={"__start_block__"},
                # No script context
                script_id=None,
                script_revision_id=None,
                organization_id=None,
            )

            # Code should still be in the output
            assert "branch_a" in result

            # But no DB calls should be made for preserved blocks
            preserved_calls = [
                call for call in mock_create_block.call_args_list if call.kwargs.get("block_label") == "branch_a"
            ]
            assert len(preserved_calls) == 0


class TestRegenerationLoopPrevention:
    """
    End-to-end tests for the regeneration loop prevention (SKY-7815).

    The regeneration loop happens when:
    1. Workflow has conditional branches A and B
    2. Run 1 caches branch A → script has A's blocks
    3. Run 2 executes branch B → triggers regeneration for B
    4. During regeneration, transform only returns B's blocks
    5. A's cached blocks are dropped from the new script
    6. Run 3 executes branch A → A is "missing" → triggers regeneration
    7. During regeneration, B's cached blocks are dropped → loop continues

    The fix has two parts:
    1. generate_script_if_needed: Don't add missing labels for conditional workflows
    2. generate_workflow_script_python_code: Preserve cached blocks from unexecuted branches
    """

    def test_regeneration_loop_scenario_is_prevented(self) -> None:
        """
        Simulate the full regeneration loop scenario and verify it's prevented.

        This test verifies both parts of the fix working together:
        - Missing labels don't trigger regeneration for conditional workflows
        - Even if regeneration IS triggered (for other reasons), cached blocks are preserved
        """
        # --- Part 1: generate_script_if_needed logic ---
        # Workflow definition has blocks: nav, branch_a_extract, branch_b_extract
        should_cache_block_labels = {"nav_block", "branch_a_extract", "branch_b_extract", "__start_block__"}

        # After Run 1: only nav and branch_a are cached
        cached_block_labels = {"nav_block", "branch_a_extract", "__start_block__"}

        missing_labels = should_cache_block_labels - cached_block_labels
        assert missing_labels == {"branch_b_extract"}

        has_conditionals = True
        blocks_to_update: set[str] = set()

        # With conditionals, missing labels should NOT be added
        if missing_labels and not has_conditionals:
            blocks_to_update.update(missing_labels)
        elif missing_labels and has_conditionals:
            pass  # Skip - expected for conditional workflows

        # No regeneration needed just because of missing labels
        assert len(blocks_to_update) == 0

        # --- Part 2: Even if regeneration IS triggered ---
        # e.g., branch B executed this run and needs caching
        blocks_to_update.add("branch_b_extract")

        # The transform output only has branch B's block
        transform_output_labels = {"nav_block", "branch_b_extract"}

        # cached_blocks from old script has branch A's data
        old_cached_block_labels = {"nav_block", "branch_a_extract"}

        # After the fix, the preservation loop handles blocks NOT in transform output
        processed_by_transform = transform_output_labels
        preserved_from_cache = old_cached_block_labels - processed_by_transform

        assert preserved_from_cache == {"branch_a_extract"}, (
            "Branch A's block should be preserved even though it wasn't in the transform output"
        )

        # Final result should have ALL blocks
        final_blocks = transform_output_labels | preserved_from_cache
        assert final_blocks == {"nav_block", "branch_a_extract", "branch_b_extract"}

    def test_no_regeneration_loop_across_three_runs(self) -> None:
        """
        Simulate 3 runs and verify no regeneration loop occurs.

        Run 1: Branch A → cache A
        Run 2: Branch B → regenerate (B is new) → A is preserved
        Run 3: Branch A → no regeneration needed (A is still cached)
        """
        # --- Run 1: Branch A executes ---
        cached_blocks_after_run1 = {"nav_block", "branch_a_extract", "__start_block__"}

        # --- Run 2: Branch B executes ---
        has_conditionals = True
        should_cache = {"nav_block", "branch_a_extract", "branch_b_extract", "__start_block__"}

        missing_run2 = should_cache - cached_blocks_after_run1
        assert missing_run2 == {"branch_b_extract"}

        blocks_to_update_run2: set[str] = set()
        # Missing labels NOT added for conditional workflows
        if missing_run2 and not has_conditionals:
            blocks_to_update_run2.update(missing_run2)

        # branch_b_extract is added because it actually executed
        blocks_to_update_run2.add("branch_b_extract")

        # Regeneration happens, but branch A is PRESERVED
        transform_output_run2 = {"nav_block", "branch_b_extract"}
        preserved_run2 = {"branch_a_extract"}  # From cache, not in transform

        cached_blocks_after_run2 = transform_output_run2 | preserved_run2 | {"__start_block__"}
        assert cached_blocks_after_run2 == {"nav_block", "branch_a_extract", "branch_b_extract", "__start_block__"}

        # --- Run 3: Branch A executes again ---
        missing_run3 = should_cache - cached_blocks_after_run2
        assert len(missing_run3) == 0, "No missing blocks after Run 2 because branch A was preserved"

        blocks_to_update_run3: set[str] = set()
        if missing_run3 and not has_conditionals:
            blocks_to_update_run3.update(missing_run3)

        # branch_a_extract already has cached code, so it's NOT added to blocks_to_update
        # (execution tracking only adds blocks that DON'T have cached code)
        should_regenerate_run3 = bool(blocks_to_update_run3)
        assert should_regenerate_run3 is False, "No regeneration needed on Run 3 - the loop is broken"
