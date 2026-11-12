"""Tests for WorkflowService.validate_workflow_block_graph.

Validates that the block graph validation is called correctly with support for
orphaned blocks, cycles, dangling references, finally blocks, and ForLoopBlock nesting.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    BranchCondition,
    ConditionalBlock,
    ForLoopBlock,
    JinjaBranchCriteria,
    NavigationBlock,
    WhileLoopBlock,
    labels_only_on_untaken_branches,
    resolve_conditional_merge_edges,
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


def _loop_wrapper(wrapper: str, loop_blocks: list) -> Block:
    if wrapper == "for_loop":
        return _for_loop_block("loop", loop_blocks=loop_blocks)
    return WhileLoopBlock(
        label="loop",
        output_parameter=_output_param("loop_output"),
        loop_blocks=loop_blocks,
        condition=JinjaBranchCriteria(expression="{{ true }}"),
    )


def _conditional(
    label: str, targets: list[str | None], next_block_label: str | None = None, with_default: bool = True
) -> ConditionalBlock:
    criteria_targets = targets[:-1] if with_default else targets
    branches = [
        BranchCondition(
            criteria=JinjaBranchCriteria(expression="{{ true }}"), next_block_label=target, is_default=False
        )
        for target in criteria_targets
    ]
    if with_default:
        branches.append(BranchCondition(next_block_label=targets[-1], is_default=True))
    return ConditionalBlock(
        label=label,
        output_parameter=_output_param(f"{label}_output"),
        branch_conditions=branches,
        next_block_label=next_block_label,
    )


def _self_merge_blocks() -> list[Block]:
    return [
        _conditional("check", ["draft", "report"], next_block_label="draft"),
        _nav_block("draft", "review"),
        _nav_block("review", "report"),
        _nav_block("report"),
    ]


def _resolved_next_map(blocks: list[Block]) -> dict[str, str | None]:
    label_to_block = {block.label: block for block in blocks}
    next_map = {block.label: block.next_block_label for block in blocks}
    resolve_conditional_merge_edges(blocks, label_to_block, next_map)
    return next_map


def _workflow_def(blocks: list, finally_block_label: str | None = None, version: int = 2) -> WorkflowDefinition:
    return WorkflowDefinition(parameters=[], blocks=blocks, finally_block_label=finally_block_label, version=version)


class TestValidateWorkflowBlockGraph:
    """Tests for validate_workflow_block_graph on WorkflowService."""

    def test_valid_linear_chain(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b")]
        svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_single_block(self) -> None:
        svc = WorkflowService()
        svc.validate_workflow_block_graph(_workflow_def([_nav_block("only")]))

    def test_empty_workflow(self) -> None:
        svc = WorkflowService()
        svc.validate_workflow_block_graph(_workflow_def([]))

    def test_orphaned_block_raises(self) -> None:
        """Three blocks where both a and b point to c, making a and b both roots (in-degree 0)."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "c"), _nav_block("b", "c"), _nav_block("c")]
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_circular_reference_two_blocks_raises(self) -> None:
        """Two blocks pointing to each other — both have in-degree 1, so no root exists."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "a")]
        with pytest.raises(InvalidWorkflowDefinition, match="Circular reference detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_circular_reference_with_root_raises(self) -> None:
        """Three blocks: a -> b -> c -> b. Has a valid root (a) but b-c form a cycle."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "c"), _nav_block("c", "b")]
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_dangling_next_block_label_raises(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "nonexistent")]
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_conditional_block(self) -> None:
        svc = WorkflowService()
        blocks = [
            ConditionalBlock(
                label="cond",
                output_parameter=_output_param("cond_output"),
                branch_conditions=[
                    BranchCondition(
                        criteria=JinjaBranchCriteria(expression="{{ true }}"),
                        next_block_label="a",
                        is_default=False,
                    ),
                    BranchCondition(next_block_label="b", is_default=True),
                ],
            ),
            _nav_block("a"),
            _nav_block("b"),
        ]
        svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_conditional_with_dangling_branch_raises(self) -> None:
        svc = WorkflowService()
        blocks = [
            ConditionalBlock(
                label="cond",
                output_parameter=_output_param("cond_output"),
                branch_conditions=[
                    BranchCondition(next_block_label="missing", is_default=True),
                ],
            ),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_valid_with_finally_block(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("b", "fin"), _nav_block("fin")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, finally_block_label="fin"))

    def test_block_pointing_to_finally_block_passes(self) -> None:
        """Edge to finally block is nullified before validation, so it should pass."""
        svc = WorkflowService()
        blocks = [_nav_block("a", "fin"), _nav_block("fin")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, finally_block_label="fin"))

    def test_for_loop_with_valid_nested_blocks(self) -> None:
        svc = WorkflowService()
        loop = _for_loop_block("loop", loop_blocks=[_nav_block("inner_a", "inner_b"), _nav_block("inner_b")])
        svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_cycle_in_nested_blocks_raises(self) -> None:
        """Cycle inside a ForLoopBlock's loop_blocks should be detected."""
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[
                _nav_block("inner_a", "inner_b"),
                _nav_block("inner_b", "inner_c"),
                _nav_block("inner_c", "inner_b"),
            ],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_orphan_in_nested_blocks_raises(self) -> None:
        """Orphaned blocks inside a ForLoopBlock should be detected.

        Both inner_a and inner_b have explicit next_block_label pointing to inner_c,
        making them both roots (in-degree 0) → multiple entry blocks.
        """
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[_nav_block("inner_a", "inner_c"), _nav_block("inner_b", "inner_c"), _nav_block("inner_c")],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_for_loop_with_dangling_reference_in_nested_blocks_raises(self) -> None:
        svc = WorkflowService()
        loop = _for_loop_block(
            "loop",
            loop_blocks=[_nav_block("inner_a", "missing")],
        )
        with pytest.raises(InvalidWorkflowDefinition, match="unknown next_block_label"):
            svc.validate_workflow_block_graph(_workflow_def([loop]))

    def test_deeply_nested_for_loop_with_cycle_raises(self) -> None:
        """A ForLoopBlock nested inside another ForLoopBlock — cycle in the inner loop is detected."""
        svc = WorkflowService()
        inner_loop = _for_loop_block(
            "inner_loop",
            loop_blocks=[
                _nav_block("deep_a", "deep_b"),
                _nav_block("deep_b", "deep_c"),
                _nav_block("deep_c", "deep_b"),
            ],
        )
        outer_loop = _for_loop_block("outer_loop", loop_blocks=[inner_loop])
        with pytest.raises(InvalidWorkflowDefinition, match="cycle"):
            svc.validate_workflow_block_graph(_workflow_def([outer_loop]))

    def test_deeply_nested_for_loop_valid(self) -> None:
        """A valid ForLoopBlock nested inside another ForLoopBlock passes validation."""
        svc = WorkflowService()
        inner_loop = _for_loop_block(
            "inner_loop",
            loop_blocks=[_nav_block("deep_a", "deep_b"), _nav_block("deep_b")],
        )
        outer_loop = _for_loop_block("outer_loop", loop_blocks=[inner_loop])
        svc.validate_workflow_block_graph(_workflow_def([outer_loop]))

    def test_duplicate_label_raises(self) -> None:
        svc = WorkflowService()
        blocks = [_nav_block("a", "b"), _nav_block("a")]
        with pytest.raises(InvalidWorkflowDefinition, match="Duplicate block label"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_disconnected_paths_raises(self) -> None:
        """Two disconnected paths should be detected as multiple entry blocks.

        Path 1: a -> b -> c (c has next_block_label=None, end of path)
        Path 2: d -> e (disconnected from path 1)

        Without sequential defaulting, c does NOT auto-connect to d,
        so both a and d are roots → multiple entry blocks.
        """
        svc = WorkflowService()
        blocks = [
            _nav_block("a", "b"),
            _nav_block("b", "c"),
            _nav_block("c"),
            _nav_block("d", "e"),
            _nav_block("e"),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks detected"):
            svc.validate_workflow_block_graph(_workflow_def(blocks))

    def test_v1_workflow_skips_validation(self) -> None:
        """v1 workflows (sequential execution) should skip DAG validation entirely."""
        svc = WorkflowService()
        # Two disconnected blocks — would fail v2 validation but should pass v1
        blocks = [_nav_block("a"), _nav_block("b")]
        svc.validate_workflow_block_graph(_workflow_def(blocks, version=1))

    def test_merge_into_own_branch_target_adds_no_loop(self) -> None:
        blocks = _self_merge_blocks()
        next_map = _resolved_next_map(blocks)
        label_to_block = {block.label: block for block in blocks}
        assert next_map["report"] is None
        assert labels_only_on_untaken_branches(blocks[0], "report", label_to_block, next_map) == {"draft", "review"}
        assert labels_only_on_untaken_branches(blocks[0], "draft", label_to_block, next_map) == set()
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_looping_graph_over_the_work_limit_keeps_main_links_and_loop_error(self, monkeypatch) -> None:
        monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block._SKIP_PATH_WORK_LIMIT", 0)
        blocks = _self_merge_blocks()
        assert _resolved_next_map(blocks)["report"] == "draft"
        with pytest.raises(InvalidWorkflowDefinition, match="Circular reference detected"):
            WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_merge_into_branch_target_reached_only_from_other_branch_keeps_link(self) -> None:
        blocks = [
            _conditional("check", ["side", "merge"], next_block_label="merge"),
            _nav_block("side", "side_end"),
            _nav_block("side_end"),
            _nav_block("merge"),
        ]
        assert _resolved_next_map(blocks)["side_end"] == "merge"
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_loop_closed_through_nested_conditional_branch_is_not_linked(self) -> None:
        blocks = [
            _conditional("outer", ["start", "shared"], next_block_label="start"),
            _nav_block("start", "inner"),
            _conditional("inner", ["side", "shared"]),
            _nav_block("side"),
            _nav_block("shared"),
        ]
        assert _resolved_next_map(blocks)["shared"] is None
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_merge_into_own_branch_target_inside_for_loop(self) -> None:
        loop = _for_loop_block("loop", loop_blocks=_self_merge_blocks())
        WorkflowService().validate_workflow_block_graph(_workflow_def([loop]))

    @pytest.mark.parametrize("inner_first", [False, True])
    def test_inner_branch_jumping_to_outer_merge_keeps_the_outer_merge_in_either_order(self, inner_first: bool) -> None:
        route = _conditional("route", ["inner", "side"], next_block_label="wrap_up")
        inner = _conditional("inner", ["wrap_up", "detail"], next_block_label="store")
        rest = [_nav_block("side"), _nav_block("detail"), _nav_block("store"), _nav_block("wrap_up", "notify")]
        rest.append(_nav_block("notify"))
        blocks: list[Block] = [inner, route, *rest] if inner_first else [route, inner, *rest]
        assert _resolved_next_map(blocks) == {
            "route": "wrap_up",
            "inner": "store",
            "side": "wrap_up",
            "detail": "store",
            "store": "wrap_up",
            "wrap_up": "notify",
            "notify": None,
        }
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    @pytest.mark.parametrize("order", list(itertools.permutations(["route", "inner", "wrap_up"])))
    def test_outer_merge_that_is_a_conditional_keeps_the_outer_merge_in_any_order(self, order: tuple[str, ...]) -> None:
        conditionals = {
            "route": _conditional("route", ["inner", "side"], next_block_label="wrap_up"),
            "inner": _conditional("inner", ["wrap_up", "detail"], next_block_label="store"),
            "wrap_up": _conditional("wrap_up", ["notify"], next_block_label="notify"),
        }
        rest = [_nav_block("side"), _nav_block("detail"), _nav_block("store"), _nav_block("notify")]
        blocks: list[Block] = [*(conditionals[label] for label in order), *rest]
        resolved = _resolved_next_map(blocks)
        assert (resolved["store"], resolved["notify"]) == ("wrap_up", None)
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_conditionals_with_equal_reach_resolve_the_same_in_any_order(self) -> None:
        conditionals = [
            _conditional("c_route", ["g_report", "a_fix"], next_block_label="d_check"),
            _conditional("d_check", ["b_retry", "g_report"], next_block_label="f_finish"),
            _conditional("e_start", ["f_finish", "g_report"], next_block_label="c_route"),
        ]
        rest = [_nav_block("a_fix", "g_report"), _nav_block("b_retry", "g_report"), _nav_block("f_finish")]
        rest.append(_nav_block("g_report"))
        resolved = [_resolved_next_map([*order, *rest]) for order in itertools.permutations(conditionals)]
        assert all(next_map == resolved[0] for next_map in resolved)

    @pytest.mark.parametrize("first", ["left", "right"])
    def test_sibling_conditionals_sharing_a_terminal_link_it_in_block_order(self, first: str) -> None:
        second = "right" if first == "left" else "left"
        blocks = [
            _conditional("root", ["left", "right"]),
            _conditional(first, [f"{first}_only", "shared"], next_block_label=f"{first}_done"),
            _conditional(second, [f"{second}_only", "shared"], next_block_label=f"{second}_done"),
            *(_nav_block(label) for label in ["left_only", "right_only", "shared", "left_done", "right_done"]),
        ]
        next_map = _resolved_next_map(blocks)
        assert (next_map["shared"], next_map[f"{first}_done"]) == (f"{first}_done", f"{second}_done")
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_skipped_merge_elsewhere_keeps_sibling_conditionals_in_block_order(self) -> None:
        blocks = [
            _conditional("root", ["left", "right"]),
            _conditional("right", ["right_only", "shared"], next_block_label="right_done"),
            _conditional("left", ["left_only", "shared"], next_block_label="left_done"),
            *(_nav_block(label) for label in ["left_only", "right_only", "shared", "left_done", "right_done"]),
            *_self_merge_blocks(),
        ]
        next_map = _resolved_next_map(blocks)
        assert next_map["report"] is None
        assert (next_map["shared"], next_map["right_done"]) == ("right_done", "left_done")

    def test_skipped_merge_link_cannot_hide_fallback_loop_through_empty_branch(self) -> None:
        blocks = [
            _conditional("route", ["prepare", "submit"], next_block_label="submit"),
            _nav_block("prepare"),
            _conditional("submit", ["confirm", None], next_block_label="route"),
            _nav_block("confirm"),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="Circular reference detected"):
            WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    @pytest.mark.parametrize("wrapper", ["for_loop", "while_loop"])
    @pytest.mark.parametrize(
        "tail",
        [
            [_conditional("submit", ["confirm", None], next_block_label="route"), _nav_block("confirm")],
            [_nav_block("submit", "settle"), _conditional("settle", [None, None], next_block_label="prepare")],
        ],
        ids=["submit_branch", "settle"],
    )
    def test_loop_body_empty_branch_ends_iteration_so_the_merge_link_stays(self, wrapper: str, tail: list) -> None:
        body = [_conditional("route", ["prepare", "submit"], next_block_label="submit"), _nav_block("prepare"), *tail]
        loop = _loop_wrapper(wrapper, body)
        _, _, next_map = loop._build_loop_graph(loop.loop_blocks)
        assert next_map["prepare"] == "submit"
        WorkflowService().validate_workflow_block_graph(_workflow_def([loop]))

    def test_skipped_merge_link_cannot_hide_fallback_loop_when_nothing_matches(self) -> None:
        blocks = [
            _conditional("route", ["prepare", "submit"], next_block_label="submit"),
            _nav_block("prepare"),
            _conditional("submit", ["confirm"], next_block_label="route", with_default=False).model_copy(
                update={"continue_on_failure": True}
            ),
            _nav_block("confirm"),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="Circular reference detected"):
            WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    def test_skipped_merge_link_does_not_count_an_unused_own_next_as_reachable(self) -> None:
        blocks = [
            _conditional("check", ["tail", "tail"], next_block_label="merge"),
            _nav_block("merge", "tail"),
            _nav_block("tail"),
        ]
        with pytest.raises(InvalidWorkflowDefinition, match="Disconnected blocks"):
            WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))

    @pytest.mark.parametrize(
        ("blocks", "linked"),
        [
            (
                [
                    _conditional("route", ["prepare", "submit"], next_block_label="submit"),
                    _nav_block("prepare"),
                    _nav_block("submit", "gate"),
                    _conditional("gate", ["settle", "done"]),
                    _conditional("settle", [None, None], next_block_label="prepare"),
                    _nav_block("done"),
                ],
                {"prepare": "submit"},
            ),
            (
                [
                    _nav_block("start", "check"),
                    _nav_block("check", "decide"),
                    _conditional("decide", ["retry", "finish"]),
                    _conditional("retry", [None, None], next_block_label="check"),
                    _nav_block("finish"),
                ],
                {},
            ),
            (
                [
                    _conditional("route", ["prepare", "side"], next_block_label="merge_check"),
                    _nav_block("prepare", "settle_check"),
                    _conditional("settle_check", [None]),
                    _nav_block("side"),
                    _conditional("merge_check", ["settle_check", "wrap"]),
                    _nav_block("wrap"),
                ],
                {"settle_check": "merge_check", "side": "merge_check"},
            ),
            (
                [
                    _conditional("start_gate", [None, "retry_gate", None]),
                    _conditional("idle_gate", [None, None]),
                    _conditional("retry_gate", ["idle_gate"], next_block_label="start_gate"),
                ],
                {},
            ),
        ],
        ids=["merge_link_kept", "fallback_loop_accepted", "conditional_tail_link_kept", "conditional_tail_accepted"],
    )
    def test_loop_free_graph_keeps_every_merge_link_and_validates(self, blocks: list, linked: dict[str, str]) -> None:
        resolved = _resolved_next_map(blocks)
        assert {label: resolved[label] for label in linked} == linked
        WorkflowService().validate_workflow_block_graph(_workflow_def(blocks))
