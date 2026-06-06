"""Tests for frontier selection, compact packet shape, and streak guards."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jinja2.sandbox import SandboxedEnvironment

from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_FAILED_TEST_NUDGES,
    POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE,
    POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE,
    _check_enforcement,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    compute_failure_signature,
    update_repeated_failure_state,
)
from skyvern.forge.sdk.copilot.output_utils import (
    sanitize_tool_result_for_llm,
    summarize_tool_result,
)
from skyvern.forge.sdk.copilot.tools import (
    _find_invalidated_labels,
    _frontier_run_size_error,
    _invalidate_verified_state_on_edit,
    _plan_frontier,
    _record_workflow_update_result,
    _referenced_output_labels,
)
from skyvern.forge.sdk.workflow.models.parameter import RESERVED_PARAMETER_KEYS


class _FakeBlock:
    def __init__(self, label: str, block_type: str, config: dict[str, Any] | None = None) -> None:
        self.label = label

        class _BT:
            def __init__(self, value: str) -> None:
                self.value = value

            def __str__(self) -> str:
                return self.value

        self.block_type = _BT(block_type)
        self._config = config or {}
        for key, value in self._config.items():
            setattr(self, key, value)

    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict[str, Any]:
        return {
            "label": self.label,
            "block_type": self.block_type.value,
            **self._config,
        }


class _FakeParameter:
    def __init__(self, key: str, default_value: object = None) -> None:
        self.key = key
        self.default_value = default_value

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"key": self.key, "default_value": self.default_value}


class _FakeDefinition:
    def __init__(self, blocks: list[_FakeBlock], parameters: list[_FakeParameter] | None = None) -> None:
        self.blocks = blocks
        self.parameters = parameters or []


class _FakeWorkflow:
    def __init__(self, definition: _FakeDefinition) -> None:
        self.workflow_definition = definition

    def model_copy(self, *, deep: bool = False) -> _FakeWorkflow:
        return copy.deepcopy(self) if deep else _FakeWorkflow(self.workflow_definition)


class _FakeStream:
    async def is_disconnected(self) -> bool:
        return False

    async def send(self, event: object) -> None:
        return None


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeBrowserState:
    def __init__(self, page: _FakePage | None) -> None:
        self._page = page

    async def get_working_page(self) -> _FakePage | None:
        return self._page


class _FakePersistentSessionsManager:
    def __init__(self, browser_state: _FakeBrowserState | None) -> None:
        self._browser_state = browser_state

    async def get_browser_state(self, session_id: str, organization_id: str) -> _FakeBrowserState | None:
        return self._browser_state


class _FakeFailingPersistentSessionsManager:
    async def get_browser_state(self, session_id: str, organization_id: str) -> _FakeBrowserState | None:
        raise RuntimeError("browser state unavailable")


def _make_ctx(**kwargs: object) -> CopilotContext:
    defaults: dict[str, Any] = dict(
        organization_id="org",
        workflow_id="wf_id",
        workflow_permanent_id="wpid",
        workflow_yaml="",
        browser_session_id=None,
        stream=_FakeStream(),
    )
    defaults.update(kwargs)
    return CopilotContext(**defaults)


# --------------------------------------------------------------------------- #
# Frontier selection — core behavior                                          #
# --------------------------------------------------------------------------- #


def test_find_invalidated_labels_detects_new_and_changed_and_downstream() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("a", "navigation", {"url": "https://x"}),
            _FakeBlock("b", "extraction", {"prompt": "p1"}),
            _FakeBlock("c", "extraction", {"prompt": "kept"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("a", "navigation", {"url": "https://x"}),
            _FakeBlock("b", "extraction", {"prompt": "p2"}),  # changed
            _FakeBlock("c", "extraction", {"prompt": "kept"}),  # unchanged but downstream
            _FakeBlock("d", "extraction", {"prompt": "new"}),  # new
        ]
    )
    invalidated = _find_invalidated_labels(old, new, ["a", "b", "c", "d"])
    assert "a" not in invalidated
    assert "b" in invalidated
    assert "c" in invalidated  # downstream of invalidated b
    assert "d" in invalidated


def test_plan_frontier_append_after_success_runs_only_appended() -> None:
    old = _FakeDefinition([_FakeBlock("a", "navigation"), _FakeBlock("b", "extraction", {"prompt": "p"})])
    new = _FakeDefinition(
        [
            _FakeBlock("a", "navigation"),
            _FakeBlock("b", "extraction", {"prompt": "p"}),
            _FakeBlock("c", "extraction", {"prompt": "q"}),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["a", "b"]
    ctx.verified_block_outputs = {"a": "nav_ok", "b": {"title": "hi"}}

    labels, _seed, frontier = _plan_frontier(ctx, ["a", "b", "c"], old, new)
    assert labels == ["c"]
    assert frontier == "c"


def test_plan_frontier_append_walks_back_when_workflow_prefix_is_not_verified() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("set_search", "navigation", {"prompt": "Fill search fields"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("set_search", "navigation", {"prompt": "Fill updated search fields"}),
            _FakeBlock("submit_search", "navigation", {"prompt": "Click Search"}),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open"]
    ctx.verified_block_outputs = {"open": "opened"}

    labels, seed, frontier = _plan_frontier(ctx, ["submit_search"], old, new)

    assert labels == ["open", "set_search", "submit_search"]
    assert seed == {}
    assert frontier == "open"


def test_plan_frontier_unchanged_workflow_continues_from_first_unverified_label() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("set_search", "navigation"),
            _FakeBlock("submit_search", "navigation"),
            _FakeBlock("extract", "extraction"),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open", "set_search"]

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open", "set_search", "submit_search", "extract"],
        definition,
        definition,
    )

    assert labels == ["submit_search", "extract"]
    assert seed == {}
    assert frontier == "submit_search"


def test_plan_frontier_verified_only_request_advances_to_next_unverified_workflow_label() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("set_search", "navigation"),
            _FakeBlock("submit_search", "navigation"),
            _FakeBlock("extract", "extraction"),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open", "set_search"]

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open", "set_search"],
        definition,
        definition,
    )

    assert labels == ["submit_search"]
    assert seed == {}
    assert frontier == "submit_search"


def test_plan_frontier_suffix_only_request_seeds_prior_browser_state_outputs() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("search", "navigation"),
            _FakeBlock("expand", "navigation"),
            _FakeBlock("extract", "extraction"),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open", "search"]
    ctx.verified_block_outputs = {
        "open": {"current_url": "https://example.com/search"},
        "search": {"current_url": "https://example.com/search/results"},
    }

    labels, seed, frontier = _plan_frontier(ctx, ["expand"], definition, definition)

    assert labels == ["expand"]
    assert seed == {
        "open": {"current_url": "https://example.com/search"},
        "search": {"current_url": "https://example.com/search/results"},
    }
    assert frontier == "expand"


def test_runtime_frontier_anchor_keeps_url_empty_to_preserve_live_state() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": None}),
            _FakeBlock("extract", "extraction"),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open"]
    ctx.verified_prefix_current_url = "https://example.com/search"

    anchored, anchor_url = tools._workflow_with_runtime_frontier_anchor(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search", "extract"],
        frontier_start_label="search",
        block_outputs_to_seed={},
    )

    assert anchor_url == "https://example.com/search"
    assert anchored is workflow
    assert workflow.workflow_definition.blocks[1].url is None


@pytest.mark.asyncio
async def test_runtime_frontier_starter_url_seed_fills_blank_browser_state(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": None}),
            _FakeBlock("extract", "extraction"),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx(browser_session_id="pbs_123")

    monkeypatch.setattr(
        tools.app,
        "PERSISTENT_SESSIONS_MANAGER",
        _FakePersistentSessionsManager(_FakeBrowserState(_FakePage("about:blank"))),
    )

    seeded = await tools._workflow_with_runtime_frontier_starter_url_seed(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search", "extract"],
        runtime_frontier_anchor_url="https://example.com/search",
    )

    assert seeded is not workflow
    assert seeded.workflow_definition.blocks[1].url == "https://example.com/search"
    assert workflow.workflow_definition.blocks[1].url is None


@pytest.mark.asyncio
async def test_runtime_frontier_starter_url_seed_fills_when_browser_state_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": None}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx(browser_session_id="pbs_123")

    monkeypatch.setattr(
        tools.app,
        "PERSISTENT_SESSIONS_MANAGER",
        _FakeFailingPersistentSessionsManager(),
    )

    seeded = await tools._workflow_with_runtime_frontier_starter_url_seed(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search"],
        runtime_frontier_anchor_url="https://example.com/search",
    )

    assert seeded is not workflow
    assert seeded.workflow_definition.blocks[1].url == "https://example.com/search"
    assert workflow.workflow_definition.blocks[1].url is None


@pytest.mark.asyncio
async def test_runtime_frontier_starter_url_seed_preserves_attached_live_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": None}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx(browser_session_id="pbs_123")

    monkeypatch.setattr(
        tools.app,
        "PERSISTENT_SESSIONS_MANAGER",
        _FakePersistentSessionsManager(_FakeBrowserState(_FakePage("https://example.com/search/results"))),
    )

    seeded = await tools._workflow_with_runtime_frontier_starter_url_seed(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search"],
        runtime_frontier_anchor_url="https://example.com/search",
    )

    assert seeded is workflow
    assert workflow.workflow_definition.blocks[1].url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_url", ["start_url", "{{ start_url }}", "example.com"])
async def test_runtime_frontier_starter_url_seed_preserves_runtime_resolved_url(
    monkeypatch: pytest.MonkeyPatch,
    explicit_url: str,
) -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": explicit_url}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx(browser_session_id="pbs_123")

    monkeypatch.setattr(
        tools.app,
        "PERSISTENT_SESSIONS_MANAGER",
        _FakePersistentSessionsManager(_FakeBrowserState(_FakePage("about:blank"))),
    )

    seeded = await tools._workflow_with_runtime_frontier_starter_url_seed(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search"],
        runtime_frontier_anchor_url="https://example.com/search",
    )

    assert seeded is workflow
    assert workflow.workflow_definition.blocks[1].url == explicit_url


def test_runtime_frontier_anchor_requires_verified_prefix() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": None}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx()
    ctx.verified_prefix_current_url = "https://example.com/search"

    anchored, anchor_url = tools._workflow_with_runtime_frontier_anchor(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search"],
        frontier_start_label="search",
        block_outputs_to_seed={},
    )

    assert anchor_url is None
    assert anchored is workflow
    assert workflow.workflow_definition.blocks[1].url is None


def test_runtime_frontier_anchor_does_not_override_explicit_block_url() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("search", "navigation", {"url": "https://example.com/explicit"}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open"]
    ctx.verified_prefix_current_url = "https://example.com/search"

    anchored, anchor_url = tools._workflow_with_runtime_frontier_anchor(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["search"],
        frontier_start_label="search",
        block_outputs_to_seed={},
    )

    assert anchor_url is None
    assert anchored is workflow
    assert workflow.workflow_definition.blocks[1].url == "https://example.com/explicit"


def test_runtime_frontier_anchor_clears_same_page_url_to_preserve_state() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("set_search", "navigation", {"url": None}),
            _FakeBlock("submit_search", "navigation", {"url": "https://example.com/search"}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open", "set_search"]
    ctx.verified_prefix_current_url = "https://example.com/search"

    anchored, anchor_url = tools._workflow_with_runtime_frontier_anchor(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["submit_search"],
        frontier_start_label="submit_search",
        block_outputs_to_seed={},
    )

    assert anchor_url == "https://example.com/search"
    assert anchored is not workflow
    assert anchored.workflow_definition.blocks[2].url is None
    assert workflow.workflow_definition.blocks[2].url == "https://example.com/search"


def test_runtime_frontier_anchor_does_not_clear_same_page_goto_url() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url", {"url": "https://example.com/search"}),
            _FakeBlock("refresh", "goto_url", {"url": "https://example.com/search"}),
        ]
    )
    workflow = _FakeWorkflow(definition)
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open"]
    ctx.verified_prefix_current_url = "https://example.com/search"

    anchored, anchor_url = tools._workflow_with_runtime_frontier_anchor(
        workflow,  # type: ignore[arg-type]
        ctx,
        labels_to_execute=["refresh"],
        frontier_start_label="refresh",
        block_outputs_to_seed={},
    )

    assert anchor_url is None
    assert anchored is workflow
    assert workflow.workflow_definition.blocks[1].url == "https://example.com/search"


def test_frontier_run_size_error_limits_long_page_changing_frontier() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("set_search", "navigation"),
            _FakeBlock("submit_search", "navigation"),
            _FakeBlock("expand_results", "navigation"),
            _FakeBlock("extract", "extraction"),
        ]
    )
    ctx = _make_ctx()

    error = _frontier_run_size_error(
        ctx,
        ["open", "set_search", "submit_search", "expand_results", "extract"],
        ["open", "set_search", "submit_search", "expand_results", "extract"],
        definition,
    )

    assert error is not None
    assert "Keep the same complete workflow YAML" in error
    assert "['open', 'set_search']" in error
    assert "Do not remove later blocks" in error


def test_frontier_run_size_result_steers_to_smaller_saved_frontier() -> None:
    result = tools._frontier_run_size_result(
        "frontier too long",
        ["open", "set_search", "submit_search", "expand_results", "extract"],
        ["open", "set_search", "submit_search", "expand_results", "extract"],
    )

    data = result["data"]
    assert result["ok"] is False
    assert data["workflow_run_skipped"] is True
    assert data["suggested_block_labels"] == ["open", "set_search"]
    assert data["deferred_block_labels"] == ["submit_search", "expand_results", "extract"]
    assert data["control_signal"] == {
        "kind": "intermediate_success",
        "user_facing_summary": data["user_facing_summary"],
        "next_tool": "run_blocks_and_collect_debug",
        "next_block_labels": ["open", "set_search"],
        "preserve_workflow_yaml": True,
    }


def test_frontier_run_size_error_allows_tool_expanded_runtime_anchor() -> None:
    definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("set_search", "navigation"),
            _FakeBlock("submit_search", "navigation"),
        ]
    )
    ctx = _make_ctx()

    assert (
        _frontier_run_size_error(
            ctx,
            ["submit_search"],
            ["open", "set_search", "submit_search"],
            definition,
        )
        is None
    )


def test_frontier_run_size_error_allows_small_or_single_action_frontiers() -> None:
    single_action_definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("search", "navigation"),
            _FakeBlock("extract", "extraction"),
        ]
    )
    long_read_definition = _FakeDefinition(
        [
            _FakeBlock("open", "goto_url"),
            _FakeBlock("extract_a", "extraction"),
            _FakeBlock("extract_b", "extraction"),
            _FakeBlock("extract_c", "extraction"),
        ]
    )
    ctx = _make_ctx()

    assert _frontier_run_size_error(ctx, ["open", "search"], ["open", "search"], single_action_definition) is None
    assert (
        _frontier_run_size_error(
            ctx,
            ["open", "search", "extract"],
            ["open", "search", "extract"],
            single_action_definition,
        )
        is None
    )
    assert (
        _frontier_run_size_error(
            ctx,
            ["open", "extract_a", "extract_b", "extract_c"],
            ["open", "extract_a", "extract_b", "extract_c"],
            long_read_definition,
        )
        is None
    )


def test_plan_frontier_edit_walks_back_to_upstream_navigation_anchor() -> None:
    # Editing a non-rerunnable block with an upstream navigation: walk back to nav.
    old = _FakeDefinition([_FakeBlock("nav", "navigation"), _FakeBlock("click", "action", {"selector": "#a"})])
    new = _FakeDefinition([_FakeBlock("nav", "navigation"), _FakeBlock("click", "action", {"selector": "#b"})])
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["nav", "click"]
    ctx.verified_block_outputs = {"nav": "ok"}

    labels, _seed, frontier = _plan_frontier(ctx, ["nav", "click"], old, new)
    assert labels == ["nav", "click"]
    assert frontier == "nav"


def test_plan_frontier_edit_read_only_block_still_walks_back_to_anchor() -> None:
    # Even for a read-only block type, we cannot rerun just the edited block
    # because there's no browser-anchor signal. Walk back to the upstream
    # navigation anchor instead.
    old = _FakeDefinition([_FakeBlock("nav", "navigation"), _FakeBlock("extract", "extraction", {"prompt": "old"})])
    new = _FakeDefinition([_FakeBlock("nav", "navigation"), _FakeBlock("extract", "extraction", {"prompt": "new"})])
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["nav", "extract"]
    ctx.verified_block_outputs = {"nav": "ok", "extract": "old_out"}

    labels, _seed, frontier = _plan_frontier(ctx, ["nav", "extract"], old, new)
    assert labels == ["nav", "extract"]
    assert frontier == "nav"


def test_plan_frontier_code_only_edited_data_read_suffix_starts_at_edited_block() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open_lookup_page", "code", {"code": "await page.goto('https://example.com')"}),
            _FakeBlock("search_target_record", "code", {"code": "await page.fill('#q', 'sample target')"}),
            _FakeBlock("expand_matching_results", "code", {"code": "return {'records': ['sample target']}"}),
            _FakeBlock(
                "extract_record_details",
                "code",
                {"code": "return {'source': {{ expand_matching_results_output }}, 'records': []}"},
            ),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_lookup_page", "code", {"code": "await page.goto('https://example.com')"}),
            _FakeBlock("search_target_record", "code", {"code": "await page.fill('#q', 'sample target')"}),
            _FakeBlock("expand_matching_results", "code", {"code": "return {'records': ['sample target']}"}),
            _FakeBlock(
                "extract_record_details",
                "code",
                {"code": "return {'source': {{ expand_matching_results_output }}, 'records': [{'number': '1'}]}"},
            ),
        ]
    )
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    ctx.verified_prefix_labels = ["open_lookup_page", "search_target_record", "expand_matching_results"]
    ctx.verified_block_outputs = {"expand_matching_results": {"records": ["sample target"]}}

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open_lookup_page", "search_target_record", "expand_matching_results", "extract_record_details"],
        old,
        new,
    )

    assert labels == ["extract_record_details"]
    assert seed == {"expand_matching_results": {"records": ["sample target"]}}
    assert frontier == "extract_record_details"


def test_plan_frontier_code_only_edited_page_action_code_walks_back_to_anchor() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open_site", "goto_url", {"url": "https://example.com"}),
            _FakeBlock("submit_search", "code", {"code": "await page.click('#search')"}),
            _FakeBlock("extract_results", "code", {"code": "return {'records': []}"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_site", "goto_url", {"url": "https://example.com"}),
            _FakeBlock("submit_search", "code", {"code": "await page.click('#search-now')"}),
            _FakeBlock("extract_results", "code", {"code": "return {'records': []}"}),
        ]
    )
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    ctx.verified_prefix_labels = ["open_site", "submit_search", "extract_results"]
    ctx.verified_block_outputs = {"open_site": {"current_url": "https://example.com"}}

    labels, seed, frontier = _plan_frontier(ctx, ["open_site", "submit_search", "extract_results"], old, new)

    assert labels == ["open_site", "submit_search", "extract_results"]
    assert seed == {}
    assert frontier == "open_site"


def test_code_only_verified_prefix_rewrite_rejects_downstream_repair_churn() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open_page", "code", {"code": "await page.goto('https://example.com')"}),
            _FakeBlock("search_results", "code", {"code": "await page.fill('#q', 'sample')"}),
            _FakeBlock("extract_results", "code", {"code": "return {'records': []}"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "code", {"code": "await page.goto('https://example.org')"}),
            _FakeBlock("search_results", "code", {"code": "await page.fill('#q', 'sample')"}),
            _FakeBlock("extract_results", "code", {"code": "return {'records': [{'title': 'sample'}]}"}),
        ]
    )
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    ctx.verified_prefix_labels = ["open_page", "search_results"]

    error = tools._code_only_verified_prefix_rewrite_error(
        ctx,
        prior_definition=old,
        new_definition=new,
        requested_labels=["extract_results"],
    )

    assert error is not None
    assert "already-verified upstream blocks: open_page" in error
    assert "`extract_results`" in error


def test_plan_frontier_edit_with_no_upstream_anchor_falls_back_to_full_list() -> None:
    old = _FakeDefinition([_FakeBlock("click", "action", {"selector": "#a"}), _FakeBlock("download", "download_to_s3")])
    new = _FakeDefinition([_FakeBlock("click", "action", {"selector": "#b"}), _FakeBlock("download", "download_to_s3")])
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["click", "download"]
    labels, seed, frontier = _plan_frontier(ctx, ["click", "download"], old, new)
    assert labels == ["click", "download"]
    assert frontier == "click"
    assert seed == {}


def test_plan_frontier_without_verified_prefix_falls_back_to_full() -> None:
    old = _FakeDefinition([_FakeBlock("a", "navigation"), _FakeBlock("b", "extraction")])
    new = _FakeDefinition([_FakeBlock("a", "navigation"), _FakeBlock("b", "extraction", {"prompt": "changed"})])
    ctx = _make_ctx()
    # No verified_prefix_labels — previous run must have failed.
    labels, _seed, frontier = _plan_frontier(ctx, ["a", "b"], old, new)
    assert labels == ["a", "b"]
    assert frontier == "a"


def test_plan_frontier_cold_start_no_old_definition_uses_first_requested() -> None:
    new = _FakeDefinition([_FakeBlock("a", "navigation")])
    ctx = _make_ctx()
    labels, _seed, frontier = _plan_frontier(ctx, ["a"], None, new)
    assert labels == ["a"]
    assert frontier == "a"


def test_plan_frontier_ambiguous_diff_falls_back_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot import tools

    def _blow_up(*args: object, **kwargs: object) -> set[str]:
        raise RuntimeError("parse failure in diff")

    monkeypatch.setattr(tools, "_find_invalidated_labels", _blow_up)

    old = _FakeDefinition([_FakeBlock("a", "navigation")])
    new = _FakeDefinition([_FakeBlock("a", "navigation")])
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["a"]
    labels, seed, frontier = _plan_frontier(ctx, ["a"], old, new)
    assert labels == ["a"]
    assert frontier == "a"
    assert seed == {}


def test_referenced_output_labels_finds_jinja_refs() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock("a", "navigation"),
            _FakeBlock("extract", "extraction", {"prompt": "Use {{ a_output }} to guide extraction"}),
        ]
    )
    refs = _referenced_output_labels(["extract"], new)
    assert "a" in refs


def test_referenced_output_labels_finds_block_form_jinja_refs() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock("extract_article_info", "extraction"),
            _FakeBlock(
                "summarize_article",
                "text_prompt",
                {
                    "prompt": (
                        "Summarize {{ extract_article_info.output.extracted_information.abstract }} "
                        "and {{ extract_article_info.title }}."
                    )
                },
            ),
        ]
    )

    refs = _referenced_output_labels(["summarize_article"], new)

    assert refs == {"extract_article_info"}


def test_plan_frontier_append_with_block_form_jinja_ref_falls_back_to_full_run() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
            _FakeBlock(
                "summarize_article",
                "text_prompt",
                {
                    "prompt": (
                        "Summarize the main findings from "
                        "{{ extract_article_info.output.extracted_information.abstract }}."
                    )
                },
            ),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open_page", "extract_article_info"]
    ctx.verified_block_outputs = {
        "open_page": "nav_ok",
        "extract_article_info": {"extracted_information": {"abstract": "Prior output"}},
    }

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open_page", "extract_article_info", "summarize_article"],
        old,
        new,
    )

    assert labels == ["open_page", "extract_article_info", "summarize_article"]
    assert seed == {}
    assert frontier == "open_page"


def test_plan_frontier_append_seeds_output_parameter_jinja_ref() -> None:
    old = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
            _FakeBlock(
                "summarize_article",
                "text_prompt",
                {
                    "prompt": (
                        "Summarize the main findings from "
                        "{{ extract_article_info_output.extracted_information.abstract }}."
                    )
                },
            ),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open_page", "extract_article_info"]
    ctx.verified_block_outputs = {
        "open_page": "nav_ok",
        "extract_article_info": {"extracted_information": {"abstract": "Prior output"}},
    }

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open_page", "extract_article_info", "summarize_article"],
        old,
        new,
    )

    assert labels == ["summarize_article"]
    assert seed == {
        "open_page": "nav_ok",
        "extract_article_info": {"extracted_information": {"abstract": "Prior output"}},
    }
    assert frontier == "summarize_article"


def test_stale_metadata_detects_corrected_subject_label_and_title() -> None:
    prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: extract_results
      navigation_goal: Search example.com for topic alpha.
    - block_type: extraction
      label: extract_results
      title: Extract Results
      next_block_label: null
      data_extraction_goal: Extract the total number of topic alpha search results.
"""
    submitted_yaml = """
title: Count example.com sample beta results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: extract_results
      navigation_goal: Search example.com for sample beta.
    - block_type: extraction
      label: extract_results
      title: Extract Results
      next_block_label: null
      data_extraction_goal: Extract the total number of sample beta search results.
"""

    stale = tools._detect_stale_block_metadata(submitted_yaml, prior_yaml)

    assert stale == [
        {
            "label": "search_topic_alpha",
            "reasons": [
                "label 'search_topic_alpha' appears stale",
                "title 'Search Topic Alpha' appears stale",
            ],
        }
    ]


