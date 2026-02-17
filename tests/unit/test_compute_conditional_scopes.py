"""Tests for compute_conditional_scopes() function.

This function maps each block label to the conditional block label whose scope it belongs to.
It handles merge-point detection, nested conditionals, and deduplication of branch targets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    HttpRequestBlock,
    TaskBlock,
    compute_conditional_scopes,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter


def _make_output_parameter(key: str) -> OutputParameter:
    now = datetime.now(tz=timezone.utc)
    return OutputParameter(
        key=key,
        parameter_type="output",
        output_parameter_id=f"op_{key}",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_block(label: str, *, next_block_label: str | None = None) -> TaskBlock:
    return TaskBlock(
        label=label,
        url="https://example.com",
        output_parameter=_make_output_parameter(label),
        next_block_label=next_block_label,
    )


def _make_http_block(label: str, *, next_block_label: str | None = None) -> HttpRequestBlock:
    return HttpRequestBlock(
        label=label,
        url="https://example.com",
        method="GET",
        output_parameter=_make_output_parameter(label),
        next_block_label=next_block_label,
    )


def _make_conditional_block(
    label: str,
    branches: list[tuple[str | None, bool]],
    *,
    next_block_label: str | None = None,
) -> ConditionalBlock:
    """Create a conditional block with the given branches.

    Args:
        label: Block label
        branches: List of (next_block_label, is_default) tuples
        next_block_label: Default next block for the conditional itself (usually None)
    """
    branch_conditions = []
    for target, is_default in branches:
        if is_default:
            branch_conditions.append(BranchCondition(next_block_label=target, is_default=True))
        else:
            branch_conditions.append(
                BranchCondition(
                    next_block_label=target,
                    criteria={"criteria_type": "jinja2_template", "expression": "{{ true }}"},
                )
            )
    return ConditionalBlock(
        label=label,
        output_parameter=_make_output_parameter(label),
        branch_conditions=branch_conditions,
        next_block_label=next_block_label,
    )


class TestComputeConditionalScopes:
    """Tests for compute_conditional_scopes()."""

    def test_simple_two_branch_conditional_with_merge(self):
        """Test a simple conditional with two branches that merge.

        Workflow:
            Conditional(C) -> Branch1 -> A -> MergePoint(M)
                           -> Branch2 -> B -> M

        Expected: A and B are scoped to C, M is NOT scoped (merge point).
        """
        block_a = _make_task_block("A", next_block_label="M")
        block_b = _make_task_block("B", next_block_label="M")
        block_m = _make_task_block("M")
        cond = _make_conditional_block("C", [("A", False), ("B", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "M": block_m,
        }
        default_next_map = {
            "C": None,
            "A": "M",
            "B": "M",
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {"A": "C", "B": "C"}
        assert "M" not in scopes  # M is a merge point

    def test_conditional_with_chain_before_merge(self):
        """Test branches with multiple blocks before merge point.

        Workflow:
            Conditional(C) -> Branch1 -> A -> B -> MergePoint(M)
                           -> Branch2 -> D -> M

        Expected: A, B, D are scoped to C. M is NOT scoped.
        """
        block_a = _make_task_block("A", next_block_label="B")
        block_b = _make_task_block("B", next_block_label="M")
        block_d = _make_task_block("D", next_block_label="M")
        block_m = _make_task_block("M")
        cond = _make_conditional_block("C", [("A", False), ("D", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "D": block_d,
            "M": block_m,
        }
        default_next_map = {
            "C": None,
            "A": "B",
            "B": "M",
            "D": "M",
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {"A": "C", "B": "C", "D": "C"}
        assert "M" not in scopes

    def test_conditional_with_terminal_branches(self):
        """Test branches that don't merge (terminate independently).

        Workflow:
            Conditional(C) -> Branch1 -> A (terminal)
                           -> Branch2 -> B (terminal)

        Expected: A and B are scoped to C since they don't appear in all branches.
        """
        block_a = _make_task_block("A")
        block_b = _make_task_block("B")
        cond = _make_conditional_block("C", [("A", False), ("B", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
        }
        default_next_map = {
            "C": None,
            "A": None,
            "B": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {"A": "C", "B": "C"}

    def test_conditional_all_branches_terminal_none(self):
        """Test when all branches have None as target (no blocks to scope).

        Workflow:
            Conditional(C) -> Branch1 -> None
                           -> Branch2 -> None

        Expected: No scopes (no blocks in the branches).
        """
        cond = _make_conditional_block("C", [(None, False), (None, True)])

        label_to_block = {"C": cond}
        default_next_map = {"C": None}

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {}

    def test_multiple_branches_same_target_deduplication(self):
        """Test that duplicate branch targets are deduplicated.

        Workflow:
            Conditional(C) -> Branch1 -> A -> M
                           -> Branch2 -> A -> M  (same as Branch1)
                           -> Branch3 -> B -> M

        With deduplication, unique targets are [A, B], so num_branches = 2.
        Both chains go to M, so M is a merge point.
        A appears in only one chain (after dedup), B in another.
        """
        block_a = _make_task_block("A", next_block_label="M")
        block_b = _make_task_block("B", next_block_label="M")
        block_m = _make_task_block("M")
        cond = _make_conditional_block("C", [("A", False), ("A", False), ("B", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "M": block_m,
        }
        default_next_map = {
            "C": None,
            "A": "M",
            "B": "M",
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # A and B are scoped to C, M is the merge point
        assert scopes == {"A": "C", "B": "C"}
        assert "M" not in scopes

    def test_nested_conditionals(self):
        """Test nested conditionals (conditional inside another's branch).

        Workflow:
            OuterCond(C1) -> Branch1 -> InnerCond(C2) -> BranchA -> X
                                                      -> BranchB -> Y
                          -> Branch2 -> Z -> MergePoint(M)

        Expected:
        - C2 is scoped to C1 (it's in C1's branch)
        - X and Y are scoped to C2 (inner conditional handles its own branches)
        - Z is scoped to C1
        - M might or might not be scoped depending on structure
        """
        block_x = _make_task_block("X")
        block_y = _make_task_block("Y")
        block_z = _make_task_block("Z", next_block_label="M")
        block_m = _make_task_block("M")
        inner_cond = _make_conditional_block("C2", [("X", False), ("Y", True)])
        outer_cond = _make_conditional_block("C1", [("C2", False), ("Z", True)])

        label_to_block = {
            "C1": outer_cond,
            "C2": inner_cond,
            "X": block_x,
            "Y": block_y,
            "Z": block_z,
            "M": block_m,
        }
        default_next_map = {
            "C1": None,
            "C2": None,  # Inner conditional doesn't have a default next
            "X": None,
            "Y": None,
            "Z": "M",
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # C2 is scoped to C1 (it's in C1's branch, and tracing stops at C2)
        assert scopes.get("C2") == "C1"
        # X and Y are scoped to C2 (inner conditional)
        assert scopes.get("X") == "C2"
        assert scopes.get("Y") == "C2"
        # Z is scoped to C1
        assert scopes.get("Z") == "C1"

    def test_no_conditionals_in_workflow(self):
        """Test workflow with no conditional blocks.

        Workflow:
            A -> B -> C

        Expected: No scopes.
        """
        block_a = _make_task_block("A", next_block_label="B")
        block_b = _make_task_block("B", next_block_label="C")
        block_c = _make_task_block("C")

        label_to_block = {
            "A": block_a,
            "B": block_b,
            "C": block_c,
        }
        default_next_map = {
            "A": "B",
            "B": "C",
            "C": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {}

    def test_conditional_with_single_branch(self):
        """Test conditional with effectively one unique branch target.

        Workflow:
            Conditional(C) -> Branch1 -> A
                           -> Branch2 -> A  (same target, deduplicated)

        After deduplication, num_branches = 1, and A appears in 1/1 chains,
        making it a "merge point" (appears in all branches).
        """
        block_a = _make_task_block("A")
        cond = _make_conditional_block("C", [("A", False), ("A", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
        }
        default_next_map = {
            "C": None,
            "A": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # A appears in all (1) branch chains, so it's treated as a merge point
        assert scopes == {}

    def test_three_branch_conditional_partial_merge(self):
        """Test three branches where only some merge.

        Workflow:
            Conditional(C) -> Branch1 -> A -> M
                           -> Branch2 -> B -> M
                           -> Branch3 -> D (terminal, no merge)

        M appears in 2/3 branches, so it's NOT a merge point.
        All of A, B, D, M should be scoped to C.
        """
        block_a = _make_task_block("A", next_block_label="M")
        block_b = _make_task_block("B", next_block_label="M")
        block_d = _make_task_block("D")
        block_m = _make_task_block("M")
        cond = _make_conditional_block("C", [("A", False), ("B", False), ("D", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "D": block_d,
            "M": block_m,
        }
        default_next_map = {
            "C": None,
            "A": "M",
            "B": "M",
            "D": None,
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # M only appears in 2/3 branches, so it's still inside the conditional scope
        assert scopes == {"A": "C", "B": "C", "D": "C", "M": "C"}

    def test_merge_point_with_blocks_after(self):
        """Test that blocks after the merge point are not scoped.

        Workflow:
            Conditional(C) -> Branch1 -> A -> M -> X -> Y
                           -> Branch2 -> B -> M

        M is the merge point (appears in both chains).
        X and Y come after M and should NOT be scoped.
        """
        block_a = _make_task_block("A", next_block_label="M")
        block_b = _make_task_block("B", next_block_label="M")
        block_m = _make_task_block("M", next_block_label="X")
        block_x = _make_task_block("X", next_block_label="Y")
        block_y = _make_task_block("Y")
        cond = _make_conditional_block("C", [("A", False), ("B", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "M": block_m,
            "X": block_x,
            "Y": block_y,
        }
        default_next_map = {
            "C": None,
            "A": "M",
            "B": "M",
            "M": "X",
            "X": "Y",
            "Y": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # A and B are scoped, M and everything after is NOT
        assert scopes == {"A": "C", "B": "C"}
        assert "M" not in scopes
        assert "X" not in scopes
        assert "Y" not in scopes

    def test_branch_to_nonexistent_block(self):
        """Test graceful handling when branch targets a non-existent block.

        This shouldn't happen in practice (validation catches it), but the
        function should handle it gracefully.
        """
        cond = _make_conditional_block("C", [("MISSING", False), ("A", True)])
        block_a = _make_task_block("A")

        label_to_block = {
            "C": cond,
            "A": block_a,
        }
        default_next_map = {
            "C": None,
            "A": None,
        }

        # Should not raise, MISSING just won't be in the results
        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # Only A is scoped (MISSING is not in label_to_block)
        assert scopes == {"A": "C"}

    def test_empty_workflow(self):
        """Test with empty inputs."""
        scopes = compute_conditional_scopes({}, {})
        assert scopes == {}

    def test_conditional_only_no_other_blocks(self):
        """Test with only a conditional block and no branch targets.

        Workflow:
            Conditional(C) -> Branch1 -> None
                           -> Branch2 -> None
        """
        cond = _make_conditional_block("C", [(None, False), (None, True)])

        label_to_block = {"C": cond}
        default_next_map = {"C": None}

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        assert scopes == {}

    def test_asymmetric_branch_lengths(self):
        """Test branches with significantly different chain lengths.

        Workflow:
            Conditional(C) -> Branch1 -> A -> B -> C2 -> D -> M
                           -> Branch2 -> M

        Branch1 has a long chain, Branch2 goes directly to M.
        M is the only block in both chains, so it's the merge point.
        """
        block_a = _make_task_block("A", next_block_label="B")
        block_b = _make_task_block("B", next_block_label="C2")
        block_c2 = _make_task_block("C2", next_block_label="D")
        block_d = _make_task_block("D", next_block_label="M")
        block_m = _make_task_block("M")
        cond = _make_conditional_block("C", [("A", False), ("M", True)])

        label_to_block = {
            "C": cond,
            "A": block_a,
            "B": block_b,
            "C2": block_c2,
            "D": block_d,
            "M": block_m,
        }
        default_next_map = {
            "C": None,
            "A": "B",
            "B": "C2",
            "C2": "D",
            "D": "M",
            "M": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # A, B, C2, D are in Branch1 only, so they're scoped
        # M appears in both branches, so it's the merge point
        assert scopes == {"A": "C", "B": "C", "C2": "C", "D": "C"}
        assert "M" not in scopes

    def test_multiple_independent_conditionals(self):
        """Test multiple conditionals at the same level (not nested).

        Workflow:
            C1 -> Branch1 -> A
               -> Branch2 -> B
            (after C1) -> C2 -> Branch1 -> X
                             -> Branch2 -> Y
        """
        block_a = _make_task_block("A", next_block_label="C2")
        block_b = _make_task_block("B", next_block_label="C2")
        block_x = _make_task_block("X")
        block_y = _make_task_block("Y")
        cond1 = _make_conditional_block("C1", [("A", False), ("B", True)])
        cond2 = _make_conditional_block("C2", [("X", False), ("Y", True)])

        label_to_block = {
            "C1": cond1,
            "C2": cond2,
            "A": block_a,
            "B": block_b,
            "X": block_x,
            "Y": block_y,
        }
        default_next_map = {
            "C1": None,
            "C2": None,
            "A": "C2",
            "B": "C2",
            "X": None,
            "Y": None,
        }

        scopes = compute_conditional_scopes(label_to_block, default_next_map)

        # A and B are scoped to C1
        # C2 is the merge point for C1 (appears in both A and B chains)
        # X and Y are scoped to C2
        assert scopes.get("A") == "C1"
        assert scopes.get("B") == "C1"
        assert "C2" not in scopes  # C2 is a merge point for C1
        assert scopes.get("X") == "C2"
        assert scopes.get("Y") == "C2"
