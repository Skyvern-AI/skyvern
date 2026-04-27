"""Tests for the copilot next_block_label chain repair logic.

Ensures that _repair_next_block_label_chain correctly stitches orphaned blocks
back into the reachable workflow chain when the LLM produces disconnected paths.
"""

from skyvern.forge.sdk.routes.workflow_copilot import (
    _break_cycles,
    _collect_reachable,
    _find_terminal_label,
    _order_orphaned_blocks,
    _repair_next_block_label_chain,
)
from skyvern.schemas.workflows import (
    BranchConditionYAML,
    BranchCriteriaYAML,
    ConditionalBlockYAML,
    ExtractionBlockYAML,
    ForLoopBlockYAML,
    NavigationBlockYAML,
)


def _nav(label: str, next_label: str | None = None) -> NavigationBlockYAML:
    return NavigationBlockYAML(label=label, next_block_label=next_label, navigation_goal="test")


class TestRepairChain:
    """Tests for _repair_next_block_label_chain."""

    def test_all_connected_no_change(self) -> None:
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label is None

    def test_single_block_no_change(self) -> None:
        blocks = [_nav("a")]
        _repair_next_block_label_chain(blocks)
        assert len(blocks) == 1
        assert blocks[0].next_block_label is None

    def test_empty_blocks(self) -> None:
        blocks: list = []
        _repair_next_block_label_chain(blocks)
        assert blocks == []

    def test_single_orphan_stitched_to_end(self) -> None:
        """A -> B (connected), C (orphaned) => A -> B -> C."""
        blocks = [_nav("a", "b"), _nav("b"), _nav("c")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label is None

    def test_orphaned_subchain_stitched(self) -> None:
        """A -> B (connected), X -> Y (orphaned subchain) => A -> B -> X -> Y."""
        blocks = [_nav("a", "b"), _nav("b"), _nav("x", "y"), _nav("y")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "x"
        assert blocks[2].next_block_label == "y"
        assert blocks[3].next_block_label is None

    def test_bug_scenario_two_disconnected_paths(self) -> None:
        """Reproduces the reported bug: copilot creates two disconnected paths.

        Original: open_url -> (end)
        LLM inserts: search_page -> extract_info but doesn't connect them to open_url.
        Expected repair: open_url -> search_page -> extract_info.
        """
        blocks = [
            NavigationBlockYAML(
                label="open_url", next_block_label=None, url="https://example.com", navigation_goal="go to url"
            ),
            NavigationBlockYAML(label="search_page", next_block_label="extract_info", navigation_goal="search"),
            ExtractionBlockYAML(label="extract_info", next_block_label=None, data_extraction_goal="extract data"),
        ]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "search_page"
        assert blocks[1].next_block_label == "extract_info"
        assert blocks[2].next_block_label is None

    def test_all_blocks_orphaned_except_first(self) -> None:
        """blocks[0] has no next, all others are orphaned."""
        blocks = [_nav("a"), _nav("b"), _nav("c")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label is None

    def test_dangling_reference_fixed(self) -> None:
        """Block references a non-existent label; treated as terminal."""
        blocks = [_nav("a", "nonexistent"), _nav("b")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label is None

    def test_cyclic_orphans_no_infinite_loop(self) -> None:
        """Orphaned blocks form a cycle: X -> Y -> X. Should not hang."""
        blocks = [_nav("a"), _nav("x", "y"), _nav("y", "x")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "x"
        # The cycle is broken; one of them becomes terminal.
        labels_chain = []
        current = blocks[0].next_block_label
        visited = set()
        while current and current not in visited:
            visited.add(current)
            labels_chain.append(current)
            block = {b.label: b for b in blocks}[current]
            current = block.next_block_label
        assert set(labels_chain) == {"x", "y"}

    def test_multiple_orphan_subchains(self) -> None:
        """Two separate orphan subchains get stitched in array order."""
        blocks = [_nav("a"), _nav("x", "y"), _nav("y"), _nav("p", "q"), _nav("q")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "x"
        assert blocks[1].next_block_label == "y"
        assert blocks[2].next_block_label == "p"
        assert blocks[3].next_block_label == "q"
        assert blocks[4].next_block_label is None

    def test_mixed_block_types(self) -> None:
        """Repair works across different block types."""
        blocks = [
            NavigationBlockYAML(label="nav_block", next_block_label=None, navigation_goal="go"),
            ExtractionBlockYAML(label="ext_block", next_block_label=None, data_extraction_goal="extract"),
        ]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "ext_block"
        assert blocks[1].next_block_label is None

    def test_duplicate_labels_does_not_crash(self) -> None:
        """Duplicate labels should not crash; last occurrence wins in the dict."""
        blocks = [_nav("a", "b"), _nav("b"), _nav("b")]  # "b" appears twice
        _repair_next_block_label_chain(blocks)
        # The repair should still produce a chain (last "b" wins in the dict)
        assert blocks[0].next_block_label == "b"


class TestConditionalBlocks:
    """Tests for chain repair with conditional blocks."""

    def test_conditional_branch_targets_not_orphaned(self) -> None:
        """Blocks reachable via conditional branches are not orphaned."""
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label="merge_point",
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="branch_a",
                ),
                BranchConditionYAML(is_default=True, next_block_label="branch_b"),
            ],
        )
        blocks = [
            cond,
            _nav("branch_a", "merge_point"),
            _nav("branch_b", "merge_point"),
            _nav("merge_point"),
        ]
        _repair_next_block_label_chain(blocks)
        # All blocks should already be reachable; no stitching needed.
        assert blocks[0].next_block_label == "merge_point"
        assert blocks[1].next_block_label == "merge_point"
        assert blocks[2].next_block_label == "merge_point"
        assert blocks[3].next_block_label is None

    def test_conditional_with_orphaned_block(self) -> None:
        """An extra block not reachable from any branch or main chain gets stitched."""
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label=None,
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="branch_a",
                ),
                BranchConditionYAML(is_default=True, next_block_label="branch_a"),
            ],
        )
        blocks = [cond, _nav("branch_a"), _nav("orphan")]
        _repair_next_block_label_chain(blocks)
        # The conditional's main chain ends at cond (next_block_label was None).
        # orphan should be stitched after cond.
        assert blocks[0].next_block_label == "orphan"
        assert blocks[2].next_block_label is None