def test_stale_metadata_accepts_renamed_corrected_subject() -> None:
    prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: extract_results
      navigation_goal: Search example.com for topic alpha.
    - block_type: extraction
      label: extract_results
      title: Extract Results
      next_block_label: null
      data_extraction_goal: Extract the total number of topic alpha search results.
"""
    submitted_yaml = """
title: Count example.com sample beta results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_sample_beta
      title: Search Sample Beta
      next_block_label: extract_results
      navigation_goal: Search example.com for sample beta.
    - block_type: extraction
      label: extract_results
      title: Extract Results
      next_block_label: null
      data_extraction_goal: Extract the total number of sample beta search results.
"""

    assert tools._detect_stale_block_metadata(submitted_yaml, prior_yaml) == []


def test_stale_metadata_accepts_reworded_action_with_same_subject() -> None:
    prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for topic alpha.
"""
    submitted_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Find example.com pages about topic alpha.
"""

    assert tools._detect_stale_block_metadata(submitted_yaml, prior_yaml) == []


def test_plan_frontier_unknown_jinja_root_falls_back_to_full_requested_list() -> None:
    old = _FakeDefinition([_FakeBlock("open_page", "navigation")])
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("summarize_article", "text_prompt", {"prompt": "Summarize {{ missing_block.abstract }}."}),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open_page"]
    ctx.verified_block_outputs = {"open_page": "nav_ok"}

    labels, seed, frontier = _plan_frontier(ctx, ["open_page", "summarize_article"], old, new)

    assert labels == ["open_page", "summarize_article"]
    assert seed == {}
    assert frontier == "open_page"


def test_plan_frontier_falls_back_when_unknown_root_coexists_with_seedable_ref() -> None:
    # Even when the suffix references a verified upstream output (so seeding
    # would otherwise let us skip the prefix), an additional unknown Jinja
    # root must still trigger the conservative full-rerun fallback.
    old = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("extract_article_info", "extraction", {"prompt": "extract abstract"}),
            _FakeBlock(
                "summarize_article",
                "text_prompt",
                {
                    "prompt": (
                        "Summarize {{ extract_article_info_output.extracted_information.abstract }} "
                        "with context {{ missing_block.note }}."
                    )
                },
            ),
        ]
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open_page", "extract_article_info"]
    ctx.verified_block_outputs = {
        "open_page": "nav_ok",
        "extract_article_info": {"extracted_information": {"abstract": "Prior output"}},
    }

    labels, seed, frontier = _plan_frontier(
        ctx,
        ["open_page", "extract_article_info", "summarize_article"],
        old,
        new,
    )

    assert labels == ["open_page", "extract_article_info", "summarize_article"]
    assert seed == {}
    assert frontier == "open_page"


def test_unknown_jinja_roots_ignores_credential_real_value_synthetic_roots() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock(
                "login",
                "login",
                {"prompt": "Sign in with {{ creds_real_username }} / {{ creds_real_password }}."},
            ),
        ],
        parameters=[_FakeParameter("creds")],
    )

    assert tools._unknown_jinja_roots(["login"], new) == set()


def test_unknown_jinja_roots_ignores_conditional_branch_context_roots() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock(
                "branch",
                "conditional",
                {
                    "expression": (
                        "{{ params.foo }} {{ outputs.bar }} {{ environment.region }} {{ env.flag }} {{ llm.model }}"
                    )
                },
            ),
        ]
    )

    assert tools._unknown_jinja_roots(["branch"], new) == set()


def test_stale_metadata_accepts_single_token_subject_change_as_known_limit() -> None:
    prior_yaml = """
