"""Tests for the PARAMETER_BINDING_ERROR failure category and related paths.

Covers:
- classifier keyword matching for the three ``register_block_parameters`` raise
  messages and the pre-run invariant message
- ``_analyze_run_blocks`` honoring precomputed ``data.failure_categories``
- ``_parameter_binding_invariant_error`` diff logic (mismatches and alignment)
- ``compute_failure_signature`` collapsing per-parameter-name text when the
  top category is ``PARAMETER_BINDING_ERROR``
- ``_repeated_frontier_failure_nudge`` picking category-specific warn/stop
  nudges at the existing streak thresholds
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    POST_PARAMETER_BINDING_STOP_NUDGE,
    POST_PARAMETER_BINDING_WARN_NUDGE,
    POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE,
    POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE,
    REPEATED_FRONTIER_STREAK_ESCALATE_AT,
    REPEATED_FRONTIER_STREAK_STOP_AT,
    _check_enforcement,
    _repeated_frontier_failure_nudge,
)
from skyvern.forge.sdk.copilot.failure_tracking import compute_failure_signature
from skyvern.forge.sdk.copilot.tools import _analyze_run_blocks, _parameter_binding_invariant_error
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)

# --------------------------------------------------------------------------- #
# Classifier                                                                  #
# --------------------------------------------------------------------------- #


def test_classify_workflow_parameter_should_have_already_been_set() -> None:
    message = "Workflow parameter product_sku should have already been set through workflow run parameters"
    categories = classify_from_failure_reason(message)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"


def test_classify_output_parameter_context_init_error() -> None:
    message = "Output parameter extract_output should have already been set through workflow run context init"
    categories = classify_from_failure_reason(message)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"


def test_classify_secret_parameter_context_init_error() -> None:
    message = "SecretParameter totp should have already been set through workflow run context init"
    categories = classify_from_failure_reason(message)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"


def test_classify_pre_run_invariant_message() -> None:
    message = (
        "Pre-run invariant: workflow_definition and persisted parameter rows disagree. "
        "workflow missing persisted: ['ticker (string)']"
    )
    categories = classify_from_failure_reason(message)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"


def test_classify_unrelated_error_does_not_match_parameter_binding() -> None:
    categories = classify_from_failure_reason("Element not found on page")
    assert categories is not None
    assert len(categories) > 0
    assert all(cat["category"] != "PARAMETER_BINDING_ERROR" for cat in categories)


# --------------------------------------------------------------------------- #
# _analyze_run_blocks honors precomputed categories                           #
# --------------------------------------------------------------------------- #


def test_analyze_run_blocks_returns_precomputed_categories() -> None:
    result = {
        "ok": False,
        "data": {
            "blocks": [],
            "failure_categories": [
                {
                    "category": "PARAMETER_BINDING_ERROR",
                    "confidence_float": 0.99,
                    "reasoning": "Pre-run invariant tripped",
                }
            ],
        },
    }
    anti_bot, empty_data, categories = _analyze_run_blocks(result)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"
    assert anti_bot is None
    assert empty_data is False


def test_analyze_run_blocks_falls_through_when_no_precomputed() -> None:
    # A failure-reason on a block should still be classified by the fallback.
    result = {
        "ok": False,
        "data": {
            "blocks": [
                {
                    "label": "nav",
                    "block_type": "navigation",
                    "status": "failed",
                    "failure_reason": "Element not found: could not click",
                }
            ],
        },
    }
    _, _, categories = _analyze_run_blocks(result)
    assert categories is not None
    assert categories[0]["category"] == "ELEMENT_NOT_FOUND"


# --------------------------------------------------------------------------- #
# _parameter_binding_invariant_error                                          #
# --------------------------------------------------------------------------- #


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


class _FakeParamDefinition:
    def __init__(self, parameters: list[Any]) -> None:
        self.parameters = parameters


class _FakeWorkflow:
    def __init__(self, workflow_id: str, parameters: list[Any]) -> None:
        self.workflow_id = workflow_id
        self.workflow_definition = _FakeParamDefinition(parameters)


def _wp(key: str, ptype: WorkflowParameterType = WorkflowParameterType.STRING) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=ptype,
        key=key,
        description=None,
        workflow_id="wf_id",
        default_value=None,
        created_at=now,
        modified_at=now,
    )


def _op(key: str) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        output_parameter_id=f"op_{key}",
        key=key,
        description=None,
        workflow_id="wf_id",
        created_at=now,
        modified_at=now,
    )


def test_invariant_aligned_returns_none() -> None:
    workflow = _FakeWorkflow("wf_id", [_wp("ticker"), _op("nav_output")])
    result = _parameter_binding_invariant_error(workflow, [_wp("ticker")], [_op("nav_output")])
    assert result is None


def test_invariant_missing_persisted_workflow_param() -> None:
    workflow = _FakeWorkflow("wf_id", [_wp("ticker"), _wp("product_sku")])
    result = _parameter_binding_invariant_error(workflow, [_wp("ticker")], [])
    assert result is not None
    summary, missing_persisted, missing_from_definition = result
    assert "product_sku" in summary
    assert "product_sku (string)" in missing_persisted["workflow"]
    assert missing_persisted["output"] == []
    assert missing_from_definition["workflow"] == []


def test_invariant_missing_persisted_output_param() -> None:
    workflow = _FakeWorkflow("wf_id", [_op("nav_output")])
    result = _parameter_binding_invariant_error(workflow, [], [])
    assert result is not None
    _, missing_persisted, _ = result
    assert "nav_output" in missing_persisted["output"]


def test_invariant_extra_persisted_workflow_param() -> None:
    # Persisted row exists for a key the definition no longer references.
    workflow = _FakeWorkflow("wf_id", [])
    result = _parameter_binding_invariant_error(workflow, [_wp("stale_key")], [])
    assert result is not None
    _, _, missing_from_definition = result
    assert any("stale_key" in entry for entry in missing_from_definition["workflow"])


def test_invariant_type_mismatch_flagged_both_ways() -> None:
    # Definition says JSON, persisted says STRING — identity is (key, type) so
    # both rows show up as diffs in opposite directions.
    workflow = _FakeWorkflow("wf_id", [_wp("cfg", WorkflowParameterType.JSON)])
    result = _parameter_binding_invariant_error(workflow, [_wp("cfg", WorkflowParameterType.STRING)], [])
    assert result is not None
    _, missing_persisted, missing_from_definition = result
    assert any("cfg (json)" in entry for entry in missing_persisted["workflow"])
    assert any("cfg (string)" in entry for entry in missing_from_definition["workflow"])


# --------------------------------------------------------------------------- #
# compute_failure_signature                                                   #
# --------------------------------------------------------------------------- #


def _param_binding_categories() -> list[dict]:
    return [{"category": "PARAMETER_BINDING_ERROR", "confidence_float": 0.95}]


def test_signature_parameter_binding_ignores_key_name() -> None:
    sig_a = compute_failure_signature(
        frontier_start_label="extract",
        failure_reason="Workflow parameter product_sku should have already been set through workflow run parameters",
        failure_categories=_param_binding_categories(),
        suspicious_success=False,
    )
    sig_b = compute_failure_signature(
        frontier_start_label="extract",
        failure_reason="Workflow parameter ticker should have already been set through workflow run parameters",
        failure_categories=_param_binding_categories(),
        suspicious_success=False,
    )
    assert sig_a is not None
    assert sig_a == sig_b


def test_signature_non_parameter_binding_preserves_text() -> None:
    sig_a = compute_failure_signature(
        frontier_start_label="extract",
        failure_reason="Element not found for selector #foo",
        failure_categories=[{"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8}],
        suspicious_success=False,
    )
    sig_b = compute_failure_signature(
        frontier_start_label="extract",
        failure_reason="Element not found for selector #bar",
        failure_categories=[{"category": "ELEMENT_NOT_FOUND", "confidence_float": 0.8}],
        suspicious_success=False,
    )
    assert sig_a is not None and sig_b is not None
    assert sig_a != sig_b


def test_signature_success_returns_none() -> None:
    assert compute_failure_signature(None, None, None, False) is None


# --------------------------------------------------------------------------- #
# Enforcement nudge selection                                                 #
# --------------------------------------------------------------------------- #


def test_nudge_warn_picks_parameter_binding_when_category_is_binding() -> None:
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.repeated_failure_nudge_emitted_at_streak = 0
    ctx.last_failure_category_top = "PARAMETER_BINDING_ERROR"
    assert _repeated_frontier_failure_nudge(ctx) is POST_PARAMETER_BINDING_WARN_NUDGE


def test_nudge_stop_picks_parameter_binding_when_category_is_binding() -> None:
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_STOP_AT
    ctx.repeated_failure_nudge_emitted_at_streak = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.last_failure_category_top = "PARAMETER_BINDING_ERROR"
    assert _repeated_frontier_failure_nudge(ctx) is POST_PARAMETER_BINDING_STOP_NUDGE


def test_nudge_warn_keeps_generic_when_category_is_other() -> None:
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.repeated_failure_nudge_emitted_at_streak = 0
    ctx.last_failure_category_top = "ELEMENT_NOT_FOUND"
    assert _repeated_frontier_failure_nudge(ctx) is POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE


def test_nudge_stop_keeps_generic_when_category_is_other() -> None:
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_STOP_AT
    ctx.repeated_failure_nudge_emitted_at_streak = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.last_failure_category_top = "ANTI_BOT_DETECTION"
    assert _repeated_frontier_failure_nudge(ctx) is POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE


def test_nudge_below_threshold_returns_none() -> None:
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = 1
    ctx.last_failure_category_top = "PARAMETER_BINDING_ERROR"
    assert _repeated_frontier_failure_nudge(ctx) is None


def test_check_enforcement_latches_parameter_binding_stop_at_stop_level() -> None:
    """Regression: POST_PARAMETER_BINDING_STOP_NUDGE must latch at STOP_AT.

    Without the latch, the stop nudge re-fires every turn once streak reaches
    STOP_AT because emitted stays at ESCALATE_AT. The latch ensures the stop
    nudge emits once, then _repeated_frontier_failure_nudge returns None until
    a different streak/category appears.
    """
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_STOP_AT
    ctx.repeated_failure_nudge_emitted_at_streak = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.last_failure_category_top = "PARAMETER_BINDING_ERROR"
    ctx.last_test_ok = False
    ctx.test_after_update_done = True

    first = _check_enforcement(ctx)
    assert first is POST_PARAMETER_BINDING_STOP_NUDGE
    assert ctx.repeated_failure_nudge_emitted_at_streak == REPEATED_FRONTIER_STREAK_STOP_AT

    # Same state — streak still STOP_AT. Without the latch fix the stop nudge
    # would fire a second time. With the latch it should return None (and let
    # other enforcement branches handle follow-up behavior, e.g. failed-test
    # nudge counting).
    ctx.last_test_ok = False
    assert _repeated_frontier_failure_nudge(ctx) is None


def test_check_enforcement_latches_generic_stop_at_stop_level() -> None:
    """The generic stop nudge must also latch; ensures the refactor preserved
    prior behavior for non-parameter-binding categories."""
    ctx = _make_ctx()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_STOP_AT
    ctx.repeated_failure_nudge_emitted_at_streak = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.last_failure_category_top = "ELEMENT_NOT_FOUND"
    ctx.last_test_ok = False
    ctx.test_after_update_done = True

    first = _check_enforcement(ctx)
    assert first is POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE
    assert ctx.repeated_failure_nudge_emitted_at_streak == REPEATED_FRONTIER_STREAK_STOP_AT
    assert _repeated_frontier_failure_nudge(ctx) is None
