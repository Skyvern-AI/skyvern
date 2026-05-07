"""Track while-loop child blocks for script caching (mirrors for-loop SKY-7751 pattern)."""

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import (
    BlockType,
    FileDownloadBlock,
    JinjaBranchCriteria,
    TaskBlock,
    WhileLoopBlock,
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


def test_while_loop_is_cacheable() -> None:
    assert BlockType.WHILE_LOOP in BLOCK_TYPES_THAT_SHOULD_BE_CACHED


def test_while_loop_child_labels_collected() -> None:
    inner = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"), url="https://x.test")
    wloop = WhileLoopBlock(
        label="w",
        output_parameter=_make_output_param("w"),
        loop_blocks=[inner],
        condition=JinjaBranchCriteria(expression="{{ true }}"),
    )
    script_blocks_by_label: dict[str, object] = {}
    blocks_to_update: set[str] = set()
    for loop_child in wloop.loop_blocks:
        if (
            loop_child.label
            and loop_child.label not in script_blocks_by_label
            and loop_child.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        ):
            blocks_to_update.add(loop_child.label)
    assert "inner_task" in blocks_to_update


def test_while_loop_file_download_child() -> None:
    dl = FileDownloadBlock(
        label="dl",
        output_parameter=_make_output_param("dl"),
        url="http://example.com",
        navigation_goal="get file",
    )
    wloop = WhileLoopBlock(
        label="w",
        output_parameter=_make_output_param("w"),
        loop_blocks=[dl],
        condition=JinjaBranchCriteria(expression="{{ false }}"),
    )
    script_blocks_by_label: dict[str, object] = {}
    blocks_to_update: set[str] = set()
    for loop_child in wloop.loop_blocks:
        if (
            loop_child.label
            and loop_child.label not in script_blocks_by_label
            and loop_child.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        ):
            blocks_to_update.add(loop_child.label)
    assert "dl" in blocks_to_update
