"""Golden-file reorder tests for the backend graph validator (SKY-9059).

For each scope (top-level, inside-loop, inside-branch), a fixture chain of four
NavigationBlocks is produced, reordered via three canonical permutations
(adjacent-swap, head-to-middle, middle-to-tail), round-tripped through
model_dump / model_validate, and handed to the graph validator.

All permutations must satisfy the four invariants enforced by
``Block._build_loop_graph`` / ``WorkflowService._build_workflow_graph``:

* unique labels
* a single root (in-degree 0 block)
* every block reachable from the root
* no cycles

The validators raise ``InvalidWorkflowDefinition`` when any invariant is
violated, so "accept the result" is expressed as "validator call does not
raise".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    ForLoopBlock,
    JinjaBranchCriteria,
    NavigationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService


def _output_param(key: str) -> OutputParameter:
    now = datetime.now(tz=timezone.utc)
    return OutputParameter(
        output_parameter_id=f"op_{key}",
        key=key,
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _nav_block(label: str, next_block_label: str | None = None) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_param(f"{label}_output"),
        next_block_label=next_block_label,
    )


def _for_loop_block(label: str, loop_blocks: list, next_block_label: str | None = None) -> ForLoopBlock:
    return ForLoopBlock(
        label=label,
        output_parameter=_output_param(f"{label}_output"),
        loop_blocks=loop_blocks,
        next_block_label=next_block_label,
    )


def _workflow_def(blocks: list, finally_block_label: str | None = None, version: int = 2) -> WorkflowDefinition:
    return WorkflowDefinition(
        parameters=[],
        blocks=blocks,
        finally_block_label=finally_block_label,
        version=version,
    )


def _roundtrip(workflow_def: WorkflowDefinition) -> WorkflowDefinition:
    """Serialize → deserialize to exercise the persistence path.

    Every reorder in the frontend eventually lands in the DB as JSON, so the
    graph validator should accept the result after a full model_dump /
    model_validate cycle — not just on the in-memory object.
    """
    return WorkflowDefinition.model_validate(workflow_def.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Permutation helpers
#
# The project tracks three canonical drag-targets that cover the interesting
# list-mutation cases against a 4-item chain [a, b, c, d]:
#
#   adjacent-swap    : swap two neighbors                  -> [a, c, b, d]
#   head-to-middle   : move the head element to the middle -> [b, c, a, d]
#   middle-to-tail   : move a middle element to the tail   -> [a, c, d, b]
#
# Reordering must NOT break the graph: labels and next_block_label pointers
# are preserved; only the list order of sibling blocks changes.
# ---------------------------------------------------------------------------

_ADJACENT_SWAP = (0, 2, 1, 3)  # swap positions 1 <-> 2
_HEAD_TO_MIDDLE = (1, 2, 0, 3)  # move position 0 to position 2
_MIDDLE_TO_TAIL = (0, 2, 3, 1)  # move position 1 to position 3

_PERMUTATIONS = [
    pytest.param(_ADJACENT_SWAP, id="adjacent_swap"),
    pytest.param(_HEAD_TO_MIDDLE, id="head_to_middle"),
    pytest.param(_MIDDLE_TO_TAIL, id="middle_to_tail"),
]


def _reorder(items: list, permutation: tuple[int, int, int, int]) -> list:
    assert len(items) == len(permutation), "permutation must cover every item"
    return [items[i] for i in permutation]


# ---------------------------------------------------------------------------
# Scope 1: top-level reordering
# ---------------------------------------------------------------------------


class TestTopLevelReorder:
    """Reorder four sibling blocks at the top of a v2 workflow.

    Edges are explicit (a -> b -> c -> d) so list order is cosmetic; the
    validator should accept any permutation as long as the references stay
    intact.
    """

    @staticmethod
    def _fixture() -> list:
        return [
            _nav_block("a", "b"),
            _nav_block("b", "c"),
            _nav_block("c", "d"),
            _nav_block("d"),
        ]

    @pytest.mark.parametrize("permutation", _PERMUTATIONS)
    def test_reorder_preserves_validation(self, permutation: tuple[int, int, int, int]) -> None:
        blocks = _reorder(self._fixture(), permutation)
        workflow_def = _roundtrip(_workflow_def(blocks))

        # Invariant sanity: unique labels, single root at 'a', all four reachable.
        labels = [b.label for b in workflow_def.blocks]
        assert sorted(labels) == ["a", "b", "c", "d"]
        assert len(set(labels)) == len(labels)

        WorkflowService().validate_workflow_block_graph(workflow_def)


# ---------------------------------------------------------------------------
# Scope 2: inside-loop reordering
# ---------------------------------------------------------------------------


class TestInsideLoopReorder:
    """Reorder four sibling blocks inside a single ForLoopBlock.

    Exercises ``Block._build_loop_graph`` via ``validate_loop_blocks``; the
    outer workflow contains only the loop, so any validation failure must
    originate from the reordered inner chain.
    """

    @staticmethod
    def _inner_blocks() -> list:
        return [
            _nav_block("inner_a", "inner_b"),
            _nav_block("inner_b", "inner_c"),
            _nav_block("inner_c", "inner_d"),
            _nav_block("inner_d"),
        ]

    @pytest.mark.parametrize("permutation", _PERMUTATIONS)
    def test_reorder_preserves_validation(self, permutation: tuple[int, int, int, int]) -> None:
        reordered = _reorder(self._inner_blocks(), permutation)
        loop = _for_loop_block("loop", loop_blocks=reordered)
        workflow_def = _roundtrip(_workflow_def([loop]))

        assert len(workflow_def.blocks) == 1
        top = workflow_def.blocks[0]
        assert isinstance(top, ForLoopBlock)
        inner_labels = [b.label for b in top.loop_blocks]
        assert sorted(inner_labels) == ["inner_a", "inner_b", "inner_c", "inner_d"]
        assert len(set(inner_labels)) == len(inner_labels)

        # Directly exercise _build_loop_graph with sequential-defaulting disabled,
        # matching what validate_loop_blocks does at persist time.
        start_label, label_to_block, _ = top._build_loop_graph(
            top.loop_blocks,
            skip_sequential_defaulting=True,
        )
        assert start_label == "inner_a"
        assert set(label_to_block.keys()) == {"inner_a", "inner_b", "inner_c", "inner_d"}

        # And the public entry-point used by the service.
        top.validate_loop_blocks()


# ---------------------------------------------------------------------------
# Scope 3: inside-branch reordering
# ---------------------------------------------------------------------------


class TestInsideBranchReorder:
    """Reorder four blocks that make up a conditional branch's child chain.

    Workflow shape:

        cond --(true)--> a -> b -> c -> d -> merge
        cond --(else)--> merge
        merge (terminal)

    The four-block chain [a, b, c, d] is what the SortableContext for the
    "true" branch scopes in the UI (SKY-9058). The reordered top-level list
    is still a valid DAG because the edges are label-based.
    """

    @staticmethod
    def _branch_chain() -> list:
        return [
            _nav_block("a", "b"),
            _nav_block("b", "c"),
            _nav_block("c", "d"),
            _nav_block("d", "merge"),
        ]

    @staticmethod
    def _conditional() -> ConditionalBlock:
        return ConditionalBlock(
            label="cond",
            output_parameter=_output_param("cond_output"),
            branch_conditions=[
                BranchCondition(
                    criteria=JinjaBranchCriteria(expression="{{ true }}"),
                    next_block_label="a",
                    is_default=False,
                ),
                BranchCondition(next_block_label="merge", is_default=True),
            ],
        )

    @pytest.mark.parametrize("permutation", _PERMUTATIONS)
    def test_reorder_preserves_validation(self, permutation: tuple[int, int, int, int]) -> None:
        chain = _reorder(self._branch_chain(), permutation)
        blocks = [self._conditional(), *chain, _nav_block("merge")]
        workflow_def = _roundtrip(_workflow_def(blocks))

        labels = [b.label for b in workflow_def.blocks]
        assert sorted(labels) == ["a", "b", "c", "cond", "d", "merge"]
        assert len(set(labels)) == len(labels)

        WorkflowService().validate_workflow_block_graph(workflow_def)
