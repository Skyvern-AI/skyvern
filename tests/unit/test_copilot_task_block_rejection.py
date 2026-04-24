"""Tests for the copilot-v2 post-emission reject of ``task`` / ``task_v2`` block
types (SKY-9174, Part F).

Part C.1 banned the types at the schema-lookup surface via `SchemaOverlay`
pre / post hooks, but the LLM can bypass that by writing YAML directly without
querying the schema. Part F closes the bypass with a YAML-level reject that
fires on every copilot-v2 write path (``_update_workflow`` + inline
``REPLACE_WORKFLOW``), keyed by block label so legacy workflows with
pre-existing ``task`` blocks can still be edited by the copilot.
"""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.forge.sdk.copilot.tools import _detect_new_banned_blocks, _update_workflow


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump(
        {"title": "wf", "workflow_definition": {"blocks": list(blocks)}},
        sort_keys=False,
    )


# ---------- Flat shapes ----------


def test_top_level_task_block_is_detected_on_first_authoring() -> None:
    submitted = _yaml({"block_type": "task", "label": "fill_contact_form", "navigation_goal": "do thing"})
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("fill_contact_form", "task")]


def test_top_level_task_v2_block_is_detected() -> None:
    submitted = _yaml({"block_type": "task_v2", "label": "legacy_taskv2", "prompt": "do it"})
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("legacy_taskv2", "task_v2")]


def test_case_and_whitespace_insensitive() -> None:
    submitted = _yaml(
        {"block_type": "TASK", "label": "a"},
        {"block_type": " task_v2 ", "label": "b"},
        {"block_type": "Task", "label": "c"},
    )
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert sorted(result) == [("a", "task"), ("b", "task_v2"), ("c", "task")]


def test_mixed_task_and_navigation_only_reports_banned() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "nav_a", "navigation_goal": "ok"},
        {"block_type": "task", "label": "bad", "navigation_goal": "bad"},
        {"block_type": "extraction", "label": "ext_a"},
    )
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("bad", "task")]


def test_only_allowed_types_returns_empty() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "n"},
        {"block_type": "extraction", "label": "e"},
        {"block_type": "validation", "label": "v"},
        {"block_type": "login", "label": "lg"},
        {"block_type": "goto_url", "label": "g"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == []


# ---------- Malformed ----------


def test_malformed_yaml_is_graceful_no_op() -> None:
    # Intentional parse failure — missing close quote.
    assert _detect_new_banned_blocks("title: 'unterminated", prior_workflow_yaml=None) == []


def test_missing_workflow_definition_is_graceful_no_op() -> None:
    assert _detect_new_banned_blocks("title: wf\n", prior_workflow_yaml=None) == []


def test_blocks_key_not_a_list_is_graceful_no_op() -> None:
    bad = "title: wf\nworkflow_definition:\n  blocks: not-a-list\n"
    assert _detect_new_banned_blocks(bad, prior_workflow_yaml=None) == []


def test_block_entry_not_a_dict_is_skipped() -> None:
    # A bare string where a block dict is expected — should be skipped, not crash.
    weird = textwrap.dedent(
        """\
        title: wf
        workflow_definition:
          blocks:
            - "not a block"
            - block_type: task
              label: real_banned
        """
    )
    assert _detect_new_banned_blocks(weird, prior_workflow_yaml=None) == [("real_banned", "task")]


# ---------- Legacy preservation (RISK-1) ----------


def test_preserved_legacy_task_block_under_same_label_does_not_reject() -> None:
    prior = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old"})
    submitted = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old edited"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


def test_new_task_block_alongside_preserved_legacy_reports_only_the_new_one() -> None:
    prior = _yaml({"block_type": "task", "label": "legacy_task"})
    submitted = _yaml(
        {"block_type": "task", "label": "legacy_task"},
        {"block_type": "task", "label": "fill_contact_form"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("fill_contact_form", "task")]


def test_renamed_legacy_task_block_is_treated_as_new() -> None:
    """Edge case: copilot re-emits a legacy task block under a different label.
    The detector has no way to know this is a rename, so it's reported as new.
    Acceptable: the copilot can recover by re-using the prior label."""
    prior = _yaml({"block_type": "task", "label": "old_name"})
    submitted = _yaml({"block_type": "task", "label": "new_name"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("new_name", "task")]


def test_prior_contains_allowed_types_submitted_adds_task_rejects() -> None:
    prior = _yaml({"block_type": "navigation", "label": "nav"})
    submitted = _yaml(
        {"block_type": "navigation", "label": "nav"},
        {"block_type": "task", "label": "bad_new"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("bad_new", "task")]


def test_legacy_task_v2_preservation() -> None:
    prior = _yaml({"block_type": "task_v2", "label": "legacy_v2"})
    submitted = _yaml({"block_type": "task_v2", "label": "legacy_v2"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


# ---------- Nested (COMP-1) ----------


def test_task_block_inside_for_loop_is_detected() -> None:
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [
                {"block_type": "navigation", "label": "inner_nav"},
                {"block_type": "task", "label": "inner_bad"},
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == [("inner_bad", "task")]


def test_nested_preservation_does_not_reject() -> None:
    prior = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "task", "label": "nested_legacy"}],
        }
    )
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "task", "label": "nested_legacy"}],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


def test_nested_new_addition_is_detected() -> None:
    prior = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "navigation", "label": "nav_inner"}],
        }
    )
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [
                {"block_type": "navigation", "label": "nav_inner"},
                {"block_type": "task", "label": "new_nested_bad"},
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("new_nested_bad", "task")]