title: Search results page
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_cats
      title: Search Cats
      next_block_label: null
      navigation_goal: Search the directory for cats.
"""
    submitted_yaml = """
title: Search results page
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_cats
      title: Search Cats
      next_block_label: null
      navigation_goal: Search the directory for dogs.
"""

    # The code gate is a conservative backstop: it requires at least two
    # removed metadata tokens before rejecting. Single-token subject swaps are
    # expected to be handled by the prompt instruction to rename changed
    # subject metadata.
    assert tools._detect_stale_block_metadata(submitted_yaml, prior_yaml) == []


def test_stale_metadata_detects_stale_title_after_label_rename() -> None:
    prior_yaml = """
title: Count example.com topic alpha results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_topic_alpha
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for topic alpha.
"""
    submitted_yaml = """
title: Count example.com sample beta results
workflow_definition:
  blocks:
    - block_type: navigation
      label: search_sample_beta
      title: Search Topic Alpha
      next_block_label: null
      navigation_goal: Search example.com for sample beta.
"""

    stale = tools._detect_stale_block_metadata(submitted_yaml, prior_yaml)

    assert stale == [
        {
            "label": "search_sample_beta",
            "reasons": ["title 'Search Topic Alpha' appears stale"],
        }
    ]


def test_stale_metadata_detects_stale_block_inside_loop_blocks() -> None:
    prior_yaml = """