class TestCollectReachable:
    """Tests for _collect_reachable helper."""

    def test_linear_chain(self) -> None:
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c")]
        label_to_block = {b.label: b for b in blocks}
        reachable: set[str] = set()
        _collect_reachable("a", label_to_block, reachable)
        assert reachable == {"a", "b", "c"}

    def test_stops_at_missing_label(self) -> None:
        blocks = [_nav("a", "missing")]
        label_to_block = {b.label: b for b in blocks}
        reachable: set[str] = set()
        _collect_reachable("a", label_to_block, reachable)
        assert reachable == {"a"}


class TestFindTerminalLabel:
    """Tests for _find_terminal_label helper."""

    def test_terminal_at_end(self) -> None:
        blocks = [_nav("a", "b"), _nav("b")]
        label_to_block = {b.label: b for b in blocks}
        assert _find_terminal_label("a", label_to_block, {"a", "b"}) == "b"

    def test_terminal_with_dangling_ref(self) -> None:
        blocks = [_nav("a", "missing")]
        label_to_block = {b.label: b for b in blocks}
        assert _find_terminal_label("a", label_to_block, {"a"}) == "a"


class TestOrderOrphanedBlocks:
    """Tests for _order_orphaned_blocks helper."""

    def test_preserves_chain_order(self) -> None:
        blocks = [_nav("a"), _nav("x", "y"), _nav("y")]
        label_to_block = {b.label: b for b in blocks}
        ordered = _order_orphaned_blocks({"x", "y"}, label_to_block, {"a", "x", "y"}, blocks)
        assert ordered == ["x", "y"]

    def test_multiple_chain_starts(self) -> None:
        blocks = [_nav("a"), _nav("p"), _nav("q")]
        label_to_block = {b.label: b for b in blocks}
        ordered = _order_orphaned_blocks({"p", "q"}, label_to_block, {"a", "p", "q"}, blocks)
        assert ordered == ["p", "q"]
        # After ordering, they should be linked
        assert label_to_block["p"].next_block_label == "q"
        assert label_to_block["q"].next_block_label is None


