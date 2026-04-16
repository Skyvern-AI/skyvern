"""Tests for frontier selection, compact packet shape, and streak guards."""

from __future__ import annotations

from typing import Any

import pytest

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
    _plan_frontier,
    _referenced_output_labels,
)


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

    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict[str, Any]:
        return {
            "label": self.label,
            "block_type": self.block_type.value,
            **self._config,
        }


class _FakeDefinition:
    def __init__(self, blocks: list[_FakeBlock]) -> None:
        self.blocks = blocks


class _FakeWorkflow:
    def __init__(self, definition: _FakeDefinition) -> None:
        self.workflow_definition = definition


class _FakeStream:
    async def is_disconnected(self) -> bool:
        return False

    async def send(self, event: Any) -> None:
        return None


def _make_ctx(**kwargs: Any) -> CopilotContext:
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

    def _blow_up(*args: Any, **kwargs: Any) -> set[str]:
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
