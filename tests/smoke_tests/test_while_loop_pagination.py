from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import Block, WhileLoopBlock
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import (
    ActionBlockYAML,
    BlockResult,
    BlockStatus,
    BranchCriteriaYAML,
    ExtractionBlockYAML,
    WhileLoopBlockYAML,
    WorkflowDefinitionYAML,
)
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext


class SmokeWorkflowRunContext(FakeWorkflowRunContext):
    def get_value(self, key: str) -> Any:
        return self.values[key]

    async def register_output_parameter_value_post_execution(self, parameter: Any, value: Any) -> None:
        self.set_value(parameter.key, value)
        if not parameter.key.endswith("_output"):
            return

        block_label = parameter.key.removesuffix("_output")
        self.set_value(block_label, dict(value) if isinstance(value, dict) else value)
        self.workflow_run_outputs[block_label] = value


def _make_pagination_while_loop() -> WhileLoopBlock:
    workflow_definition = WorkflowDefinitionYAML(
        parameters=[],
        blocks=[
            WhileLoopBlockYAML(
                label="paginate_results",
                condition=BranchCriteriaYAML(
                    criteria_type="jinja2_template",
                    expression="{{ current_index == 0 or extract_page.has_next_page }}",
                ),
                loop_blocks=[
                    ExtractionBlockYAML(
                        label="extract_page",
                        next_block_label="click_next",
                        data_extraction_goal="Extract visible rows and whether another page is available",
                        data_schema={
                            "type": "object",
                            "properties": {
                                "rows": {"type": "array", "items": {"type": "object"}},
                                "has_next_page": {"type": "boolean"},
                            },
                        },
                    ),
                    ActionBlockYAML(
                        label="click_next",
                        navigation_goal="Click the Next button if it is enabled",
                    ),
                ],
            )
        ],
    )

    converted = convert_workflow_definition(workflow_definition, workflow_id="wf_smoke")
    loop_block = converted.blocks[0]
    assert isinstance(loop_block, WhileLoopBlock)
    return loop_block


@pytest.mark.asyncio
async def test_while_loop_paginates_until_next_is_unavailable() -> None:
    loop_block = _make_pagination_while_loop()
    workflow_run_context = SmokeWorkflowRunContext(values={})
    pages = [
        {"rows": [{"name": "Alpha"}, {"name": "Beta"}], "has_next_page": True},
        {"rows": [{"name": "Gamma"}], "has_next_page": False},
    ]
    current_page = 0
    executed_steps: list[tuple[str, int]] = []

    async def fake_execute_safe(block: Block, **kwargs: Any) -> BlockResult:
        nonlocal current_page

        loop_index = kwargs["current_index"]
        executed_steps.append((block.label, loop_index))

        if block.label == "extract_page":
            output_value = pages[current_page]
        elif block.label == "click_next":
            output_value = {"clicked": pages[current_page]["has_next_page"]}
            if pages[current_page]["has_next_page"]:
                current_page += 1
        else:
            raise AssertionError(f"Unexpected block executed: {block.label}")

        await workflow_run_context.register_output_parameter_value_post_execution(block.output_parameter, output_value)
        return BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            output_parameter_value=output_value,
            status=BlockStatus.completed,
            workflow_run_block_id=f"wrb_{block.label}_{loop_index}",
        )

    mock_app = MagicMock()
    mock_skyvern_ctx = MagicMock()
    with (
        patch.object(Block, "execute_safe", new=fake_execute_safe),
        patch.dict(block_module.__dict__, {"app": mock_app, "skyvern_context": mock_skyvern_ctx}),
    ):
        mock_skyvern_ctx.current.return_value = None
        mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
        mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

        result = await loop_block._execute_while_loop_helper(
            workflow_run_id="wr_smoke",
            workflow_run_block_id="wrb_paginate_results",
            workflow_run_context=workflow_run_context,
            organization_id="org_smoke",
        )

    assert executed_steps == [
        ("extract_page", 0),
        ("click_next", 0),
        ("extract_page", 1),
        ("click_next", 1),
    ]
    assert result.natural_completion is True
    assert len(result.outputs_with_loop_values) == 2
    assert len(result.block_outputs) == 4
    assert all(block_output.status == BlockStatus.completed for block_output in result.block_outputs)
    assert workflow_run_context.values["extract_page"]["rows"] == [{"name": "Gamma"}]
    assert workflow_run_context.values["extract_page"]["has_next_page"] is False
