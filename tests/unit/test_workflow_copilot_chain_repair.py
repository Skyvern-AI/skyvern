"""Tests for the copilot next_block_label chain repair logic.

Ensures that _repair_next_block_label_chain stitches orphaned blocks back into the
reachable chain, without chaining the blocks a conditional's branches reach.
"""

from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.workflow_yaml import (
    _break_cycles,
    _collect_reachable,
    _find_terminal_label,
    _normalize_copilot_yaml,
    _normalize_copilot_yaml_with_dropped_links,
    _order_orphaned_blocks,
    _repair_next_block_label_chain,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import (
    BranchConditionYAML,
    BranchCriteriaYAML,
    ConditionalBlockYAML,
    ExtractionBlockYAML,
    ForLoopBlockYAML,
    NavigationBlockYAML,
    WorkflowCreateYAMLRequest,
)


def _nav(label: str, next_label: str | None = None) -> NavigationBlockYAML:
    return NavigationBlockYAML(label=label, next_block_label=next_label, navigation_goal="test")


def _loop(label: str, next_label: str | None, loop_blocks: list) -> ForLoopBlockYAML:
    return ForLoopBlockYAML(label=label, next_block_label=next_label, loop_blocks=loop_blocks)


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

    def test_an_unversioned_list_is_left_unstitched_and_revalidates(self) -> None:
        workflow_yaml = """
title: Two steps
workflow_definition:
  parameters: []
  blocks:
  - block_type: navigation
    label: first
    url: https://example.com
    navigation_goal: Open the page.
  - block_type: navigation
    label: second
    navigation_goal: Read the page.
"""
        request = _normalize_copilot_yaml(workflow_yaml)

        assert request.workflow_definition.version == 1
        assert [block.next_block_label for block in request.workflow_definition.blocks] == [None, None]
        WorkflowCreateYAMLRequest.model_validate(request.model_dump(mode="json"))

    def test_a_list_ordered_top_level_stays_unstitched_while_its_loop_body_chains(self) -> None:
        blocks = [_nav("a"), _loop("loop", None, [_nav("x"), _nav("y")]), _nav("b")]

        _repair_next_block_label_chain(blocks, list_ordered=True)

        assert [block.next_block_label for block in blocks] == [None, None, None]
        assert [block.next_block_label for block in blocks[1].loop_blocks] == ["y", None]


class TestConditionalBlocks:
    """Blocks a conditional's branches reach keep their places; the rest of the list is stitched as usual."""

    def test_conditional_workflow_links_the_blocks_before_it_and_saves(self) -> None:
        workflow_yaml = """
title: Release check
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: agent_step
    label: find_release
    prompt: Find the latest release.
  - block_type: code
    label: check_recency
    code: "result = {'is_recent': True}"
  - block_type: conditional
    label: route
    branch_conditions:
    - criteria:
        expression: "{{ check_recency_output.is_recent }}"
      next_block_label: download_release
    - is_default: true
      next_block_label: read_notice
  - block_type: file_download
    label: download_release
    url: https://example.com/releases
    navigation_goal: Download the release archive.
    next_block_label: null
  - block_type: agent_step
    label: read_notice
    prompt: Read the README notice.
    next_block_label: null
"""
        request = _normalize_copilot_yaml(workflow_yaml)
        blocks = request.workflow_definition.blocks
        assert [block.next_block_label for block in blocks] == ["check_recency", "route", None, None, None]
        route = blocks[2]
        assert isinstance(route, ConditionalBlockYAML)
        assert [branch.next_block_label for branch in route.branch_conditions] == ["download_release", "read_notice"]
        WorkflowService().validate_workflow_block_graph(
            convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
        )

    def test_orphan_after_conditional_becomes_its_merge_point(self) -> None:
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
        assert [block.next_block_label for block in blocks] == ["orphan", None, None]

    def test_connected_graph_whose_start_is_not_listed_first_is_left_alone(self) -> None:
        route = ConditionalBlockYAML(
            label="route",
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"), next_block_label="download_release"
                ),
                BranchConditionYAML(is_default=True, next_block_label="read_notice"),
            ],
        )
        blocks = [_nav("download_release"), _nav("read_notice"), _nav("check_recency", "route"), route]
        _repair_next_block_label_chain(blocks)
        assert [block.next_block_label for block in blocks] == [None, None, "route", None]

    def test_conditional_keeps_its_merge_point_when_linked(self) -> None:
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label="merge",
            branch_conditions=[
                BranchConditionYAML(criteria=BranchCriteriaYAML(expression="{{ true }}"), next_block_label="a"),
                BranchConditionYAML(is_default=True, next_block_label="b"),
            ],
        )
        blocks = [_nav("start"), cond, _nav("a", "merge"), _nav("b"), _nav("merge")]
        _repair_next_block_label_chain(blocks)
        assert [block.next_block_label for block in blocks] == ["cond", "merge", "merge", None, None]

    def test_branch_cycle_not_broken(self) -> None:
        cond = ConditionalBlockYAML(
            label="cond",
            next_block_label=None,
            branch_conditions=[
                BranchConditionYAML(
                    criteria=BranchCriteriaYAML(expression="{{ true }}"),
                    next_block_label="a",
                ),
                BranchConditionYAML(is_default=True, next_block_label="b"),
            ],
        )
        blocks = [_nav("a", "cond"), cond, _nav("b", "a")]
        _repair_next_block_label_chain(blocks)
        assert blocks[0].next_block_label == "cond"
        assert blocks[2].next_block_label == "a"
        assert [branch.next_block_label for branch in cond.branch_conditions] == ["a", "b"]

    def test_loop_body_still_repaired_beside_conditional(self) -> None:
        cond = ConditionalBlockYAML(
            label="cond",
            branch_conditions=[BranchConditionYAML(is_default=True, next_block_label="loop")],
        )
        inner = [_nav("l_a"), _nav("l_b")]
        blocks = [cond, _loop("loop", None, inner), _nav("orphan")]
        _repair_next_block_label_chain(blocks)
        assert inner[0].next_block_label == "l_b"
        assert blocks[1].next_block_label is None


