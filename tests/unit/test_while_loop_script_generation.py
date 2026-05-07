"""Unit tests for WhileLoop script generation and caching (SKY-8771 / #10624)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import (
    _build_while_loop_statement,
    _is_inline_only_loop_cached_code,
    _is_while_loop_cached_code,
)
from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED
from skyvern.schemas.workflows import BlockType


class TestWhileLoopBranchCriteriaRehydration:
    def test_branch_criteria_jinja_defaults_and_prompt(self) -> None:
        from skyvern.forge.sdk.workflow.models.block import JinjaBranchCriteria, PromptBranchCriteria
        from skyvern.services.script_service import _while_loop_branch_criteria

        assert isinstance(_while_loop_branch_criteria("{{ x }}", None), JinjaBranchCriteria)
        assert isinstance(_while_loop_branch_criteria("{{ x }}", "jinja2_template"), JinjaBranchCriteria)
        p = _while_loop_branch_criteria("still has bills on this page", "prompt")
        assert isinstance(p, PromptBranchCriteria)
        assert p.expression == "still has bills on this page"

    def test_branch_criteria_rejects_unknown_type(self) -> None:
        from skyvern.services.script_service import _while_loop_branch_criteria

        with pytest.raises(ValueError, match="unsupported criteria_type"):
            _while_loop_branch_criteria("x", "wat")


class TestWhileLoopConditionCriteriaTypeCodegen:
    def test_codegen_rejects_unknown_criteria_type(self) -> None:
        from skyvern.core.script_generations.generate_script import _while_loop_condition_criteria_type

        with pytest.raises(ValueError, match="unsupported condition criteria_type"):
            _while_loop_condition_criteria_type(
                {"condition": {"criteria_type": "future_type", "expression": "x"}, "block_type": "while_loop"}
            )


class TestWhileLoopInCacheableBlocks:
    def test_while_loop_in_block_types_that_should_be_cached(self) -> None:
        assert BlockType.WHILE_LOOP in BLOCK_TYPES_THAT_SHOULD_BE_CACHED


class TestWhileLoopScriptGeneration:
    def test_build_while_loop_statement_structure(self) -> None:
        block: dict[str, Any] = {
            "block_type": "while_loop",
            "label": "paginate",
            "condition": {"criteria_type": "jinja2_template", "expression": "{{ current_index == 0 or has_next }}"},
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "extract_page",
                    "data_extraction_goal": "Extract rows",
                },
            ],
        }
        result = _build_while_loop_statement("paginate", block)
        assert hasattr(result, "target")
        assert hasattr(result, "iter")
        code = cst.Module(body=[result]).code
        assert "skyvern.while_loop" in code
        assert "condition" in code
        assert "criteria_type" in code
        assert "'jinja2_template'" in code or '"jinja2_template"' in code
        assert "label" in code

    def test_build_while_loop_statement_prompt_criteria_type(self) -> None:
        block: dict[str, Any] = {
            "block_type": "while_loop",
            "label": "paginate",
            "condition": {"criteria_type": "prompt", "expression": "there is a next page of bills"},
            "loop_blocks": [],
        }
        result = _build_while_loop_statement("paginate", block)
        code = cst.Module(body=[result]).code
        assert "'prompt'" in code or '"prompt"' in code


class TestWhileLoopCachedCodeDetection:
    def test_is_while_loop_cached_code_positive(self) -> None:
        code = "async for current_value in skyvern.while_loop(condition='{{ x }}', label='w'):\n    pass\n"
        assert _is_while_loop_cached_code(code) is True

    def test_is_inline_only_loop_cached_code_unions_for_and_while(self) -> None:
        assert _is_inline_only_loop_cached_code("async for x in skyvern.loop(values='', label='a'):") is True
        assert (
            _is_inline_only_loop_cached_code(
                "async for current_value in skyvern.while_loop(condition='{{ 1 }}', label='b'):"
            )
            is True
        )
        assert _is_inline_only_loop_cached_code("def foo():\n    return 1\n") is False


class TestWhileLoopTransformation:
    def test_while_child_blocks_identified_by_parent_id(self) -> None:
        while_block = MagicMock()
        while_block.workflow_run_block_id = "wfrb_while_1"
        while_block.parent_workflow_run_block_id = None
        while_block.block_type = BlockType.WHILE_LOOP
        while_block.label = "wloop"

        child = MagicMock()
        child.parent_workflow_run_block_id = "wfrb_while_1"
        child.label = "inner_extraction"
        child.block_type = "extraction"
        child.task_id = "t1"

        children = [
            b for b in [while_block, child] if b.parent_workflow_run_block_id == while_block.workflow_run_block_id
        ]
        assert len(children) == 1
        assert children[0].task_id == "t1"


@pytest.mark.asyncio
async def test_transform_whileloop_block_with_mocked_db() -> None:
    mock_workflow_run_resp = MagicMock()
    mock_workflow_run_resp.run_request = MagicMock()
    mock_workflow_run_resp.run_request.workflow_id = "wpid_w"
    mock_workflow_run_resp.run_request.model_dump = MagicMock(return_value={"workflow_id": "wpid_w", "parameters": {}})

    mock_while_def = MagicMock()
    mock_while_def.block_type = BlockType.WHILE_LOOP
    mock_while_def.label = "wloop"
    mock_while_def.model_dump = MagicMock(
        return_value={
            "block_type": "while_loop",
            "label": "wloop",
            "condition": {"criteria_type": "jinja2_template", "expression": "{{ ok }}"},
            "loop_blocks": [
                {"block_type": "extraction", "label": "extract_data", "data_extraction_goal": "Get data"},
            ],
        }
    )

    mock_workflow = MagicMock()
    mock_workflow.model_dump = MagicMock(return_value={"workflow_id": "wf_w", "workflow_definition": {"blocks": []}})
    mock_workflow.workflow_definition.blocks = [mock_while_def]

    mock_while_run = MagicMock()
    mock_while_run.workflow_run_block_id = "wfrb_while_parent"
    mock_while_run.parent_workflow_run_block_id = None
    mock_while_run.block_type = BlockType.WHILE_LOOP
    mock_while_run.label = "wloop"
    mock_while_run.task_id = None
    mock_while_run.created_at = 1

    mock_child = MagicMock()
    mock_child.workflow_run_block_id = "wfrb_child"
    mock_child.parent_workflow_run_block_id = "wfrb_while_parent"
    mock_child.block_type = "extraction"
    mock_child.label = "extract_data"
    mock_child.task_id = "task_w_1"
    mock_child.status = "completed"
    mock_child.output = {"extracted": "x"}
    mock_child.created_at = 2

    mock_task = MagicMock()
    mock_task.model_dump = MagicMock(return_value={"task_id": "task_w_1"})
    mock_task.task_id = "task_w_1"

    mock_action = MagicMock()
    mock_action.action_type = "extract"
    mock_action.model_dump = MagicMock(return_value={"action_type": "extract", "action_id": "a1"})
    mock_action.get_xpath = MagicMock(return_value="//div")
    mock_action.has_mini_agent = False

    with (
        patch("skyvern.services.workflow_service.get_workflow_run_response", new_callable=AsyncMock) as mock_get,
        patch("skyvern.core.script_generations.transform_workflow_run.app") as mock_app,
    ):
        mock_get.return_value = mock_workflow_run_resp
        mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=mock_workflow)
        mock_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(return_value=[mock_while_run, mock_child])
        mock_app.DATABASE.tasks.get_tasks_by_ids = AsyncMock(return_value=[mock_task])
        mock_app.DATABASE.tasks.get_tasks_actions = AsyncMock(return_value=[mock_action])

        result = await transform_workflow_run_to_code_gen_input(
            workflow_run_id="wr_w",
            organization_id="org_w",
        )

    assert len(result.workflow_blocks) == 1
    wb = result.workflow_blocks[0]
    assert wb["block_type"] == "while_loop"
    lbs = wb.get("loop_blocks", [])
    assert len(lbs) == 1
    assert lbs[0].get("task_id") == "task_w_1"
    assert "task_w_1" in result.actions_by_task


@pytest.mark.asyncio
async def test_whileloop_script_compiles() -> None:
    from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code

    blocks = [
        {
            "block_type": "while_loop",
            "label": "w",
            "condition": {"criteria_type": "jinja2_template", "expression": "{{ false }}"},
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "ex",
                    "data_extraction_goal": "x",
                },
            ],
        },
    ]
    workflow = {
        "workflow_id": "wf_w",
        "title": "W",
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
        ),
    ):
        result = await generate_workflow_script_python_code(
            file_name="w.py",
            workflow_run_request={"workflow_id": "wp_w"},
            workflow=workflow,
            blocks=blocks,
            actions_by_task={},
            script_id="s1",
            script_revision_id="r1",
            organization_id="o1",
        )
    assert compile(result.source_code, "<w>", "exec")
    assert "async for current_value in skyvern.while_loop" in result.source_code
    assert "criteria_type" in result.source_code
    assert "def run_workflow" in result.source_code


@pytest.mark.asyncio
async def test_cached_whileloop_unexecuted_branch_not_at_module_level() -> None:
    from skyvern.core.script_generations.generate_script import generate_workflow_script_python_code
    from skyvern.services.workflow_script_service import ScriptBlockSource

    blocks = [
        {"block_type": "task", "label": "t1", "task_id": "tsk", "title": "T"},
    ]
    cached_blocks = {
        "wlab": ScriptBlockSource(
            label="wlab",
            code=("async for current_value in skyvern.while_loop(condition='{{ false }}', label='wlab'):\n    pass\n"),
            run_signature="async for current_value in skyvern.while_loop(condition='{{ false }}', label='wlab'):",
            workflow_run_id="wr_p",
            workflow_run_block_id="wrb_p",
            input_fields=None,
            requires_agent=False,
        ),
    }
    workflow = {"workflow_id": "wf", "title": "X", "workflow_definition": {"parameters": []}}
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
            file_name="x.py",
            workflow_run_request={"workflow_id": "wp"},
            workflow=workflow,
            blocks=blocks,
            actions_by_task={"tsk": []},
            script_id="s",
            script_revision_id="r",
            organization_id="o",
            cached_blocks=cached_blocks,
        )
    compile(result.source_code, "<x>", "exec")
    assert "async for current_value in skyvern.while_loop" not in result.source_code