class TestBreakCycles:
    """Tests for _break_cycles helper."""

    def test_no_cycle(self) -> None:
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is False
        # Chain unchanged
        assert label_to_block["a"].next_block_label == "b"
        assert label_to_block["b"].next_block_label == "c"
        assert label_to_block["c"].next_block_label is None

    def test_simple_cycle(self) -> None:
        """A -> B -> C -> A becomes A -> B -> C (terminal)."""
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c", "a")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is True
        assert label_to_block["c"].next_block_label is None
        assert label_to_block["a"].next_block_label == "b"
        assert label_to_block["b"].next_block_label == "c"

    def test_two_block_cycle(self) -> None:
        """A -> B -> A becomes A -> B (terminal)."""
        blocks = [_nav("a", "b"), _nav("b", "a")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is True
        assert label_to_block["a"].next_block_label == "b"
        assert label_to_block["b"].next_block_label is None

    def test_self_referencing_block(self) -> None:
        """A -> A becomes A (terminal)."""
        blocks = [_nav("a", "a")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is True
        assert label_to_block["a"].next_block_label is None

    def test_cycle_mid_chain(self) -> None:
        """A -> B -> C -> B (cycle back to B, not to start)."""
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c", "b")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is True
        assert label_to_block["a"].next_block_label == "b"
        assert label_to_block["b"].next_block_label == "c"
        assert label_to_block["c"].next_block_label is None

    def test_cycle_through_conditional_branch(self) -> None:
        """Conditional branch leads to a chain that cycles back to an ancestor.

        fetch_token -> cond (branch -> nav -> reporting -> fetch_token)
        The cycle through the branch should be broken at reporting.
        """
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label=None,
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="nav",
                ),
                BranchConditionYAML(is_default=True, next_block_label="nav"),
            ],
        )
        blocks = [_nav("fetch_token", "cond"), cond, _nav("nav", "reporting"), _nav("reporting", "fetch_token")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("fetch_token", label_to_block) is True
        # The back-edge reporting -> fetch_token should be broken
        assert label_to_block["reporting"].next_block_label is None
        # Rest of chain intact
        assert label_to_block["fetch_token"].next_block_label == "cond"
        assert label_to_block["nav"].next_block_label == "reporting"

    def test_multiple_branch_cycles_all_broken(self) -> None:
        """Multiple conditional branches each cycle back — all should be broken.

        Reproduces the AllianceHealth pattern:
        fetch_token -> cond (branch1 -> b1 -> fetch_token,
                             branch2 -> b2 -> fetch_token,
                             default -> b3 -> fetch_token)
        """
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label=None,
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ x == 1 }}"),
                    next_block_label="b1",
                ),
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ x == 0 }}"),
                    next_block_label="b2",
                ),
                BranchConditionYAML(is_default=True, next_block_label="b3"),
            ],
        )
        blocks = [
            _nav("fetch_token", "cond"),
            cond,
            _nav("b1", "fetch_token"),
            _nav("b2", "fetch_token"),
            _nav("b3", "fetch_token"),
        ]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("fetch_token", label_to_block) is True
        # All three back-edges should be broken
        assert label_to_block["b1"].next_block_label is None
        assert label_to_block["b2"].next_block_label is None
        assert label_to_block["b3"].next_block_label is None

    def test_branch_directly_cycles_to_ancestor(self) -> None:
        """A branch's next_block_label directly points to an ancestor.

        A -> cond (branch -> A): the branch itself is the back-edge.
        """
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label=None,
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="a",
                ),
                BranchConditionYAML(is_default=True, next_block_label="a"),
            ],
        )
        blocks = [_nav("a", "cond"), cond]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is True
        # The branch's next_block_label should be broken
        assert cond.branch_conditions[0].next_block_label is None

    def test_conditional_merge_point_not_broken(self) -> None:
        """Branches converging on a merge point should NOT be treated as cycles.

        A -> cond (branch1 -> B -> D, branch2 -> C -> D, main -> D)
        D is a merge point, not a cycle target.
        """
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label="d",
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="b",
                ),
                BranchConditionYAML(is_default=True, next_block_label="c"),
            ],
        )
        blocks = [_nav("a", "cond"), cond, _nav("b", "d"), _nav("c", "d"), _nav("d")]
        label_to_block = {b.label: b for b in blocks}
        assert _break_cycles("a", label_to_block) is False
        # All pointers intact
        assert label_to_block["b"].next_block_label == "d"
        assert label_to_block["c"].next_block_label == "d"
        assert cond.next_block_label == "d"