title: For-each search results
workflow_definition:
  blocks:
    - block_type: for_loop
      label: per_topic
      loop_blocks:
        - block_type: navigation
          label: search_topic_alpha
          title: Search Topic Alpha
          next_block_label: null
          navigation_goal: Search example.com for topic alpha.
"""
    submitted_yaml = """
title: For-each search results
workflow_definition:
  blocks:
    - block_type: for_loop
      label: per_topic
      loop_blocks:
        - block_type: navigation
          label: search_topic_alpha
          title: Search Topic Alpha
          next_block_label: null
          navigation_goal: Search example.com for sample beta.
"""

    stale = tools._detect_stale_block_metadata(submitted_yaml, prior_yaml)

    assert {item["label"] for item in stale} == {"search_topic_alpha"}


def test_stale_metadata_message_indicates_truncation_when_over_limit() -> None:
    items = [{"label": f"label_{i}", "reasons": [f"reason {i}"]} for i in range(7)]
    message = tools._stale_block_metadata_message(items)
    assert "and 2 more" in message


def test_stale_metadata_message_omits_truncation_indicator_under_limit() -> None:
    items = [{"label": f"label_{i}", "reasons": [f"reason {i}"]} for i in range(3)]
    message = tools._stale_block_metadata_message(items)
    assert "more" not in message


def test_referenced_output_labels_ignores_non_block_jinja_roots() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock(
                "summarize_article",
                "text_prompt",
                {"prompt": "Summarize {{ search_term.field }} for {{ loop.index }}."},
            ),
        ]
    )

    refs = _referenced_output_labels(["summarize_article"], new)

    assert refs == set()


def test_plan_frontier_append_only_with_workflow_param_does_not_fall_back() -> None:
    old = _FakeDefinition(
        [_FakeBlock("open_page", "navigation")],
        parameters=[_FakeParameter("search_term")],
    )
    new = _FakeDefinition(
        [
            _FakeBlock("open_page", "navigation"),
            _FakeBlock("search", "navigation", {"prompt": "Search for {{ search_term }} on this site"}),
        ],
        parameters=[_FakeParameter("search_term")],
    )
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["open_page"]
    ctx.verified_block_outputs = {"open_page": "nav_ok"}

    labels, seed, frontier = _plan_frontier(ctx, ["open_page", "search"], old, new)

    assert labels == ["search"]
    assert seed == {"open_page": "nav_ok"}
    assert frontier == "search"


def test_template_builtin_roots_track_jinja_and_skyvern_contexts() -> None:
    assert tools._JINJA_RUNTIME_GLOBAL_ROOTS == frozenset(SandboxedEnvironment().globals)
    assert tools._JINJA_RUNTIME_GLOBAL_ROOTS <= tools._TEMPLATE_BUILTIN_ROOTS
    assert tools._JINJA_LITERAL_ROOTS <= tools._TEMPLATE_BUILTIN_ROOTS
    assert tools._JINJA_SPECIAL_CONTEXT_ROOTS <= tools._TEMPLATE_BUILTIN_ROOTS
    assert frozenset(RESERVED_PARAMETER_KEYS) <= tools._SKYVERN_TEMPLATE_CONTEXT_ROOTS
    assert {"parameters", "browser_session_id", "organization_id"} <= tools._SKYVERN_TEMPLATE_CONTEXT_ROOTS
    assert tools._SKYVERN_TEMPLATE_CONTEXT_ROOTS <= tools._TEMPLATE_BUILTIN_ROOTS


def test_unknown_jinja_roots_ignores_jinja_and_skyvern_context_roots() -> None:
    new = _FakeDefinition(
        [
            _FakeBlock(
                "summarize",
                "text_prompt",
                {
                    "prompt": (
                        "{{ range }} {{ dict }} {{ namespace }} {{ cycler }} {{ joiner }} {{ lipsum }} "
                        "{{ none }} {{ true }} {{ false }} {{ loop.index }} {{ self }} {{ varargs }} {{ kwargs }} "
                        "{{ parameters.search_term }} {{ browser_session_id }} {{ organization_id }} "
                        "{{ current_date }} {{ workflow_run_id }}"
                    )
                },
            ),
        ]
    )

    assert tools._unknown_jinja_roots(["summarize"], new) == set()


# --------------------------------------------------------------------------- #
# Compact packet shape                                                        #
# --------------------------------------------------------------------------- #


def test_compact_packet_sanitizer_keeps_new_fields_and_omits_html() -> None:
    raw = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_1",
            "overall_status": "failed",
            "requested_block_labels": ["a", "b"],
            "executed_block_labels": ["b"],
            "frontier_start_label": "b",
            "blocks": [{"label": "b", "block_type": "EXTRACTION", "status": "failed"}],
            "current_url": "https://x",
            "page_title": "t",
            "action_trace_summary": ["click #btn"],
            "screenshot_base64": "aaa",
        },
    }
    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", raw)
    data = sanitized["data"]
    assert "visible_elements_html" not in data
    assert data["screenshot_base64"].startswith("[base64 image omitted")
    assert data["requested_block_labels"] == ["a", "b"]
    assert data["executed_block_labels"] == ["b"]
    assert data["frontier_start_label"] == "b"
    assert data["action_trace_summary"] == ["click #btn"]


def test_summarize_tool_result_reflects_executed_frontier_with_cache_note() -> None:
    result = {
        "ok": True,
        "data": {
            "overall_status": "completed",
            "requested_block_labels": ["a", "b", "c"],
            "executed_block_labels": ["c"],
            "frontier_start_label": "c",
            "blocks": [{"label": "c", "status": "completed"}],
        },
    }
    summary = summarize_tool_result("run_blocks_and_collect_debug", result)
    assert summary.startswith("Run c:")
    assert "completed" in summary
    assert "skipped prefix from cache" in summary


# --------------------------------------------------------------------------- #
# Repeated-failure state + enforcement                                        #
# --------------------------------------------------------------------------- #


def _set_failure_ctx(ctx: CopilotContext, definition: _FakeDefinition, reason: str) -> None:
    ctx.last_workflow = _FakeWorkflow(definition)
    ctx.last_executed_block_labels = [b.label for b in definition.blocks]
    ctx.last_frontier_start_label = definition.blocks[0].label
    ctx.last_test_suspicious_success = False
    ctx.last_test_failure_reason = reason


def test_update_repeated_failure_state_increments_on_same_signature_and_fingerprint() -> None:
    ctx = _make_ctx()
    defn = _FakeDefinition([_FakeBlock("a", "extraction", {"prompt": "p"})])
    _set_failure_ctx(ctx, defn, "Selector not found")

    result = {"ok": False, "data": {"failure_categories": [{"category": "EXTRACTION_FAILURE"}]}}
    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 1
    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 2
    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 3


def test_update_repeated_failure_state_resets_on_fingerprint_change() -> None:
    ctx = _make_ctx()
    d1 = _FakeDefinition([_FakeBlock("a", "extraction", {"prompt": "p1"})])
    d2 = _FakeDefinition([_FakeBlock("a", "extraction", {"prompt": "p2"})])
    result = {"ok": False, "data": {"failure_categories": []}}

    _set_failure_ctx(ctx, d1, "Selector not found")
    update_repeated_failure_state(ctx, result)
    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 2
    # Pre-populate emitted so the reset-to-0 below actually observes the reset
    # rather than a field that was never bumped.
    ctx.repeated_failure_nudge_emitted_at_streak = 2

    _set_failure_ctx(ctx, d2, "Selector not found")
    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 1
    assert ctx.repeated_failure_nudge_emitted_at_streak == 0


def test_update_repeated_failure_state_resets_on_meaningful_success() -> None:
    ctx = _make_ctx()
    defn = _FakeDefinition([_FakeBlock("a", "extraction")])
    _set_failure_ctx(ctx, defn, "Selector not found")

    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_failure_streak_count == 2

    ctx.last_test_failure_reason = None
    ctx.last_test_suspicious_success = False
    update_repeated_failure_state(ctx, {"ok": True, "data": {}})
    assert ctx.repeated_failure_streak_count == 0
    assert ctx.last_failure_signature is None
    assert ctx.repeated_failure_nudge_emitted_at_streak == 0


def test_repeated_failure_end_to_end_flow_1_then_warn_then_stop() -> None:
    """Streak must survive across warn/stop transitions rather than being
    reset by enforcement — otherwise the stop nudge would never fire naturally
    from repeated identical failures."""
    ctx = _make_ctx()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    # Exhaust the failed-test nudge budget so it doesn't interfere with
    # the frontier-streak assertions below.
    ctx.failed_test_nudge_count = MAX_FAILED_TEST_NUDGES
    defn = _FakeDefinition([_FakeBlock("a", "extraction", {"prompt": "p"})])
    _set_failure_ctx(ctx, defn, "Selector not found")
    result = {"ok": False, "data": {"failure_categories": []}}

    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 1
    assert _check_enforcement(ctx) is None

    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 2
    assert _check_enforcement(ctx) == POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE
    assert ctx.repeated_failure_streak_count == 2
    assert _check_enforcement(ctx) != POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE

    update_repeated_failure_state(ctx, result)
    assert ctx.repeated_failure_streak_count == 3
    assert _check_enforcement(ctx) == POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE
    assert _check_enforcement(ctx) != POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE


def test_compute_failure_signature_none_on_clean_success() -> None:
    assert (
        compute_failure_signature(
            frontier_start_label="a",
            failure_reason=None,
            failure_categories=None,
            suspicious_success=False,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Verified-prefix preservation on failure                                     #
# --------------------------------------------------------------------------- #


def test_failed_unchanged_rerun_preserves_verified_prefix_and_outputs() -> None:
    """A failed rerun of the same workflow must NOT clear prior verified
    state. A subsequent edit can then still use the append/anchor
    optimization instead of running the whole chain from scratch.
    """
    from skyvern.forge.sdk.copilot import tools

    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["a", "b"]
    ctx.verified_block_outputs = {"a": "nav", "b": {"title": "hi"}}

    failed_result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_fail",
            "blocks": [
                {"label": "a", "status": "completed"},
                {"label": "b", "status": "failed", "failure_reason": "Selector not found"},
            ],
        },
    }

    # Prior state unchanged by a failed run so the next edit can still
    # optimize the frontier.
    tools._record_run_blocks_result(ctx, failed_result)
    assert ctx.verified_prefix_labels == ["a", "b"]
    assert ctx.verified_block_outputs == {"a": "nav", "b": {"title": "hi"}}


def test_run_blocks_outcome_rolls_forward_after_failed_preview() -> None:
    ctx = _make_ctx()

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_fail",
                "blocks": [{"label": "summarize", "status": "failed", "failure_reason": "Jinja ref undefined"}],
            },
        },
    )
    assert ctx.last_test_ok is False

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_success",
                "blocks": [{"label": "summarize", "status": "completed", "extracted_data": {"summary": "ok"}}],
            },
        },
    )

    assert ctx.last_test_ok is True
    assert ctx.last_test_failure_reason is None


def test_yaml_diff_invalidation_drops_edited_label_and_downstream() -> None:
    """When the YAML changes between runs, verified-state invalidation based
    on the diff should drop edited labels and anything downstream so the
    next frontier planner doesn't seed stale values.
    """
    ctx = _make_ctx()
    ctx.verified_prefix_labels = ["a", "b", "c"]
    ctx.verified_block_outputs = {"a": "nav", "b": {"v": 1}, "c": "x"}

    old = _FakeDefinition(
        [
            _FakeBlock("a", "navigation"),
            _FakeBlock("b", "extraction", {"prompt": "p"}),
            _FakeBlock("c", "extraction", {"prompt": "kept"}),
        ]
    )
    new = _FakeDefinition(
        [
            _FakeBlock("a", "navigation"),
            _FakeBlock("b", "extraction", {"prompt": "CHANGED"}),
            _FakeBlock("c", "extraction", {"prompt": "kept"}),
        ]
    )
    invalidated = _find_invalidated_labels(old, new, list(ctx.verified_prefix_labels))
    assert invalidated == {"b", "c"}
    for label in invalidated:
        ctx.verified_block_outputs.pop(label, None)
    ctx.verified_prefix_labels = [label for label in ctx.verified_prefix_labels if label not in invalidated]
    assert ctx.verified_prefix_labels == ["a"]
    assert ctx.verified_block_outputs == {"a": "nav"}


# --------------------------------------------------------------------------- #
# Edit-time verified-state invalidation                                        #
# --------------------------------------------------------------------------- #


def _wf_def(*specs: tuple[str, str, dict[str, Any]], params: list[_FakeParameter] | None = None) -> _FakeDefinition:
    return _FakeDefinition(
        [_FakeBlock(label, block_type, config) for label, block_type, config in specs],
        parameters=params,
    )


def _seed_verified(ctx: CopilotContext, labels: list[str], *, current_url: str | None, full: bool) -> None:
    ctx.verified_prefix_labels = list(labels)
    ctx.verified_block_outputs = {label: {"output": label} for label in labels}
    ctx.verified_prefix_current_url = current_url
    ctx.last_full_workflow_test_ok = full
    evidence = ctx.workflow_verification_evidence
    evidence.block_verified = list(labels)
    evidence.full_workflow_verified = full
    evidence.live_page_state_verified = True
    evidence.verified_from_current_browser_state = True
    evidence.current_url_observed_after_workflow_run = True
    evidence.current_url_may_encode_runtime_state = True


def test_edit_invalidates_verified_goal_block_on_split_path() -> None:
    prior = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "grab results"}),
    )
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "grab DIFFERENT results"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["open", "search", "extract"], current_url="https://example.com/results", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == ["open", "search"]
    assert "extract" not in ctx.verified_block_outputs
    assert ctx.workflow_verification_evidence.block_verified == ["open", "search"]
    assert ctx.verified_prefix_current_url is None
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.workflow_verification_evidence.live_page_state_verified is False
    assert ctx.workflow_verification_evidence.verified_from_current_browser_state is False

    # Split path: run_blocks passes old==new; the pruned prefix makes the edited
    # block the frontier again instead of reusing it as verified.
    labels, _seed, frontier = _plan_frontier(ctx, ["open", "search", "extract"], new, new)
    assert frontier == "extract"
    assert "extract" in labels


def test_append_only_edit_keeps_prefix_but_drops_end_to_end_claim() -> None:
    prior = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
    )
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "grab results"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["open", "search"], current_url="https://example.com/after", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    # Append-after-success optimization stays intact.
    assert ctx.verified_prefix_labels == ["open", "search"]
    assert set(ctx.verified_block_outputs) == {"open", "search"}
    assert ctx.workflow_verification_evidence.block_verified == ["open", "search"]
    assert ctx.verified_prefix_current_url == "https://example.com/after"
    # But the workflow is no longer verified end to end.
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_remove_trailing_block_clears_full_workflow_evidence() -> None:
    prior = _wf_def(
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
        ("c", "extraction", {"prompt": "c"}),
    )
    new = _wf_def(
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["a", "b", "c"], current_url="https://example.com/c", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert "c" not in ctx.verified_prefix_labels
    assert "c" not in ctx.verified_block_outputs
    assert "c" not in ctx.workflow_verification_evidence.block_verified
    assert ctx.verified_prefix_current_url is None
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_no_op_resave_preserves_verified_state() -> None:
    specs = (
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["a", "b"], current_url="https://example.com/b", full=True)

    _invalidate_verified_state_on_edit(ctx, _wf_def(*specs), _wf_def(*specs))

    assert ctx.verified_prefix_labels == ["a", "b"]
    assert ctx.verified_prefix_current_url == "https://example.com/b"
    assert ctx.workflow_verification_evidence.full_workflow_verified is True
    assert ctx.last_full_workflow_test_ok is True


def test_no_op_resave_preserves_block_verified_only_end_to_end_claim() -> None:
    specs = (
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
    )
    ctx = _make_ctx()
    evidence = ctx.workflow_verification_evidence
    evidence.block_verified = ["a", "b"]
    evidence.full_workflow_verified = True
    ctx.last_full_workflow_test_ok = True

    _invalidate_verified_state_on_edit(ctx, _wf_def(*specs), _wf_def(*specs))

    assert evidence.full_workflow_verified is True
    assert ctx.last_full_workflow_test_ok is True


def test_missing_new_definition_with_trust_fails_closed() -> None:
    specs = (
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["a", "b"], current_url="https://example.com/b", full=True)

    _invalidate_verified_state_on_edit(ctx, _wf_def(*specs), None)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.workflow_verification_evidence.full_workflow_verified is False


def test_edit_unverified_upstream_invalidates_downstream_verified() -> None:
    prior = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "extract"}),
    )
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search CHANGED"}),
        ("extract", "extraction", {"prompt": "extract"}),
    )
    ctx = _make_ctx()
    # Non-contiguous verified state: open + extract verified, search NOT verified.
    ctx.verified_prefix_labels = ["open", "extract"]
    ctx.verified_block_outputs = {"open": 1, "extract": 3}
    ctx.workflow_verification_evidence.block_verified = ["open", "extract"]

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert "extract" not in ctx.verified_prefix_labels
    assert "extract" not in ctx.workflow_verification_evidence.block_verified
    assert "open" in ctx.verified_prefix_labels


def test_chokepoint_uses_passed_prior_when_last_workflow_absent() -> None:
    prior = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("extract", "extraction", {"prompt": "extract"}),
    )
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("extract", "extraction", {"prompt": "extract CHANGED"}),
    )
    ctx = _make_ctx()
    # Saved workflow verified via run_blocks without ever populating last_workflow.
    ctx.last_workflow = None
    ctx.verified_prefix_labels = ["open", "extract"]
    ctx.verified_block_outputs = {"open": 1, "extract": 2}
    ctx.workflow_verification_evidence.block_verified = ["open", "extract"]

    _record_workflow_update_result(ctx, {"ok": True, "_workflow": _FakeWorkflow(new)}, prior)

    assert ctx.last_workflow is not None
    assert "extract" not in ctx.verified_prefix_labels
    assert "extract" not in ctx.verified_block_outputs
    assert "extract" in tools._unverified_current_workflow_labels(ctx)


def test_unavailable_prior_with_trust_fails_closed() -> None:
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("extract", "extraction", {"prompt": "extract"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["open", "extract"], current_url="https://example.com/x", full=True)

    _invalidate_verified_state_on_edit(ctx, None, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.verified_prefix_current_url is None
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_differ_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    prior = _wf_def(("a", "goto_url", {"url": "https://example.com"}))
    new = _wf_def(("a", "goto_url", {"url": "https://example.com/2"}))
    ctx = _make_ctx()
    _seed_verified(ctx, ["a"], current_url="https://example.com", full=True)

    def _boom(*_args: object, **_kwargs: object) -> set[str]:
        raise RuntimeError("differ blew up")

    monkeypatch.setattr(tools, "_find_invalidated_labels", _boom)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.verified_prefix_current_url is None


def test_fused_and_split_leave_identical_verified_state() -> None:
    prior = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "extract"}),
    )
    new = _wf_def(
        ("open", "goto_url", {"url": "https://example.com"}),
        ("search", "navigation", {"prompt": "search"}),
        ("extract", "extraction", {"prompt": "extract CHANGED"}),
    )

    def _build() -> CopilotContext:
        c = _make_ctx()
        _seed_verified(c, ["open", "search", "extract"], current_url="https://example.com/results", full=True)
        return c

    split_ctx = _build()
    _invalidate_verified_state_on_edit(split_ctx, prior, new)
    fused_ctx = _build()
    _invalidate_verified_state_on_edit(fused_ctx, prior, new)

    assert split_ctx.verified_prefix_labels == fused_ctx.verified_prefix_labels == ["open", "search"]
    assert split_ctx.verified_block_outputs == fused_ctx.verified_block_outputs
    assert (
        split_ctx.workflow_verification_evidence.block_verified
        == fused_ctx.workflow_verification_evidence.block_verified
        == ["open", "search"]
    )

    # Neither the split seam (new, new) nor the fused seam (prior, new) reuses the
    # edited block as verified.
    split_labels, _s, _sf = _plan_frontier(split_ctx, ["open", "search", "extract"], new, new)
    fused_labels, _f, _ff = _plan_frontier(fused_ctx, ["open", "search", "extract"], prior, new)
    assert "extract" in split_labels
    assert "extract" in fused_labels


def test_parameter_value_change_resets_verified_trust() -> None:
    prior = _wf_def(
        ("search", "navigation", {"prompt": "search {{ term }}"}),
        ("extract", "extraction", {"prompt": "grab"}),
        params=[_FakeParameter("term", "cats")],
    )
    new = _wf_def(
        ("search", "navigation", {"prompt": "search {{ term }}"}),
        ("extract", "extraction", {"prompt": "grab"}),
        params=[_FakeParameter("term", "dogs")],
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["search", "extract"], current_url="https://example.com/r", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_parameter_addition_resets_verified_trust() -> None:
    # A block can reference a parameter by template without a config edit, so an
    # added key may alter behavior the block-diff alone won't catch — fail closed.
    prior = _wf_def(
        ("search", "navigation", {"prompt": "search"}),
        params=[_FakeParameter("term", "cats")],
    )
    new = _wf_def(
        ("search", "navigation", {"prompt": "search"}),
        params=[_FakeParameter("term", "cats"), _FakeParameter("limit", 10)],
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["search"], current_url="https://example.com/s", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_parameter_removal_resets_verified_trust() -> None:
    prior = _wf_def(
        ("search", "navigation", {"prompt": "search {{ term }}"}),
        ("extract", "extraction", {"prompt": "grab"}),
        params=[_FakeParameter("term", "cats"), _FakeParameter("limit", 10)],
    )
    new = _wf_def(
        ("search", "navigation", {"prompt": "search {{ term }}"}),
        ("extract", "extraction", {"prompt": "grab"}),
        params=[_FakeParameter("term", "cats")],
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["search", "extract"], current_url="https://example.com/r", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.block_verified == []
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


def test_parameter_reorder_keeps_verified_trust() -> None:
    # Parameters are referenced by key, so pure reordering changes no behavior.
    prior = _wf_def(
        ("search", "navigation", {"prompt": "search"}),
        params=[_FakeParameter("term", "cats"), _FakeParameter("limit", 10)],
    )
    new = _wf_def(
        ("search", "navigation", {"prompt": "search"}),
        params=[_FakeParameter("limit", 10), _FakeParameter("term", "cats")],
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["search"], current_url="https://example.com/s", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == ["search"]
    assert ctx.workflow_verification_evidence.full_workflow_verified is True
    assert ctx.last_full_workflow_test_ok is True


def test_reorder_resets_verified_trust() -> None:
    prior = _wf_def(
        ("a", "goto_url", {"url": "https://example.com"}),
        ("b", "navigation", {"prompt": "b"}),
    )
    new = _wf_def(
        ("b", "navigation", {"prompt": "b"}),
        ("a", "goto_url", {"url": "https://example.com"}),
        ("c", "extraction", {"prompt": "c"}),
    )
    ctx = _make_ctx()
    _seed_verified(ctx, ["a", "b"], current_url="https://example.com/b", full=True)

    _invalidate_verified_state_on_edit(ctx, prior, new)

    assert ctx.verified_prefix_labels == []
    assert ctx.verified_block_outputs == {}
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.last_full_workflow_test_ok is False


# --------------------------------------------------------------------------- #
# Code-only runtime rerun and output evidence                                 #
# --------------------------------------------------------------------------- #


def test_code_only_suffix_after_mutating_prefix_verifies_chain_without_replay() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    definition = _FakeDefinition(
        [
            _FakeBlock("open_site", "code", {"code": "await page.goto('https://example.com')"}),
            _FakeBlock("search_product", "code", {"code": "await page.fill('#q', 'part')"}),
            _FakeBlock("add_to_cart", "code", {"code": "await page.click('#add')"}),
            _FakeBlock("confirm_cart", "code", {"code": "return {'cart': 'ready'}"}),
        ]
    )
    ctx.last_workflow = _FakeWorkflow(definition)
    ctx.verified_prefix_labels = ["open_site", "search_product", "add_to_cart", "confirm_cart"]

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_suffix",
                "executed_block_labels": ["confirm_cart"],
                "blocks": [
                    {
                        "label": "confirm_cart",
                        "block_type": "code",
                        "status": "completed",
                        "output": {"cart": "ready"},
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.workflow_verification_evidence.full_workflow_verified is True


def test_code_only_suffix_run_without_mutating_prefix_does_not_verify_full_workflow() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    definition = _FakeDefinition(
        [
            _FakeBlock("open_site", "code", {"code": "await page.goto('https://example.com')"}),
            _FakeBlock("search_results", "code", {"code": "await page.fill('#q', 'sample')"}),
            _FakeBlock("extract_results", "code", {"code": "return {'records': []}"}),
        ]
    )
    ctx.last_workflow = _FakeWorkflow(definition)
    ctx.verified_prefix_labels = ["open_site", "search_results", "extract_results"]

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_extract",
                "executed_block_labels": ["extract_results"],
                "blocks": [
                    {
                        "label": "extract_results",
                        "block_type": "code",
                        "status": "completed",
                        "output": {"records": [{"title": "sample result"}]},
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is False
    assert "suffix frontier" in str(ctx.last_test_failure_reason)
    assert ctx.workflow_verification_evidence.full_workflow_verified is False


def test_code_only_run_result_rejects_non_json_serializable_code_output() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_locator",
                "blocks": [
                    {
                        "label": "extract_records",
                        "block_type": "code",
                        "status": "completed",
                        "output": "Object '<class 'playwright.async_api._generated.Locator'>' is not JSON serializable",
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert "non-JSON-serializable runtime object" in str(ctx.last_test_failure_reason)


def test_code_only_run_result_accepts_real_data_with_transient_non_json_locals() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_data",
                "blocks": [
                    {
                        "label": "extract_records",
                        "block_type": "code",
                        "status": "completed",
                        "output": {
                            "records": [{"title": "sample result"}],
                            "debug": (
                                "Object '<class 'playwright.async_api._generated.Locator'>' is not JSON serializable"
                            ),
                        },
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None


def test_code_only_run_result_rejects_empty_data_read_output() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_empty",
                "blocks": [
                    {
                        "label": "extract_records",
                        "block_type": "code",
                        "status": "completed",
                        "output": {"records": [], "count": 0},
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert "no meaningful page data" in str(ctx.last_test_failure_reason)


def test_code_only_run_result_rejects_missing_requested_structured_detail() -> None:
    ctx = _make_ctx(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        user_message="Get the matching record number and expiration.",
    )

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_details",
                "blocks": [
                    {
                        "label": "extract_record_details",
                        "block_type": "code",
                        "status": "completed",
                        "output": {"records": [{"title": "sample result"}]},
                    }
                ],
            },
        },
    )

    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert "expiration" in str(ctx.last_test_failure_reason)
    assert "number" in str(ctx.last_test_failure_reason)


def test_observed_code_only_url_keeps_cert_failure_repairable() -> None:
    reason = "Failed to navigate to url https://bad.example/search. Error message: net::ERR_CERT_DATE_INVALID"
    ctx = _make_ctx(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        observed_browser_urls=["https://bad.example/search"],
    )

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_cert",
                "blocks": [{"label": "open_site", "status": "failed", "failure_reason": reason}],
            },
        },
    )

    assert ctx.last_test_non_retriable_nav_error is None
    assert ctx.last_test_failure_reason == reason


def test_observed_standard_url_still_treats_cert_failure_as_non_retriable() -> None:
    reason = "Failed to navigate to url https://bad.example/search. Error message: net::ERR_CERT_DATE_INVALID"
    ctx = _make_ctx(observed_browser_urls=["https://bad.example/search"])

    tools._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_cert",
                "blocks": [{"label": "open_site", "status": "failed", "failure_reason": reason}],
            },
        },
    )

    assert ctx.last_test_non_retriable_nav_error == reason


def test_code_only_failed_mutating_frontier_blocks_blind_rerun() -> None:
    ctx = _make_ctx(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    ctx.code_only_failed_mutating_frontier_label = "add_exact_product_to_cart"

    result = tools._tool_loop_error(ctx, "update_and_run_blocks", {"block_labels": ["add_exact_product_to_cart"]})

    assert result is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_code_only_failed_mutation_needs_inspection"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
