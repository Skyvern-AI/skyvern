from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _build_user_context,
    _code_authoring_repair_context_prompt,
    _prior_run_debug_text,
)
from skyvern.forge.sdk.copilot.build_test_outcome import recorded_outcome_from_run_blocks_result
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.output_utils import (
    project_build_test_packet_for_llm,
    project_direct_test_handoff_packet_for_llm,
)
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    finalize_runtime_authoring_repair_context_from_page_observation,
    repair_page_evidence_is_admissible,
)
from skyvern.forge.sdk.copilot.tools.run_execution import build_test_evidence_packet

_RUN_ID = "wr_analytics_scalar"
_RUN_BROWSER_SESSION_ID = "pbs_run_visible"
_RENDERED_SCALAR = "Website visitors 9.42K"


def _copilot_context() -> CopilotContext:
    return CopilotContext(
        organization_id="org_fixture",
        workflow_id="wf_fixture",
        workflow_permanent_id="wpid_fixture",
        workflow_yaml="workflow_definition:\n  blocks: []\n",
        persisted_workflow_yaml="workflow_definition:\n  blocks: []\n",
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
        api_key=None,
    )


def _generated_browser_failure() -> dict[str, object]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": _RUN_ID,
            "browser_session_id": _RUN_BROWSER_SESSION_ID,
            "overall_status": "failed",
            "blocks": [
                {
                    "workflow_run_block_id": "wrb_read_visitors",
                    "label": "read_visitors",
                    "block_type": "code",
                    "status": "failed",
                    "failure_reason": "The generated browser operation failed after the page rendered.",
                    "error_codes": ["browser_operation_failed"],
                }
            ],
            "failing_code_line": 7,
            "authoring_repair_context": {
                "workflow_run_id": _RUN_ID,
                "current_origin": "https://analytics.fixture.test",
                "current_url": "https://analytics.fixture.test/dashboard",
                "current_title": "Pathfold Analytics",
                "page_evidence_source": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "rendered_value_excerpt": _RENDERED_SCALAR,
            },
            "post_run_page_evidence": {
                "workflow_run_id": _RUN_ID,
                "source_browser_session_id": _RUN_BROWSER_SESSION_ID,
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "current_url": "https://analytics.fixture.test/dashboard",
                "page_title": "Pathfold Analytics",
                # The production-shaped failure: a scalar rendered in the run browser is not a
                # classified result container, so it must remain a bounded page fact instead.
                "visible_text_excerpt": _RENDERED_SCALAR,
                "result_containers": [],
            },
        },
    }


@pytest.mark.parametrize("attempt", range(3))
def test_generated_browser_repair_keeps_run_visible_scalar_in_ordinary_repair(attempt: int) -> None:
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_copilot_context(), _generated_browser_failure())
    ).model_dump(mode="json", exclude_none=True)
    ordinary_repair_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="Repair the recorded generated browser failure.",
    )

    assert attempt in range(3)
    assert f'"workflow_run_id": "{_RUN_ID}"' in ordinary_repair_input
    assert f'"browser_session_id": "{_RUN_BROWSER_SESSION_ID}"' in ordinary_repair_input
    assert _RENDERED_SCALAR in ordinary_repair_input


def test_scalar_only_run_visible_evidence_is_admitted_but_not_sent_to_direct_test_handoff() -> None:
    result = _generated_browser_failure()
    data = result["data"]
    assert isinstance(data, dict)
    evidence = data["post_run_page_evidence"]
    assert isinstance(evidence, dict)
    assert repair_page_evidence_is_admissible(evidence) is True

    direct_handoff = project_direct_test_handoff_packet_for_llm(build_test_evidence_packet(_copilot_context(), result))

    assert direct_handoff.failure is not None
    assert direct_handoff.failure.page_state is not None
    assert direct_handoff.failure.page_state.rendered_value_excerpt is None


def test_generated_repair_context_and_recorded_outcome_preserve_the_rendered_scalar() -> None:
    result = _generated_browser_failure()
    data = result["data"]
    assert isinstance(data, dict)
    evidence = data["post_run_page_evidence"]
    assert isinstance(evidence, dict)
    outcome = recorded_outcome_from_run_blocks_result(result, page_evidence=evidence)

    assert outcome is not None
    assert outcome.observed_page_value_excerpt == _RENDERED_SCALAR

    ctx = _copilot_context()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.pending_code_authoring_runtime_repair_context = CodeAuthoringRepairContext(
        block_label="read_visitors",
        reason_code="runtime_block_failure",
        workflow_run_id=_RUN_ID,
    )
    ctx.composition_page_evidence = evidence

    finalized = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert finalized is not None
    assert finalized.rendered_value_excerpt == _RENDERED_SCALAR
    assert _RENDERED_SCALAR in _code_authoring_repair_context_prompt(ctx)