class TestRepairChainWithCycles:
    """Tests for _repair_next_block_label_chain handling circular references."""

    def test_full_cycle_all_blocks(self) -> None:
        """A -> B -> C -> A: cycle broken, all blocks remain in chain."""
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c", "a")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label is None

    def test_cycle_with_orphaned_block(self) -> None:
        """A -> B -> A (cycle) + C (orphaned): cycle broken, C stitched after B."""
        blocks = [_nav("a", "b"), _nav("b", "a"), _nav("c")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label is None

    def test_cycle_with_orphaned_subchain(self) -> None:
        """A -> B -> C -> A (cycle) + X -> Y (orphaned): cycle broken, X -> Y stitched."""
        blocks = [_nav("a", "b"), _nav("b", "c"), _nav("c", "a"), _nav("x", "y"), _nav("y")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "b"
        assert blocks[1].next_block_label == "c"
        assert blocks[2].next_block_label == "x"
        assert blocks[3].next_block_label == "y"
        assert blocks[4].next_block_label is None


def _loop(label: str, next_label: str | None, loop_blocks: list) -> ForLoopBlockYAML:
    return ForLoopBlockYAML(label=label, next_block_label=next_label, loop_blocks=loop_blocks)


class TestNestedForLoopRepair:
    """Tests for recursive repair inside ForLoopBlockYAML.loop_blocks."""

    def test_loop_blocks_orphan_stitched(self) -> None:
        """Orphaned blocks inside loop_blocks are stitched."""
        inner = [_nav("l_a", "l_b"), _nav("l_b"), _nav("l_c")]
        blocks = [_loop("loop", None, inner)]
        _repair_next_block_label_chain(blocks)
        assert inner[0].next_block_label == "l_b"
        assert inner[1].next_block_label == "l_c"
        assert inner[2].next_block_label is None

    def test_loop_blocks_cycle_broken(self) -> None:
        """Cycles inside loop_blocks are broken."""
        inner = [_nav("l_a", "l_b"), _nav("l_b", "l_a")]
        blocks = [_loop("loop", None, inner)]
        _repair_next_block_label_chain(blocks)
        assert inner[0].next_block_label == "l_b"
        assert inner[1].next_block_label is None

    def test_loop_blocks_cycle_and_orphan(self) -> None:
        """Cycle + orphan inside loop_blocks: cycle broken, orphan stitched."""
        inner = [_nav("l_a", "l_b"), _nav("l_b", "l_a"), _nav("l_c")]
        blocks = [_loop("loop", None, inner)]
        _repair_next_block_label_chain(blocks)
        assert inner[0].next_block_label == "l_b"
        assert inner[1].next_block_label == "l_c"
        assert inner[2].next_block_label is None

    def test_top_level_and_loop_both_repaired(self) -> None:
        """Both top-level chain AND loop_blocks are repaired."""
        inner = [_nav("l_x"), _nav("l_y")]  # l_y orphaned
        blocks = [
            _nav("a", "loop"),
            _loop("loop", None, inner),
            _nav("orphan"),  # top-level orphan
        ]
        _repair_next_block_label_chain(blocks)
        # Top-level: loop -> orphan stitched
        assert blocks[1].next_block_label == "orphan"
        assert blocks[2].next_block_label is None
        # Inner: l_x -> l_y stitched
        assert inner[0].next_block_label == "l_y"
        assert inner[1].next_block_label is None

    def test_nested_loop_inside_loop(self) -> None:
        """ForLoop inside a ForLoop — inner-inner loop_blocks also repaired."""
        inner_inner = [_nav("ii_a"), _nav("ii_b")]  # ii_b orphaned
        inner_loop = _loop("inner_loop", None, inner_inner)
        inner = [_nav("i_a", "inner_loop"), inner_loop]
        blocks = [_loop("outer_loop", None, inner)]
        _repair_next_block_label_chain(blocks)
        # inner_inner repaired
        assert inner_inner[0].next_block_label == "ii_b"
        assert inner_inner[1].next_block_label is None

    def test_single_block_in_loop_no_crash(self) -> None:
        """Single block inside loop_blocks — no repair needed, no crash."""
        inner = [_nav("only")]
        blocks = [_loop("loop", None, inner)]
        _repair_next_block_label_chain(blocks)
        assert inner[0].next_block_label is None

    def test_empty_loop_blocks_no_crash(self) -> None:
        """Empty loop_blocks — no crash."""
        blocks = [_loop("loop", None, [])]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label is None

    def test_alliance_health_pattern_with_loop(self) -> None:
        """Real-world pattern: conditional inside top-level, for_loop with cycles inside loop_blocks.

        Reproduces the AllianceHealth workflow structure where loop_blocks contain
        blocks that cycle back within the loop.
        """
        # Inner loop blocks: nav -> extract -> reporting -> nav (cycle)
        inner = [
            _nav("nav_details", "extract_locations"),
            ExtractionBlockYAML(
                label="extract_locations",
                next_block_label="reporting",
                data_extraction_goal="extract",
            ),
            NavigationBlockYAML(
                label="reporting",
                next_block_label="nav_details",  # cycle back!
                navigation_goal="report",
            ),
        ]
        loop_block = _loop("iterate_results", None, inner)
        blocks = [
            _nav("fetch_token", "navigate_search"),
            _nav("navigate_search", "extract_results"),
            ExtractionBlockYAML(
                label="extract_results",
                next_block_label="iterate_results",
                data_extraction_goal="extract count",
            ),
            loop_block,
        ]
        _repair_next_block_label_chain(blocks)
        # Top-level chain should be intact (no issues there)
        assert blocks[0].next_block_label == "navigate_search"
        assert blocks[1].next_block_label == "extract_results"
        assert blocks[2].next_block_label == "iterate_results"
        assert blocks[3].next_block_label is None
        # Inner loop: cycle should be broken
        assert inner[0].next_block_label == "extract_locations"
        assert inner[1].next_block_label == "reporting"
        assert inner[2].next_block_label is None  # cycle broken
