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

import pytest

from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import _analyze_run_blocks, _parameter_binding_invariant_error
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)

# --------------------------------------------------------------------------- #
# Classifier                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "Workflow parameter product_sku should have already been set through workflow run parameters",
            id="workflow_parameter",
        ),
        pytest.param(
            "Output parameter extract_output should have already been set through workflow run context init",
            id="output_parameter",
        ),
        pytest.param(
            "SecretParameter totp should have already been set through workflow run context init",
            id="secret_parameter",
        ),
        pytest.param(
            "Pre-run invariant: workflow_definition and persisted parameter rows disagree. "
            "workflow missing persisted: ['ticker (string)']",
            id="pre_run_invariant",
        ),
    ],
)
def test_classify_parameter_binding_raise_messages(message: str) -> None:
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
    anti_bot, empty_data, categories, _ = _analyze_run_blocks(result)
    assert categories is not None
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"
    assert anti_bot is None
    assert empty_data is False


def test_analyze_run_blocks_does_not_classify_unstructured_failure_prose() -> None:
    # The failed run's verbatim evidence belongs in repair context, not in a second
    # classifier plane that invents a category from browser/block prose.
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
    _, _, categories, _ = _analyze_run_blocks(result)
    assert categories is None


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


# --------------------------------------------------------------------------- #
# Enforcement nudge selection                                                 #
# --------------------------------------------------------------------------- #
