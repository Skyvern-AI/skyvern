"""Tests that nested for-loop child blocks are recursively tracked for caching.

When a ForLoopBlock completes, ALL descendant blocks (not just direct children)
that are cacheable and not yet cached should be added to blocks_to_update.

This is critical for workflows which have double-nested for-loops:
  loop → inner_loop → download_files (file_download)

Without recursive tracking, the inner for-loop's children never get cached
script functions, and every iteration runs the full AI agent loop.

Ref: SKY-8684
"""

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import (
    ExtractionBlock,
    FileDownloadBlock,
    ForLoopBlock,
    HttpRequestBlock,
    NavigationBlock,
    UrlBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import (
    _collect_uncached_loop_children,
)


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


# ---------------------------------------------------------------------------
# Part 1: Core recursive collection behavior
# ---------------------------------------------------------------------------
class TestCollectUncachedLoopChildren:
    """Test _collect_uncached_loop_children with various nesting depths."""

    def test_single_level_collects_direct_children(self) -> None:
        """Single for-loop with cacheable children should collect them all."""
        download = FileDownloadBlock(
            label="download_file",
            output_parameter=_make_output_param("download_file"),
            url="http://example.com",
            navigation_goal="Download the file",
        )
        nav = NavigationBlock(
            label="navigate",
            output_parameter=_make_output_param("navigate"),
            url="http://example.com",
            navigation_goal="Navigate",
        )
        loop = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[nav, download],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(loop, script_blocks_by_label, result)

        assert result == {"navigate", "download_file"}

    def test_double_nested_collects_grandchildren(self) -> None:
        """Double-nested for-loop should collect children at ALL levels."""
        download = FileDownloadBlock(
            label="download_files",
            output_parameter=_make_output_param("download_files"),
            url="http://example.com",
            navigation_goal="Download",
        )
        nav = NavigationBlock(
            label="navigate_to_page",
            output_parameter=_make_output_param("navigate_to_page"),
            url="http://example.com",
            navigation_goal="Navigate",
        )
        extract = ExtractionBlock(
            label="extract_docs",
            output_parameter=_make_output_param("extract_docs"),
            data_extraction_goal="Extract documents",
        )
        inner_loop = ForLoopBlock(
            label="inner_loop",
            output_parameter=_make_output_param("inner_loop"),
            loop_blocks=[nav, extract, download],
        )
        outer_loop = ForLoopBlock(
            label="outer_loop",
            output_parameter=_make_output_param("outer_loop"),
            loop_blocks=[inner_loop],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, result)

        assert result == {"inner_loop", "navigate_to_page", "extract_docs", "download_files"}

    def test_triple_nested_collects_all_descendants(self) -> None:
        """Three levels of nesting should still collect everything."""
        deep_download = FileDownloadBlock(
            label="deep_download",
            output_parameter=_make_output_param("deep_download"),
            url="http://example.com",
            navigation_goal="Download",
        )
        inner_inner = ForLoopBlock(
            label="inner_inner_loop",
            output_parameter=_make_output_param("inner_inner_loop"),
            loop_blocks=[deep_download],
        )
        inner = ForLoopBlock(
            label="inner_loop",
            output_parameter=_make_output_param("inner_loop"),
            loop_blocks=[inner_inner],
        )
        outer = ForLoopBlock(
            label="outer_loop",
            output_parameter=_make_output_param("outer_loop"),
            loop_blocks=[inner],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(outer, script_blocks_by_label, result)

        assert result == {"inner_loop", "inner_inner_loop", "deep_download"}


# ---------------------------------------------------------------------------
# Part 2: Edge cases and guard conditions
# ---------------------------------------------------------------------------
class TestCollectUncachedLoopChildrenEdgeCases:
    """Edge cases: already-cached, non-cacheable types, missing labels."""

    def test_already_cached_children_not_collected(self) -> None:
        """Children already in script_blocks_by_label should be skipped."""
        download = FileDownloadBlock(
            label="download_file",
            output_parameter=_make_output_param("download_file"),
            url="http://example.com",
            navigation_goal="Download",
        )
        nav = NavigationBlock(
            label="navigate",
            output_parameter=_make_output_param("navigate"),
            url="http://example.com",
            navigation_goal="Navigate",
        )
        inner_loop = ForLoopBlock(
            label="inner_loop",
            output_parameter=_make_output_param("inner_loop"),
            loop_blocks=[nav, download],
        )
        outer_loop = ForLoopBlock(
            label="outer_loop",
            output_parameter=_make_output_param("outer_loop"),
            loop_blocks=[inner_loop],
        )

        script_blocks_by_label = {"download_file": object(), "inner_loop": object()}
        result: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, result)

        # Only navigate should be collected
        assert result == {"navigate"}

    def test_already_cached_forloop_still_recursed_into(self) -> None:
        """Even if the inner ForLoopBlock is already cached, its children should still be checked.

        The inner for-loop itself may be cached but its children may not be.
        """
        download = FileDownloadBlock(
            label="download_file",
            output_parameter=_make_output_param("download_file"),
            url="http://example.com",
            navigation_goal="Download",
        )
        inner_loop = ForLoopBlock(
            label="inner_loop",
            output_parameter=_make_output_param("inner_loop"),
            loop_blocks=[download],
        )
        outer_loop = ForLoopBlock(
            label="outer_loop",
            output_parameter=_make_output_param("outer_loop"),
            loop_blocks=[inner_loop],
        )

        # inner_loop is cached, but download_file is NOT
        script_blocks_by_label = {"inner_loop": object()}
        result: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, result)

        # download_file should still be collected even though its parent for-loop is cached
        assert result == {"download_file"}

    def test_non_cacheable_block_types_skipped(self) -> None:
        """http_request is NOT in BLOCK_TYPES_THAT_SHOULD_BE_CACHED — should be skipped."""
        http_block = HttpRequestBlock(
            label="send_webhook",
            output_parameter=_make_output_param("send_webhook"),
            url="http://example.com/webhook",
            method="POST",
        )
        download = FileDownloadBlock(
            label="download_file",
            output_parameter=_make_output_param("download_file"),
            url="http://example.com",
            navigation_goal="Download",
        )
        loop = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[http_block, download],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(loop, script_blocks_by_label, result)

        assert "send_webhook" not in result
        assert result == {"download_file"}

    def test_children_without_label_skipped(self) -> None:
        """Blocks with an empty label should be silently skipped (falsy check)."""
        download = FileDownloadBlock(
            label="",
            output_parameter=_make_output_param("download_file"),
            url="http://example.com",
            navigation_goal="Download",
        )
        loop = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[download],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(loop, script_blocks_by_label, result)

        assert result == set()

    def test_empty_loop_blocks(self) -> None:
        """ForLoopBlock with no children should produce no results."""
        loop = ForLoopBlock(
            label="empty_loop",
            output_parameter=_make_output_param("empty_loop"),
            loop_blocks=[],
        )

        script_blocks_by_label: dict[str, object] = {}
        result: set[str] = set()
        _collect_uncached_loop_children(loop, script_blocks_by_label, result)

        assert result == set()