def _normalize_and_save_check(workflow_yaml: str) -> tuple[WorkflowCreateYAMLRequest, list[dict]]:
    with capture_logs() as logs:
        request, dropped_links = _normalize_copilot_yaml_with_dropped_links(workflow_yaml)
    assert request.workflow_definition.version == 2
    WorkflowService().validate_workflow_block_graph(
        convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
    )
    assert len([entry for entry in logs if entry["event"] == "Copilot dropped link to unknown block"]) == len(
        dropped_links
    )
    return request, dropped_links


class TestUnknownLinks:
    def test_link_to_a_block_that_does_not_exist_is_dropped(self) -> None:
        request, drops = _normalize_and_save_check("""
title: One step
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: navigation
    label: open_page
    url: https://example.com
    navigation_goal: Open the page.
    next_block_label: never_added
""")
        assert request.workflow_definition.blocks[0].next_block_label is None
        assert [(drop["block"], drop["target"]) for drop in drops] == [("open_page", "never_added")]

    def test_branch_to_a_block_that_does_not_exist_falls_through_to_the_conditionals_next_block(self) -> None:
        request, drops = _normalize_and_save_check("""
title: Branching
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: code
    label: check_flag
    code: "result = {'ready': True}"
    next_block_label: route
  - block_type: conditional
    label: route
    branch_conditions:
    - criteria:
        expression: "{{ check_flag_output.ready }}"
      next_block_label: never_added
    - is_default: true
      next_block_label: read_notice
    next_block_label: wrap_up
  - block_type: navigation
    label: read_notice
    navigation_goal: Read the notice.
    next_block_label: null
  - block_type: navigation
    label: wrap_up
    navigation_goal: Close the page.
    next_block_label: null
""")
        route = request.workflow_definition.blocks[1]
        assert isinstance(route, ConditionalBlockYAML)
        assert [branch.next_block_label for branch in route.branch_conditions] == ["wrap_up", "read_notice"]
        assert route.next_block_label == "wrap_up"
        assert [(drop["block"], drop["target"], drop["branch_index"]) for drop in drops] == [
            ("route", "never_added", 0)
        ]

    def test_loop_body_link_to_a_block_outside_the_loop_is_dropped(self) -> None:
        request, drops = _normalize_and_save_check("""
title: Loop
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: navigation
    label: open_list
    url: https://example.com
    navigation_goal: Open the list.
    next_block_label: each_row
  - block_type: for_loop
    label: each_row
    loop_variable_reference: "{{ open_list_output }}"
    next_block_label: wrap_up
    loop_blocks:
    - block_type: navigation
      label: open_row
      navigation_goal: Open the row.
      next_block_label: read_row
    - block_type: navigation
      label: read_row
      navigation_goal: Read the row.
      next_block_label: wrap_up
  - block_type: navigation
    label: wrap_up
    navigation_goal: Close the page.
    next_block_label: null
""")
        top = request.workflow_definition.blocks
        assert [block.next_block_label for block in top] == ["each_row", "wrap_up", None]
        loop = top[1]
        assert isinstance(loop, ForLoopBlockYAML)
        assert [block.next_block_label for block in loop.loop_blocks] == ["read_row", None]
        assert [(drop["block"], drop["target"]) for drop in drops] == [("read_row", "wrap_up")]

    def test_conditional_with_every_routed_branch_dropped_still_reaches_its_next_block(self) -> None:
        request, drops = _normalize_and_save_check("""
title: Branching
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: code
    label: check_flag
    code: "result = {'ready': True}"
    next_block_label: route
  - block_type: conditional
    label: route
    branch_conditions:
    - criteria:
        expression: "{{ check_flag_output.ready }}"
      next_block_label: never_added
    - is_default: true
      next_block_label: null
    next_block_label: wrap_up
  - block_type: navigation
    label: wrap_up
    navigation_goal: Close the page.
    next_block_label: null
""")
        route = request.workflow_definition.blocks[1]
        assert isinstance(route, ConditionalBlockYAML)
        assert [branch.next_block_label for branch in route.branch_conditions] == ["wrap_up", None]
        assert drops == [{"block": "route", "branch_index": 0, "target": "never_added", "now": "wrap_up"}]

    def test_dropped_branches_take_the_next_block_the_chain_repair_gives_their_conditional(self) -> None:
        request, _ = _normalize_and_save_check("""
title: Branching
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: code
    label: check_flag
    code: "result = {'ready': True}"
    next_block_label: route
  - block_type: conditional
    label: route
    branch_conditions:
    - criteria:
        expression: "{{ check_flag_output.ready }}"
      next_block_label: never_added
    - is_default: true
      next_block_label: also_never_added
    next_block_label: null
  - block_type: navigation
    label: wrap_up
    navigation_goal: Close the page.
    next_block_label: null
""")
        route = request.workflow_definition.blocks[1]
        assert isinstance(route, ConditionalBlockYAML)
        assert route.next_block_label == "wrap_up"
        assert [branch.next_block_label for branch in route.branch_conditions] == ["wrap_up", "wrap_up"]

    def test_dropped_branch_does_not_copy_a_next_block_that_cycle_repair_cuts(self) -> None:
        request, _ = _normalize_and_save_check("""
title: Branching
workflow_definition:
  version: 2
  parameters: []
  blocks:
  - block_type: code
    label: check_flag
    code: "result = {'ready': True}"
    next_block_label: read_page
  - block_type: navigation
    label: read_page
    navigation_goal: Read the page.
    next_block_label: route
  - block_type: conditional
    label: route
    branch_conditions:
    - criteria:
        expression: "{{ check_flag_output.ready }}"
      next_block_label: never_added
    - is_default: true
      next_block_label: wrap_up
    next_block_label: read_page
  - block_type: navigation
    label: wrap_up
    navigation_goal: Close the page.
    next_block_label: null
""")
        route = request.workflow_definition.blocks[2]
        assert isinstance(route, ConditionalBlockYAML)
        assert [branch.next_block_label for branch in route.branch_conditions] == [None, "wrap_up"]


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
