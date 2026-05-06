"""Tests for the mid-correction wall-clock guard and salvage path."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.agent import (
    _TIMEOUT_REPLY_DEFAULT,
    _TIMEOUT_REPLY_TESTED,
    _build_timeout_exit_result,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.tools import (
    PER_TOOL_CALL_BUDGET_SECONDS,
    _record_run_blocks_result,
    _tool_loop_error,
)


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _ctx_after_failure_with_verified_prefix(*, elapsed_seconds: float) -> CopilotContext:
    ctx = _fresh_context()
    ctx.copilot_run_start_monotonic = time.monotonic() - elapsed_seconds
    ctx.last_test_ok = False
    ctx.last_failed_workflow_yaml = "yaml-failed"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-failed")
    ctx.last_workflow_yaml = "yaml-failed"
    return ctx


def test_tool_loop_error_blocks_run_when_budget_low_after_failure() -> None:
    elapsed = TOTAL_TIMEOUT_SECONDS - (PER_TOOL_CALL_BUDGET_SECONDS - 30)
    ctx = _ctx_after_failure_with_verified_prefix(elapsed_seconds=elapsed)
    for tool in ("update_and_run_blocks", "run_blocks_and_collect_debug"):
        msg = _tool_loop_error(ctx, tool)
        assert msg is not None and "Wall-clock budget too low" in msg, tool


def test_tool_loop_error_no_guard_when_budget_sufficient() -> None:
    ctx = _ctx_after_failure_with_verified_prefix(
        elapsed_seconds=TOTAL_TIMEOUT_SECONDS - (PER_TOOL_CALL_BUDGET_SECONDS + 60)
    )
    assert _tool_loop_error(ctx, "update_and_run_blocks") is None


def test_tool_loop_error_no_guard_on_first_call() -> None:
    ctx = _fresh_context()
    ctx.copilot_run_start_monotonic = time.monotonic() - (TOTAL_TIMEOUT_SECONDS - 10)
    assert _tool_loop_error(ctx, "update_and_run_blocks") is None


def test_tool_loop_error_no_guard_when_no_good_workflow_exists() -> None:
    ctx = _fresh_context()
    ctx.copilot_run_start_monotonic = time.monotonic() - (TOTAL_TIMEOUT_SECONDS - 10)
    ctx.last_test_ok = False
    ctx.last_failed_workflow_yaml = "yaml-failed"
    assert _tool_loop_error(ctx, "update_and_run_blocks") is None


def test_tool_loop_error_no_guard_for_non_block_running_tools() -> None:
    elapsed = TOTAL_TIMEOUT_SECONDS - (PER_TOOL_CALL_BUDGET_SECONDS - 30)
    ctx = _ctx_after_failure_with_verified_prefix(elapsed_seconds=elapsed)
    for tool in ("update_workflow", "list_credentials", "get_run_results"):
        assert _tool_loop_error(ctx, tool) is None, tool


def test_tool_loop_error_guard_persists_through_update_workflow() -> None:
    # ``last_test_ok`` flips to None on every ``update_workflow``, but
    # ``last_failed_workflow_yaml`` stays — so the guard must still fire.
    elapsed = TOTAL_TIMEOUT_SECONDS - (PER_TOOL_CALL_BUDGET_SECONDS - 30)
    ctx = _ctx_after_failure_with_verified_prefix(elapsed_seconds=elapsed)
    ctx.last_test_ok = None
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-edited")
    ctx.last_workflow_yaml = "yaml-edited"
    msg = _tool_loop_error(ctx, "update_and_run_blocks")
    assert msg is not None and "Wall-clock budget too low" in msg


def test_record_run_blocks_result_promotes_last_good_on_real_success() -> None:
    ctx = _fresh_context()
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-1")
    ctx.last_workflow_yaml = "yaml-1"
    _record_run_blocks_result(ctx, {"ok": True, "data": {"blocks": []}})
    assert ctx.last_good_workflow is ctx.last_workflow
    assert ctx.last_good_workflow_yaml == "yaml-1"


def test_record_run_blocks_result_does_not_promote_on_empty_data_success() -> None:
    ctx = _fresh_context()
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-1")
    ctx.last_workflow_yaml = "yaml-1"
    blocks = [{"block_type": "EXTRACTION", "label": "x", "status": "completed", "extracted_data": None}]
    _record_run_blocks_result(ctx, {"ok": True, "data": {"blocks": blocks}})
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_good_workflow is None


def test_record_run_blocks_result_does_not_promote_on_failure() -> None:
    ctx = _fresh_context()
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-prev")
    ctx.last_good_workflow_yaml = "yaml-prev"
    _record_run_blocks_result(ctx, {"ok": False, "data": {"blocks": []}})
    assert ctx.last_good_workflow is not None
    assert ctx.last_good_workflow_yaml == "yaml-prev"


def test_build_timeout_exit_result_surfaces_last_good_after_failure() -> None:
    ctx = _fresh_context()
    ctx.last_test_ok = False
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-failed")
    ctx.last_workflow_yaml = "yaml-failed"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    ctx.workflow_persisted = True
    result = _build_timeout_exit_result(ctx, global_llm_context=None)
    assert result.updated_workflow is ctx.last_good_workflow
    assert result.workflow_yaml == "yaml-good"
    assert result.unvalidated is True
    assert result.user_response == _TIMEOUT_REPLY_TESTED


def test_build_timeout_exit_result_surfaces_last_good_on_mid_flight_cancellation() -> None:
    # Deadline fires between ``_record_workflow_update_result`` and
    # ``_record_run_blocks_result``: ``last_test_ok`` is None but
    # ``last_workflow`` is the in-flight unverified shape.
    ctx = _fresh_context()
    ctx.last_test_ok = None
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-in-flight")
    ctx.last_workflow_yaml = "yaml-in-flight"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    result = _build_timeout_exit_result(ctx, global_llm_context=None)
    assert result.updated_workflow is ctx.last_good_workflow
    assert result.unvalidated is True


def test_build_timeout_exit_result_falls_through_when_no_last_good() -> None:
    ctx = _fresh_context()
    ctx.last_test_ok = False
    result = _build_timeout_exit_result(ctx, global_llm_context=None)
    assert result.user_response == _TIMEOUT_REPLY_DEFAULT
    assert result.updated_workflow is None


def _fake_run_result(envelope: dict[str, Any]) -> SimpleNamespace:
    import json as _json

    return SimpleNamespace(final_output=_json.dumps(envelope), new_items=[])


def _fake_chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-id",
        workflow_permanent_id="wfp-id",
        workflow_yaml="",
        message="",
        browser_session_id=None,
        workflow_copilot_chat_id="chat-id",
    )


def test_translate_to_agent_result_salvages_last_good_on_failed_reply() -> None:
    from skyvern.forge.sdk.copilot.agent import _translate_to_agent_result

    ctx = _fresh_context()
    ctx.last_test_ok = False
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-failed")
    ctx.last_workflow_yaml = "yaml-failed"
    ctx.last_update_block_count = 5
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    agent_text = "Built 4 verified blocks; block 5 failed."
    result = _fake_run_result({"type": "REPLY", "user_response": agent_text, "goal_reached": False})
    agent_result = _translate_to_agent_result(
        result, ctx, global_llm_context=None, chat_request=_fake_chat_request(), organization_id="o"
    )
    assert agent_result.updated_workflow is ctx.last_good_workflow
    assert agent_result.workflow_yaml == "yaml-good"
    assert agent_result.unvalidated is True
    # Failure rewrite would have replaced the agent's text with one based on
    # ``last_update_block_count=5``; salvage must skip the rewrite.
    assert agent_result.user_response == agent_text


def test_translate_to_agent_result_salvages_after_failure_then_update_workflow() -> None:
    # failure → ``update_workflow`` → guard → REPLY: ``last_test_ok`` is
    # cleared by the edit but ``last_failed_workflow_yaml`` stays sticky.
    from skyvern.forge.sdk.copilot.agent import _translate_to_agent_result

    ctx = _fresh_context()
    ctx.last_test_ok = None
    ctx.last_failed_workflow_yaml = "yaml-failed"
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-edited")
    ctx.last_workflow_yaml = "yaml-edited"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    agent_text = "Built 4 blocks; block 5 failed; ran out of time."
    result = _fake_run_result({"type": "REPLY", "user_response": agent_text, "goal_reached": False})
    agent_result = _translate_to_agent_result(
        result, ctx, global_llm_context=None, chat_request=_fake_chat_request(), organization_id="o"
    )
    assert agent_result.updated_workflow is ctx.last_good_workflow
    assert agent_result.unvalidated is True
    assert agent_result.user_response == agent_text


def test_translate_to_agent_result_does_not_salvage_on_standalone_edit() -> None:
    # Pure "edited but never tested" — no failure recorded — must not salvage.
    from skyvern.forge.sdk.copilot.agent import _translate_to_agent_result

    ctx = _fresh_context()
    ctx.last_test_ok = None
    ctx.last_failed_workflow_yaml = None
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-edited")
    ctx.last_workflow_yaml = "yaml-edited"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    result = _fake_run_result({"type": "REPLY", "user_response": "Drafted.", "goal_reached": False})
    agent_result = _translate_to_agent_result(
        result, ctx, global_llm_context=None, chat_request=_fake_chat_request(), organization_id="o"
    )
    assert agent_result.updated_workflow is ctx.last_workflow
    assert agent_result.workflow_yaml == "yaml-edited"


def test_salvage_skipped_on_suspicious_success() -> None:
    # The empty-data carve-out in ``_build_wip_exit_result`` deliberately
    # drops the proposal; salvage must respect that.
    ctx = _fresh_context()
    ctx.last_test_ok = None
    ctx.last_test_suspicious_success = True
    ctx.last_workflow = SimpleNamespace(workflow_id="wf-suspicious")
    ctx.last_workflow_yaml = "yaml-suspicious"
    ctx.last_good_workflow = SimpleNamespace(workflow_id="wf-good")
    ctx.last_good_workflow_yaml = "yaml-good"
    result = _build_timeout_exit_result(ctx, global_llm_context=None)
    assert result.updated_workflow is None
