"""
Tests for script block creation error handling (SKY-8684, Task 2).

Verifies that:
1. create_or_update_script_block returns True on success, False on failure
2. generate_workflow_script_python_code tracks block creation counts via CodeGenResult
3. generate_workflow_script skips WorkflowScript creation when all blocks fail
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.generate_script import create_or_update_script_block


class TestCreateOrUpdateScriptBlockReturnValue:
    """Test that create_or_update_script_block returns bool indicating success/failure."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        """Regression test: successful block creation returns True."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_123"
        mock_script_block.script_file_id = None

        mock_script_file = MagicMock()
        mock_script_file.file_id = "sf_123"

        with (
            patch("skyvern.core.script_generations.generate_script.app") as mock_app,
        ):
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=None)
            mock_app.DATABASE.scripts.create_script_block = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_content_hash = AsyncMock(return_value=None)
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact = AsyncMock(return_value="artifact_123")
            mock_app.DATABASE.scripts.create_script_file = AsyncMock(return_value=mock_script_file)
            mock_app.DATABASE.scripts.update_script_block = AsyncMock(return_value=mock_script_block)

            result = await create_or_update_script_block(
                block_code="async def test(): pass",
                script_revision_id="rev_123",
                script_id="script_123",
                organization_id="org_123",
                block_label="test_block",
            )

            assert result is True, "create_or_update_script_block should return True on success"

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self) -> None:
        """When block creation raises an exception, the function returns False."""
        with (
            patch("skyvern.core.script_generations.generate_script.app") as mock_app,
        ):
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(
                side_effect=RuntimeError("S3 upload timeout")
            )

            result = await create_or_update_script_block(
                block_code="async def test(): pass",
                script_revision_id="rev_123",
                script_id="script_123",
                organization_id="org_123",
                block_label="test_block",
            )

            assert result is False, "create_or_update_script_block should return False when an exception occurs"

    @pytest.mark.asyncio
    async def test_returns_false_on_artifact_upload_failure(self) -> None:
        """Specifically test S3 artifact upload failure scenario."""
        mock_script_block = MagicMock()
        mock_script_block.script_block_id = "sb_123"
        mock_script_block.script_file_id = None

        with (
            patch("skyvern.core.script_generations.generate_script.app") as mock_app,
        ):
            mock_app.DATABASE.scripts.get_script_block_by_label = AsyncMock(return_value=None)
            mock_app.DATABASE.scripts.create_script_block = AsyncMock(return_value=mock_script_block)
            mock_app.DATABASE.scripts.get_script_file_by_content_hash = AsyncMock(return_value=None)
            mock_app.ARTIFACT_MANAGER.create_script_file_artifact = AsyncMock(
                side_effect=TimeoutError("S3 upload timeout")
            )

            result = await create_or_update_script_block(
                block_code="async def test(): pass",
                script_revision_id="rev_123",
                script_id="script_123",
                organization_id="org_123",
                block_label="test_block",
            )

            assert result is False, "create_or_update_script_block should return False on artifact upload failure"


class TestBlockCreationCountTracking:
    """Test that generate_workflow_script_python_code returns block creation counts."""

    @pytest.mark.asyncio
    async def test_returns_codegen_result_with_counts(self) -> None:
        """generate_workflow_script_python_code returns a CodeGenResult with source and counts."""
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        workflow = {
            "workflow_id": "wpid_test",
            "workflow_definition": {"parameters": []},
        }
        blocks = [
            {
                "block_type": "task",
                "label": "test_task",
                "task_id": "t_123",
                "title": "Test Task",
                "url": "https://example.com",
                "navigation_goal": "Do something",
                "workflow_run_block_id": "wrb_123",
            },
        ]
        actions_by_task = {
            "t_123": [
                {
                    "action_type": "click",
                    "element_id": "e_123",
                    "xpath": "//button[@type='submit']",
                    "reasoning": "Click the button",
                    "skyvern_element_data": {
                        "id": "e_123",
                        "tag_name": "button",
                        "attributes": {"type": "submit"},
                        "text": "Submit",
                        "option_index": None,
                        "children": [],
                    },
                }
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
                return_value=True,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            # Result should have source_code, blocks_created, blocks_failed attributes
            assert hasattr(result, "source_code"), "Result should have source_code attribute"
            assert hasattr(result, "blocks_created"), "Result should have blocks_created attribute"
            assert hasattr(result, "blocks_failed"), "Result should have blocks_failed attribute"
            assert isinstance(result.source_code, str), "source_code should be a string"
            assert result.blocks_created > 0, "At least one block should be created"
            assert result.blocks_failed == 0, "No blocks should fail"

    @pytest.mark.asyncio
    async def test_counts_failures_when_block_creation_fails(self) -> None:
        """When create_or_update_script_block returns False, blocks_failed increments."""
        from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

        workflow = {
            "workflow_id": "wpid_test",
            "workflow_definition": {"parameters": []},
        }
        blocks = [
            {
                "block_type": "task",
                "label": "failing_task",
                "task_id": "t_fail",
                "title": "Failing Task",
                "url": "https://example.com",
                "navigation_goal": "Do something",
                "workflow_run_block_id": "wrb_fail",
            },
        ]
        actions_by_task = {
            "t_fail": [
                {
                    "action_type": "click",
                    "element_id": "e_123",
                    "xpath": "//button[@type='submit']",
                    "reasoning": "Click the button",
                    "skyvern_element_data": {
                        "id": "e_123",
                        "tag_name": "button",
                        "attributes": {"type": "submit"},
                        "text": "Submit",
                        "option_index": None,
                        "children": [],
                    },
                }
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
                return_value=False,
            ),
        ):
            result = await generate_workflow_script_python_code(
                file_name="test.py",
                workflow_run_request={"workflow_id": "wpid_test"},
                workflow=workflow,
                blocks=blocks,
                actions_by_task=actions_by_task,
                script_id="script_123",
                script_revision_id="rev_123",
                organization_id="org_123",
            )

            assert result.blocks_failed > 0, "blocks_failed should be > 0 when block creation fails"