# ---------------------------------------------------------------------------
# Part 3: Proof tests — recreate real-world workflow structures
# These prove the fix works for production workflow patterns we identified.
# ---------------------------------------------------------------------------
class TestProductionWorkflowProof:
    """Recreate production workflow block structures from investigation.

    These tests use representative block labels and types to prove
    the recursive fix handles each workflow correctly. If these pass, we have
    confidence the fix will work in production.
    """

    def test_nested_download_loop(self) -> None:
        """High-volume nested download workflow pattern (SKY-8684).

        Structure:
          0: go_to_target_website (navigation)
          1: links_extraction (extraction)
          2: loop (for_loop)
             └── inner_loop_starts (for_loop)            ← NESTED
                 ├── navigate_to_specific_page (navigation)
                 ├── Extract_documents (extraction)
                 ├── extract_current_url (extraction)
                 └── download_files (file_download)
          3: merge_identifiers (extraction)
        """
        # Build the inner loop children
        navigate = NavigationBlock(
            label="navigate_to_specific_page",
            output_parameter=_make_output_param("navigate_to_specific_page"),
            url="{{website_url}}",
            navigation_goal="Navigate to page",
        )
        extract_docs = ExtractionBlock(
            label="Extract_documents",
            output_parameter=_make_output_param("Extract_documents"),
            data_extraction_goal="Extract documents",
        )
        extract_url = ExtractionBlock(
            label="extract_current_url",
            output_parameter=_make_output_param("extract_current_url"),
            data_extraction_goal="Extract current URL",
        )
        download = FileDownloadBlock(
            label="download_files",
            output_parameter=_make_output_param("download_files"),
            url="{{website_url}}",
            navigation_goal="Download files",
        )

        # Build nested for-loops
        inner_loop = ForLoopBlock(
            label="inner_loop_starts",
            output_parameter=_make_output_param("inner_loop_starts"),
            loop_blocks=[navigate, extract_docs, extract_url, download],
        )
        outer_loop = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[inner_loop],
        )

        # Nothing cached yet (first run)
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, blocks_to_update)

        # ALL 5 blocks should be collected
        assert blocks_to_update == {
            "inner_loop_starts",
            "navigate_to_specific_page",
            "Extract_documents",
            "extract_current_url",
            "download_files",
        }

    def test_pagination_download_loop(self) -> None:
        """Pagination + download nested loop pattern (SKY-8684).

        Structure:
          3: pagination_loop_starts (for_loop)
             ├── Extract_documents (extraction)
             ├── get_current_url (extraction)
             ├── document_navigation_loop (for_loop)      ← NESTED
             │   ├── navigate_to_document (navigation)
             │   ├── download (file_download)
             │   └── go_back (goto_url)                   ← NOT cacheable
             └── navigate_table_pagination (navigation)
        """
        nav_doc = NavigationBlock(
            label="navigate_to_document",
            output_parameter=_make_output_param("navigate_to_document"),
            url="{{url}}",
            navigation_goal="Navigate to document",
        )
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="{{url}}",
            navigation_goal="Download",
        )
        # goto_url (UrlBlock) is NOT in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        go_back = UrlBlock(
            label="go_back",
            output_parameter=_make_output_param("go_back"),
            url="{{url}}",
        )
        inner_loop = ForLoopBlock(
            label="document_navigation_loop",
            output_parameter=_make_output_param("document_navigation_loop"),
            loop_blocks=[nav_doc, download, go_back],
        )

        extract_docs = ExtractionBlock(
            label="Extract_documents",
            output_parameter=_make_output_param("Extract_documents"),
            data_extraction_goal="Extract documents",
        )
        get_url = ExtractionBlock(
            label="get_current_url",
            output_parameter=_make_output_param("get_current_url"),
            data_extraction_goal="Get current URL",
        )
        nav_page = NavigationBlock(
            label="navigate_table_pagination",
            output_parameter=_make_output_param("navigate_table_pagination"),
            url="{{url}}",
            navigation_goal="Next page",
        )
        outer_loop = ForLoopBlock(
            label="pagination_loop_starts",
            output_parameter=_make_output_param("pagination_loop_starts"),
            loop_blocks=[extract_docs, get_url, inner_loop, nav_page],
        )

        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, blocks_to_update)

        # go_back (goto_url) should NOT be collected — not in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
        assert "go_back" not in blocks_to_update

        # All cacheable blocks should be collected
        assert blocks_to_update == {
            "Extract_documents",
            "get_current_url",
            "document_navigation_loop",
            "navigate_to_document",
            "download",
            "navigate_table_pagination",
        }

    def test_page_document_loop(self) -> None:
        """Page-then-document nested loop pattern (SKY-8684).

        Structure:
          3: for_each_page (for_loop)
             ├── Extract_documents (extraction)
             ├── for_each_document (for_loop)              ← NESTED
             │   ├── download (file_download)
             │   └── navigate_table_pagination (navigation)
        """
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="{{url}}",
            navigation_goal="Download",
        )
        nav_page = NavigationBlock(
            label="navigate_table_pagination",
            output_parameter=_make_output_param("navigate_table_pagination"),
            url="{{url}}",
            navigation_goal="Next page",
        )
        inner_loop = ForLoopBlock(
            label="for_each_document",
            output_parameter=_make_output_param("for_each_document"),
            loop_blocks=[download, nav_page],
        )
        extract = ExtractionBlock(
            label="Extract_documents",
            output_parameter=_make_output_param("Extract_documents"),
            data_extraction_goal="Extract documents",
        )
        outer_loop = ForLoopBlock(
            label="for_each_page",
            output_parameter=_make_output_param("for_each_page"),
            loop_blocks=[extract, inner_loop],
        )

        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()
        _collect_uncached_loop_children(outer_loop, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == {
            "Extract_documents",
            "for_each_document",
            "download",
            "navigate_table_pagination",
        }

    def test_single_level_reference(self) -> None:
        """Single-level nesting reference case — already works, regression guard.

        This already works today. Proves our recursive fix doesn't break
        the simple case.

        Structure:
          for_each_file (for_loop)
          └── download (file_download)
        """
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="{{url}}",
            navigation_goal="Download",
        )
        loop = ForLoopBlock(
            label="for_each_file",
            output_parameter=_make_output_param("for_each_file"),
            loop_blocks=[download],
        )

        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()
        _collect_uncached_loop_children(loop, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == {"download"}


# ---------------------------------------------------------------------------
# Part 4: Integration — simulate _execute_single_block tracking conditions
# ---------------------------------------------------------------------------
class TestExecuteSingleBlockTrackingConditions:
    """Simulate the tracking conditions from _execute_single_block.

    These tests recreate the conditional logic from service.py lines ~2347-2390
    to verify that _collect_uncached_loop_children would be called correctly
    when integrated. This follows the same pattern as
    test_script_generation_race_condition.py::TestFinalizeParameter.
    """

    def test_forloop_with_adaptive_caching_triggers_collection(self) -> None:
        """When adaptive caching is on and ForLoopBlock completes, children should be collected."""
        download = FileDownloadBlock(
            label="download_files",
            output_parameter=_make_output_param("download_files"),
            url="http://example.com",
            navigation_goal="Download",
        )
        inner_loop = ForLoopBlock(
            label="inner_loop",
            output_parameter=_make_output_param("inner_loop"),
            loop_blocks=[download],
        )
        block = ForLoopBlock(
            label="outer_loop",
            output_parameter=_make_output_param("outer_loop"),
            loop_blocks=[inner_loop],
        )

        is_adaptive_caching_active = True
        is_script_run = False
        block_status_cacheable = True
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()

        if isinstance(block, ForLoopBlock) and (is_adaptive_caching_active or is_script_run) and block_status_cacheable:
            _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)

        assert "inner_loop" in blocks_to_update
        assert "download_files" in blocks_to_update

    def test_forloop_with_script_run_triggers_collection(self) -> None:
        """When is_script_run is True, children should be collected."""
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="http://example.com",
            navigation_goal="Download",
        )
        block = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[download],
        )

        is_adaptive_caching_active = False
        is_script_run = True
        block_status_cacheable = True
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()

        if isinstance(block, ForLoopBlock) and (is_adaptive_caching_active or is_script_run) and block_status_cacheable:
            _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == {"download"}

    def test_forloop_without_caching_or_script_does_not_trigger(self) -> None:
        """When neither adaptive caching nor script run is active, no collection."""
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="http://example.com",
            navigation_goal="Download",
        )
        block = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[download],
        )

        is_adaptive_caching_active = False
        is_script_run = False
        block_status_cacheable = True
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()

        if isinstance(block, ForLoopBlock) and (is_adaptive_caching_active or is_script_run) and block_status_cacheable:
            _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == set()

    def test_forloop_with_non_cacheable_status_does_not_trigger(self) -> None:
        """When block status is not cacheable (e.g., failed), no collection."""
        download = FileDownloadBlock(
            label="download",
            output_parameter=_make_output_param("download"),
            url="http://example.com",
            navigation_goal="Download",
        )
        block = ForLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[download],
        )

        is_adaptive_caching_active = True
        is_script_run = True
        block_status_cacheable = False
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()

        if isinstance(block, ForLoopBlock) and (is_adaptive_caching_active or is_script_run) and block_status_cacheable:
            _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == set()

    def test_non_forloop_block_does_not_trigger(self) -> None:
        """Non-ForLoopBlock blocks should not trigger collection."""
        block = NavigationBlock(
            label="navigate",
            output_parameter=_make_output_param("navigate"),
            url="http://example.com",
            navigation_goal="Navigate",
        )

        is_adaptive_caching_active = True
        is_script_run = True
        block_status_cacheable = True
        script_blocks_by_label: dict[str, object] = {}
        blocks_to_update: set[str] = set()

        if isinstance(block, ForLoopBlock) and (is_adaptive_caching_active or is_script_run) and block_status_cacheable:
            _collect_uncached_loop_children(block, script_blocks_by_label, blocks_to_update)

        assert blocks_to_update == set()