def test_deeply_nested_for_loop_is_walked() -> None:
    """for_loop nested inside another for_loop — recursion must reach the innermost level."""
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "outer",
            "loop_blocks": [
                {
                    "block_type": "for_loop",
                    "label": "inner",
                    "loop_blocks": [{"block_type": "task", "label": "deeply_nested_bad"}],
                }
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == [("deeply_nested_bad", "task")]


# ---------- Missing label — should not crash ----------


def test_block_without_label_is_skipped() -> None:
    """A banned block missing the ``label`` key can't be identified for
    preservation matching; skip it rather than crash. The YAML validator
    downstream will surface the missing-label error on its own."""
    submitted = _yaml({"block_type": "task", "navigation_goal": "no label"})
    # No label → not collectible; result is empty (downstream Pydantic reject
    # will surface the malformed block).
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == []


# ---------- Integration-shape tests: _update_workflow end-to-end ----------
#
# These exercise the reject path at the tool-helper boundary, confirming the
# detection + error tool-result shape + dedicated OTEL span. The success path
# (YAML with only allowed types, or with preserved legacy task labels) is also
# covered — we patch ``_process_workflow_yaml`` and the workflow-service write
# so the test does not need a DB.


def _ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.workflow_yaml = prior_yaml
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    return ctx


@pytest.mark.asyncio
async def test_update_workflow_rejects_new_task_block_and_emits_span() -> None:
    submitted = _yaml({"block_type": "task", "label": "fill_contact_form", "navigation_goal": "do"})
    ctx = _ctx(prior_yaml=None)

    with patch("skyvern.forge.sdk.copilot.tools._record_banned_block_reject_span") as mock_span:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    assert "fill_contact_form" in result["error"]
    for alternative in ("navigation", "extraction", "validation", "login"):
        assert alternative in result["error"]

    # Dedicated span fired with source_tool + items for logfire trend analysis.
    mock_span.assert_called_once_with("_update_workflow", [("fill_contact_form", "task")])


@pytest.mark.asyncio
async def test_update_workflow_preserves_legacy_task_block_under_unchanged_label() -> None:
    """Copilot edit of a legacy workflow that already carries a ``task`` block
    must not fail the reject. The helper sees the task label in prior YAML
    and treats its re-emission as legacy preservation, not a new addition."""
    prior = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old"})
    # New YAML preserves the legacy task block AND adds an allowed-type block.
    submitted = _yaml(
        {"block_type": "task", "label": "legacy_task", "navigation_goal": "old"},
        {"block_type": "navigation", "label": "new_nav", "navigation_goal": "new"},
    )
    ctx = _ctx(prior_yaml=prior)

    fake_workflow = MagicMock()
    fake_workflow.title = "t"
    fake_workflow.description = "d"
    fake_workflow.workflow_definition = MagicMock()
    fake_workflow.proxy_location = None
    fake_workflow.webhook_callback_url = None
    fake_workflow.persist_browser_session = False
    fake_workflow.model = None
    fake_workflow.max_screenshot_scrolls = None
    fake_workflow.extra_http_headers = None
    fake_workflow.run_with = None
    fake_workflow.ai_fallback = None
    fake_workflow.cache_key = None
    fake_workflow.run_sequentially = None
    fake_workflow.sequential_key = None

    with (
        patch("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", return_value=fake_workflow),
        patch("skyvern.forge.sdk.copilot.tools.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True
    # The new YAML was accepted and assigned to ctx as the current workflow state.
    assert ctx.workflow_yaml == submitted


@pytest.mark.asyncio
async def test_update_workflow_allows_all_allowed_block_types() -> None:
    """Baseline success path: only allowed block types, no prior — passes through."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "n", "navigation_goal": "x"},
        {"block_type": "validation", "label": "v", "complete_criterion": "c"},
    )
    ctx = _ctx(prior_yaml=None)

    fake_workflow = MagicMock()
    for attr in (
        "title",
        "description",
        "workflow_definition",
        "proxy_location",
        "webhook_callback_url",
        "persist_browser_session",
        "model",
        "max_screenshot_scrolls",
        "extra_http_headers",
        "run_with",
        "ai_fallback",
        "cache_key",
        "run_sequentially",
        "sequential_key",
    ):
        setattr(fake_workflow, attr, None)

    with (
        patch("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", return_value=fake_workflow),
        patch("skyvern.forge.sdk.copilot.tools.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True
