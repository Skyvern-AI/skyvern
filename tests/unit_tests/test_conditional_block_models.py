from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    BranchCriteria,
    BranchEvaluationContext,
    ConditionalBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockType


class AlwaysTrueCriteria(BranchCriteria):
    """Test double that always evaluates to True."""

    criteria_type: str = "always_true"

    async def evaluate(self, context: BranchEvaluationContext) -> bool:  # pragma: no cover - async stub
        return True


def build_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        key="result",
        output_parameter_id="op-123",
        workflow_id="wf-123",
        created_at=now,
        modified_at=now,
    )


def build_branch(order: int, is_default: bool = False, next_block_label: str | None = None) -> BranchCondition:
    criteria = None if is_default else AlwaysTrueCriteria()
    return BranchCondition(
        order=order,
        criteria=criteria,
        next_block_label=next_block_label,
        is_default=is_default,
    )


def test_branch_condition_requires_matching_default_state() -> None:
    with pytest.raises(ValueError):
        BranchCondition(order=0, criteria=None, is_default=False)

    with pytest.raises(ValueError):
        BranchCondition(order=1, criteria=AlwaysTrueCriteria(), is_default=True)


def test_conditional_block_requires_unique_default_branch() -> None:
    output_parameter = build_output_parameter()
    branches = [
        build_branch(order=0, next_block_label="A"),
        build_branch(order=1, is_default=True),
        build_branch(order=2, is_default=True),
    ]

    with pytest.raises(ValueError):
        ConditionalBlock(label="cond", output_parameter=output_parameter, branches=branches)


def test_conditional_block_orders_branches_by_order_value() -> None:
    output_parameter = build_output_parameter()
    block = ConditionalBlock(
        label="cond",
        output_parameter=output_parameter,
        branches=[build_branch(order=5, next_block_label="B"), build_branch(order=2, next_block_label="A")],
    )

    assert [branch.order for branch in block.branches] == [2, 5]
    assert block.block_type == BlockType.CONDITIONAL


def test_conditional_block_serializes_branch_criteria() -> None:
    output_parameter = build_output_parameter()
    block = ConditionalBlock(
        label="cond",
        output_parameter=output_parameter,
        branches=[
            build_branch(order=0, next_block_label="A"),
            build_branch(order=99, is_default=True, next_block_label=None),
        ],
    )

    data = block.model_dump()
    assert data["block_type"] == BlockType.CONDITIONAL
    assert data["branches"][0]["criteria"]["criteria_type"] == "always_true"
    assert data["branches"][1]["is_default"] is True
