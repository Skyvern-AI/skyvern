from __future__ import annotations

import asyncio
import copy
import json
import textwrap
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from skyvern.forge.sdk.copilot.agent import (
    _build_user_context,
    _code_authoring_repair_context_prompt,
    _prior_run_debug_text,
    _recorded_build_test_outcome_prompt,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    _declared_path_returned_empty_scalar,
    recorded_outcome_from_authoring_repair_context,
    recorded_outcome_from_run_blocks_result,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.output_contracts import code_block_available_contracts_by_label
from skyvern.forge.sdk.copilot.output_utils import (
    _compact_packet_for_aggregate_limit,
    project_build_test_packet_for_llm,
    project_direct_test_handoff_packet_for_llm,
)
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    REPAIR_INSTRUCTION_MAX_CHARS,
    WRAPPER_SCOPE_FAILURE_CLASS,
    WRAPPER_SCOPE_REPAIR_INSTRUCTION,
    finalize_runtime_authoring_repair_context_from_page_observation,
    record_pending_runtime_authoring_repair_context,
    repair_page_evidence_is_admissible,
)
from skyvern.forge.sdk.copilot.tools.run_execution import (
    CopilotExecutionSnapshot,
    _build_recorded_build_test_outcome,
    _ExecutionResult,
    _failure_action_trace_summary,
    _newest_failed_result,
    _record_run_blocks_result,
    _RunExecution,
    build_test_evidence_packet,
)
from skyvern.forge.sdk.workflow.models.code_block_recorder import CODE_BLOCK_FILENAME, user_code_line_from_exception
from skyvern.forge.sdk.workflow.models.workflow import Workflow

_RUN_ID = "wr_analytics_scalar"
_RUN_BROWSER_SESSION_ID = "pbs_run_visible"
_RENDERED_SCALAR = "Website visitors 9.42K"
_VARIANT_RUN_ID = "wr_variant_selection"
_RESTOCK_SELECTOR = "#choice-a"
_PURCHASE_SELECTOR = "#add-to-cart"
_OVERLAY_ID = "notice-overlay"
_RESTOCK_NOTICE_TEXT = "We will let you know when this option is available again."
_COMPLETED_PATH = "visitors"
_CONSENT_PACKET_PATH = Path(__file__).resolve().parent / "fixtures/copilot/consent_cover_repair/packet.json"
_WRAPPER_SCOPE_PACKET_DIR = Path(__file__).resolve().parent / "fixtures/copilot/wrapper_scope_exception"
_WRAPPER_SCOPE_LABEL = "open_and_play_catalog_items"
_WRAPPER_SCOPE_GLOBAL_LINE = "global items_completed, items_started"
_CONSENT_BLOCK_LABEL = "extract_order_documents"
_CONSENT_LAYER_TEXT = "Terms of Service"
_CANDIDATE_RUN_ID = "wr_candidate_price_scope"
_CANDIDATE_LABEL = "choose_candidate"
# A container carries a table's rows or a text block's excerpt and never both: every producer sets
# one in its table branch and the other in the else, and the structured path mirrors that payload.
_CANDIDATE_TABLE_ROWS = ["Aurora list 100 now 80", "Basalt list 120 now 90"]
_CANDIDATE_REGION_CONTAINERS: list[dict[str, object]] = [{"sample_rows": list(_CANDIDATE_TABLE_ROWS)}]
_CANDIDATE_SPREAD_CONTAINERS: list[dict[str, object]] = [
    {"sample_rows": ["Aurora list 100", "Aurora now 80", "Aurora size M"]},
    {"sample_rows": ["Basalt list 120", "Basalt now 90"]},
    {"text_excerpt": "Season promotions 10 off any clearance item"},
]
_CANDIDATE_SPREAD_SUMMARIES = [
    "Aurora list 100",
    "Basalt list 120",
    "Season promotions 10 off any clearance item",
    "Aurora now 80",
    "Basalt now 90",
    "Aurora size M",
]
_CANDIDATE_WRONG_OUTPUT: dict[str, object] = {
    "chosen": "Aurora",
    "now_amount": 10.0,
    "sale_score": 0,
    "candidates": [
        {"identity": "Aurora", "list_amount": 100.0, "now_amount": 80.0},
        {"identity": "Basalt", "list_amount": None, "now_amount": 90.0},
    ],
}


_FAILED_LABEL = "read_visitors"
_FAILED_ACTION = "wait failed code_line=7"
_PREDECESSOR_LABEL = "open_dashboard"
_PREDECESSOR_END_URL = "https://analytics.fixture.test/dashboard?range=30d"
_PREDECESSOR_FINAL_ACTION = "click completed code_line=4"


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


def _two_block_generated_browser_failure() -> dict[str, object]:
    result = _generated_browser_failure()
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    blocks.append(
        {
            "workflow_run_block_id": "wrb_open_dashboard",
            "label": _PREDECESSOR_LABEL,
            "block_type": "code",
            "status": "completed",
        }
    )
    data["requested_block_labels"] = [_PREDECESSOR_LABEL, _FAILED_LABEL]
    data["executed_block_labels"] = [_PREDECESSOR_LABEL, _FAILED_LABEL]
    data["observed_block_end_urls"] = {_PREDECESSOR_LABEL: _PREDECESSOR_END_URL}
    data["per_block_action_observations"] = {
        _PREDECESSOR_LABEL: [_PREDECESSOR_FINAL_ACTION],
        _FAILED_LABEL: [_FAILED_ACTION],
    }
    data["action_observations"] = [_PREDECESSOR_FINAL_ACTION, _FAILED_ACTION]
    return result


def test_two_block_repair_input_names_the_page_the_predecessor_block_ended_on() -> None:
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_copilot_context(), _two_block_generated_browser_failure())
    ).model_dump(mode="json", exclude_none=True)
    repair_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="Repair the recorded two-block failure.",
    )

    assert f'"{_PREDECESSOR_LABEL}": "{_PREDECESSOR_END_URL}"' in repair_input
    assert _PREDECESSOR_FINAL_ACTION in repair_input
    assert _FAILED_ACTION in repair_input
    assert f'"block_label": "{_FAILED_LABEL}"' in repair_input


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


def _variant_selection_failure(
    block_type: str, failed_entry_extra: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": _VARIANT_RUN_ID,
            "browser_session_id": "pbs_variant_selection",
            "overall_status": "failed",
            "requested_block_labels": ["select_variant"],
            "executed_block_labels": ["select_variant"],
            "blocks": [
                {
                    "workflow_run_block_id": "wrb_select_variant",
                    "label": "select_variant",
                    "block_type": block_type,
                    "status": "failed",
                    "failure_reason": (
                        "Failed to execute code block. Reason: TimeoutError: Locator.click: Timeout 3000ms "
                        f'exceeded. <div id="{_OVERLAY_ID}"> intercepts pointer events'
                    ),
                    "error_codes": ["user_code_error"],
                    # Newest first, as the action repository returns it, and with no recorder line stamp:
                    # the exception came from the recorded click, not from the block's own raise.
                    "action_trace": [
                        {
                            "action": "click",
                            "status": "failed",
                            "reasoning": None,
                            "element": _PURCHASE_SELECTOR,
                            **(failed_entry_extra or {}),
                        },
                        {"action": "click", "status": "completed", "reasoning": None, "element": _RESTOCK_SELECTOR},
                        {"action": "goto_url", "status": "completed", "reasoning": None, "element": None},
                    ],
                }
            ],
            "post_run_page_evidence": {
                "workflow_run_id": _VARIANT_RUN_ID,
                "source_browser_session_id": "pbs_variant_selection",
                "source_tool": "inspect_page_for_composition",
                "observed_after_workflow_run": True,
                "current_url": "https://shop.fixture.test/item",
                "page_title": "Item",
                "visible_text_excerpt": _RESTOCK_NOTICE_TEXT,
                "result_containers": [],
            },
        },
    }


def _variant_repair_input(block_type: str, failed_entry_extra: dict[str, object] | None = None) -> str:
    result = _variant_selection_failure(block_type, failed_entry_extra)
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    data["action_trace_summary"] = _failure_action_trace_summary(_newest_failed_result(blocks))
    packet = project_build_test_packet_for_llm(build_test_evidence_packet(_copilot_context(), result)).model_dump(
        mode="json", exclude_none=True
    )
    return _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet),
        user_message="The block never selected a buyable option. Repair it.",
    )


def test_variant_repair_input_names_the_choice_the_failed_code_block_clicked() -> None:
    repair_input = _variant_repair_input("CODE")

    assert f"click {_RESTOCK_SELECTOR} completed" in repair_input
    assert f"click {_PURCHASE_SELECTOR} failed" in repair_input
    assert _OVERLAY_ID in repair_input
    assert _RESTOCK_NOTICE_TEXT in repair_input


def test_native_task_failure_still_projects_actions_without_their_element_ids() -> None:
    repair_input = _variant_repair_input("TASK")

    assert "click completed" in repair_input
    assert _RESTOCK_SELECTOR not in repair_input
    assert _PURCHASE_SELECTOR not in repair_input


def test_repair_input_bounds_the_recorded_response_and_drops_recorded_reasoning() -> None:
    reasoning = "the shopper wants the cheapest option under 40 dollars"
    overlong = "Locator.click: Timeout 3000ms exceeded. " + "page detail " * 200 + "overlong-tail"

    assert reasoning not in _variant_repair_input("CODE", {"reasoning": reasoning})

    bounded = _variant_repair_input("CODE", {"response": overlong})
    assert "overlong-tail" not in bounded
    assert "response=Locator.click: Timeout 3000ms exceeded." in bounded


def test_an_omission_with_no_owning_block_label_reads_no_blocks_at_all() -> None:
    blocks = [{"label": "read_elsewhere", "block_type": "code", "extracted_data": {_COMPLETED_PATH: ""}}]

    assert _declared_path_returned_empty_scalar(blocks, _COMPLETED_PATH, "read_elsewhere") is True
    assert _declared_path_returned_empty_scalar(blocks, _COMPLETED_PATH, "") is False


def test_consent_cover_run_facts_reach_ordinary_repair() -> None:
    """The captured cold-run packet, driven through the renderer an ordinary repair turn reads."""
    packet = json.loads(_CONSENT_PACKET_PATH.read_text())
    ctx = _copilot_context()
    ctx.workflow_yaml = packet["workflow_yaml"]
    ctx.persisted_workflow_yaml = packet["workflow_yaml"]
    projected = project_build_test_packet_for_llm(build_test_evidence_packet(ctx, packet["result"])).model_dump(
        mode="json", exclude_none=True
    )
    repair_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(projected),
        user_message="The saved run never reached the documents table. Repair it.",
    )

    assert _CONSENT_LAYER_TEXT not in packet["workflow_yaml"]
    assert _CONSENT_BLOCK_LABEL in repair_input
    assert "goto_url completed" in repair_input
    assert "Failed to execute code block. Reason: TimeoutError" in repair_input
    assert _CONSENT_LAYER_TEXT in repair_input


def _candidate_scope_page_evidence(containers: list[dict[str, object]]) -> dict[str, object]:
    return {
        "workflow_run_id": _CANDIDATE_RUN_ID,
        "source_browser_session_id": _RUN_BROWSER_SESSION_ID,
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "current_url": "https://catalog.fixture.test/results",
        "page_title": "Matching items",
        "visible_text_excerpt": "Matching items",
        "result_containers": containers,
    }


def _candidate_scope_failure(containers: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": _CANDIDATE_RUN_ID,
            "browser_session_id": _RUN_BROWSER_SESSION_ID,
            "overall_status": "failed",
            "requested_block_labels": [_CANDIDATE_LABEL],
            "executed_block_labels": [_CANDIDATE_LABEL],
            "blocks": [
                {
                    "workflow_run_block_id": "wrb_candidate_scope",
                    "label": _CANDIDATE_LABEL,
                    "block_type": "code",
                    "status": "failed",
                    "failure_reason": "chose a candidate from a page-wide amount",
                    "error_codes": ["user_code_error"],
                    "output": _CANDIDATE_WRONG_OUTPUT,
                },
            ],
            "post_run_page_evidence": _candidate_scope_page_evidence(
                _CANDIDATE_REGION_CONTAINERS if containers is None else containers
            ),
        },
    }


def _candidate_scope_result_summaries(result: dict[str, object]) -> list[str]:
    packet = project_build_test_packet_for_llm(build_test_evidence_packet(_copilot_context(), result))
    assert packet.failure is not None
    assert packet.failure.page_state is not None
    return packet.failure.page_state.result_summaries


def test_candidate_price_scope_projects_every_candidate_row_of_the_result_table() -> None:
    summaries = _candidate_scope_result_summaries(_candidate_scope_failure())

    assert summaries == _CANDIDATE_TABLE_ROWS


def test_candidate_price_scope_keeps_the_wrong_output_and_an_absent_value_absent() -> None:
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_copilot_context(), _candidate_scope_failure())
    )

    registered = [output for output in packet.registered_outputs if output.label == _CANDIDATE_LABEL]
    assert len(registered) == 1
    assert registered[0].output == _CANDIDATE_WRONG_OUTPUT
    assert isinstance(registered[0].output, dict)
    candidates = registered[0].output["candidates"]
    assert isinstance(candidates, list)
    assert isinstance(candidates[1], dict)
    assert candidates[1]["list_amount"] is None

    repair_input = _build_user_context(
        workflow_yaml="",
        chat_history_text="",
        global_llm_context="",
        debug_run_info_text=_prior_run_debug_text(packet.model_dump(mode="json", exclude_none=True)),
        user_message="Compare each candidate on its own amounts.",
    )

    assert '"list_amount": null' in repair_input
    assert "chose a candidate from a page-wide amount" in repair_input


def test_candidate_price_scope_survives_the_aggregate_packet_limit() -> None:
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_copilot_context(), _candidate_scope_failure(_CANDIDATE_SPREAD_CONTAINERS))
    )
    compacted = _compact_packet_for_aggregate_limit(packet, [])

    assert compacted.failure is not None
    assert compacted.failure.page_state is not None
    assert compacted.failure.page_state.result_summaries == _CANDIDATE_SPREAD_SUMMARIES


def test_candidate_price_scope_aggregate_limit_stays_bounded_across_many_regions() -> None:
    regions = ["Aurora", "Basalt", "Cobalt", "Dune"]
    containers: list[dict[str, object]] = [
        {"sample_rows": [f"{region} rank {rank} " + "attribute " * 30 for rank in range(3)]} for region in regions
    ]
    packet = project_build_test_packet_for_llm(
        build_test_evidence_packet(_copilot_context(), _candidate_scope_failure(containers))
    )
    compacted = _compact_packet_for_aggregate_limit(packet, [])

    assert compacted.failure is not None
    assert compacted.failure.page_state is not None
    summaries = compacted.failure.page_state.result_summaries
    assert len(summaries) == 6
    assert all(any(summary.startswith(region) for summary in summaries) for region in regions)


def test_candidate_price_scope_keeps_every_region_in_the_recorded_outcome_page_refs() -> None:
    ctx = _copilot_context()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.pending_code_authoring_runtime_repair_context = CodeAuthoringRepairContext(
        block_label=_CANDIDATE_LABEL,
        reason_code="runtime_block_failure",
        workflow_run_id=_CANDIDATE_RUN_ID,
    )
    ctx.composition_page_evidence = _candidate_scope_page_evidence(_CANDIDATE_SPREAD_CONTAINERS)

    finalized = finalize_runtime_authoring_repair_context_from_page_observation(ctx)

    assert finalized is not None
    refs = recorded_outcome_from_authoring_repair_context(finalized).page_evidence_refs
    assert "result:Basalt list 120" in refs
    assert "result:Basalt now 90" in refs

    prompt = _code_authoring_repair_context_prompt(ctx)
    assert "Aurora now 80" in prompt
    assert "Basalt now 90" in prompt


def test_candidate_price_scope_long_region_cannot_starve_a_later_region() -> None:
    summaries = _candidate_scope_result_summaries(
        _candidate_scope_failure(
            [
                {"sample_rows": [f"Aurora detail {index}" for index in range(9)]},
                {"sample_rows": ["Basalt list 120", "Basalt now 90"]},
                {"text_excerpt": "Season promotions 10 off any clearance item"},
            ]
        )
    )

    assert "Basalt list 120" in summaries
    assert "Basalt now 90" in summaries
    assert "Season promotions 10 off any clearance item" in summaries
    assert not [summary for summary in summaries if "omitted" in summary]


def test_runtime_repair_context_names_the_failure_the_run_stopped_on() -> None:
    ctx = _copilot_context()
    record_pending_runtime_authoring_repair_context(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": _RUN_ID,
                "blocks": [
                    {"label": "search_directory", "status": "failed", "failure_reason": "search stalled"},
                    {"label": "select_first_result", "status": "failed", "failure_reason": "no result row"},
                ],
            },
        },
    )

    pending = ctx.pending_code_authoring_runtime_repair_context
    assert pending is not None
    assert pending.block_label == "select_first_result"


class _StubPage:
    """Every attribute is another stub, every call returns one, and awaiting one yields one, so
    the block runs to its scope failure without a browser."""

    url = "https://example.com/items/1"

    def __getattr__(self, name: str) -> _StubPage:
        return _StubPage()

    def __call__(self, *args: Any, **kwargs: Any) -> _StubPage:
        return _StubPage()

    def __await__(self) -> Generator[None, None, _StubPage]:
        yield from ()
        return _StubPage()

    def __index__(self) -> int:
        return 1

    def __contains__(self, item: object) -> bool:
        return True

    def __add__(self, other: object) -> str:
        return self.url

    def __radd__(self, other: object) -> str:
        return self.url


def _runner_reason_from_executing(code: str) -> str:
    """The runner's reason for this block, from actually running it as the runtime does: the
    block body as the body of a wrapper function, under the runner's filename and line offset."""
    full_code = "\nasync def wrapper():\n" + textwrap.indent(textwrap.dedent(code), "    ") + "\n"
    namespace: dict[str, Any] = {"page": _StubPage(), "setup_only": True}
    exec(compile(full_code, CODE_BLOCK_FILENAME, "exec"), namespace, namespace)  # noqa: S102
    try:
        asyncio.run(namespace["wrapper"]())
    except Exception as exc:
        return f"CodeBlock failed with {type(exc).__name__} at line {user_code_line_from_exception(exc)}: {exc}."
    raise AssertionError("the wrapped block ran without raising")


def _wrapper_scope_packet(name: str) -> dict[str, Any]:
    return json.loads((_WRAPPER_SCOPE_PACKET_DIR / name).read_text(encoding="utf-8"))


def _wrapper_scope_result(failure_reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_wrapper_scope_fixture",
            "overall_status": "failed",
            "blocks": [{"label": _WRAPPER_SCOPE_LABEL, "status": "failed", "failure_reason": failure_reason}],
        },
    }


def _pending_after(result: dict[str, Any], workflow_yaml: str) -> CodeAuthoringRepairContext:
    ctx = _copilot_context()
    record_pending_runtime_authoring_repair_context(ctx, result, workflow_yaml=workflow_yaml)
    pending = ctx.pending_code_authoring_runtime_repair_context
    assert pending is not None
    return pending


def _with_block_code(workflow_yaml: str, code: str) -> str:
    document = yaml.safe_load(workflow_yaml)
    block = next(b for b in document["workflow_definition"]["blocks"] if b["label"] == _WRAPPER_SCOPE_LABEL)
    assert block["code"] != code
    block["code"] = code
    edited = yaml.safe_dump(document, sort_keys=False)
    assert code_block_available_contracts_by_label(edited)[_WRAPPER_SCOPE_LABEL].code == code
    return edited


def _as_second_run(result: dict[str, Any]) -> dict[str, Any]:
    second = copy.deepcopy(result)
    second["data"]["workflow_run_id"] = "wr_wrapper_scope_fixture_second"
    return second


def _record_run(ctx: CopilotContext, result: dict[str, Any], workflow_yaml: str) -> tuple[str, object]:
    ctx.workflow_yaml = workflow_yaml
    _record_run_blocks_result(ctx, result)
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None and outcome.reason_code == "runtime_block_failure"
    return outcome.structural_failure_identity, ctx.recorded_build_test_outcome_history[-1]["structural_key"]


def test_name_error_then_unbound_local_error_on_the_same_counter_are_one_scope_defect() -> None:
    name_error = _wrapper_scope_packet("packet-name-error.json")
    unbound = _wrapper_scope_packet("packet-unbound-local-error.json")
    code_with_global = code_block_available_contracts_by_label(name_error["workflow_yaml"])[_WRAPPER_SCOPE_LABEL].code
    code_without_global = code_block_available_contracts_by_label(unbound["workflow_yaml"])[_WRAPPER_SCOPE_LABEL].code
    assert _WRAPPER_SCOPE_GLOBAL_LINE in code_with_global and _WRAPPER_SCOPE_GLOBAL_LINE not in code_without_global
    assert (
        _runner_reason_from_executing(code_with_global) == name_error["result"]["data"]["blocks"][0]["failure_reason"]
    )
    executed_reason = _runner_reason_from_executing(code_without_global)
    assert executed_reason.startswith("CodeBlock failed with UnboundLocalError at line 47: ")
    assert executed_reason == unbound["result"]["data"]["blocks"][0]["failure_reason"]

    first = _pending_after(name_error["result"], name_error["workflow_yaml"])
    second = _pending_after(_wrapper_scope_result(executed_reason), unbound["workflow_yaml"])

    assert first.runtime_failure_class == WRAPPER_SCOPE_FAILURE_CLASS
    assert second.runtime_failure_class == WRAPPER_SCOPE_FAILURE_CLASS
    assert first.repair_instruction == second.repair_instruction == WRAPPER_SCOPE_REPAIR_INSTRUCTION
    assert len(WRAPPER_SCOPE_REPAIR_INSTRUCTION) <= REPAIR_INSTRUCTION_MAX_CHARS


def test_the_second_scope_exception_records_the_same_outcome_identity_as_the_first() -> None:
    name_error = _wrapper_scope_packet("packet-name-error.json")
    unbound = _wrapper_scope_packet("packet-unbound-local-error.json")
    ctx = _copilot_context()

    first_identity, first_key = _record_run(ctx, name_error["result"], name_error["workflow_yaml"])
    second_identity, second_key = _record_run(ctx, _as_second_run(unbound["result"]), unbound["workflow_yaml"])

    assert first_identity and first_identity == second_identity
    assert first_key is not None and first_key == second_key
    assert len(ctx.recorded_build_test_outcome_history) == 2


def test_a_regrade_after_the_repair_context_is_finalized_keeps_the_first_identity() -> None:
    name_error = _wrapper_scope_packet("packet-name-error.json")
    ctx = _copilot_context()
    first_identity, _ = _record_run(ctx, name_error["result"], name_error["workflow_yaml"])
    ctx.last_code_authoring_repair_context = ctx.pending_code_authoring_runtime_repair_context
    ctx.pending_code_authoring_runtime_repair_context = None

    regraded = _build_recorded_build_test_outcome(ctx, name_error["result"], None)

    assert regraded is not None and regraded.structural_failure_identity == first_identity


def test_an_unrelated_name_error_keeps_its_own_outcome_identity() -> None:
    name_error = _wrapper_scope_packet("packet-name-error.json")
    unbound = _wrapper_scope_packet("packet-unbound-local-error.json")
    ctx = _copilot_context()
    typo = copy.deepcopy(name_error["result"])
    typo["data"]["blocks"][0]["failure_reason"] = (
        "CodeBlock failed with NameError at line 48: name 'typo' is not defined."
    )

    typo_identity, typo_key = _record_run(ctx, typo, name_error["workflow_yaml"])
    scope_identity, scope_key = _record_run(ctx, _as_second_run(unbound["result"]), unbound["workflow_yaml"])

    assert typo_identity != scope_identity
    assert typo_key != scope_key


def test_an_inner_helpers_global_does_not_hide_the_outer_helpers_scope_defect() -> None:
    packet = _wrapper_scope_packet("packet-name-error.json")
    code = textwrap.dedent(
        """\
        count = 0

        async def outer():
            async def inner():
                global count
                count = 5

            await inner()
            count += 1

        await outer()
        return {"count": count}
        """
    )
    reason = _runner_reason_from_executing(code)
    assert reason.startswith("CodeBlock failed with UnboundLocalError at line 9: ")

    pending = _pending_after(_wrapper_scope_result(reason), _with_block_code(packet["workflow_yaml"], code))

    assert pending.runtime_failure_class == WRAPPER_SCOPE_FAILURE_CLASS


def test_executed_snapshot_yaml_outranks_the_context_draft_for_scope_classification() -> None:
    packet = _wrapper_scope_packet("packet-name-error.json")
    fixed_yaml = _with_block_code(packet["workflow_yaml"], 'items_started = 0\nreturn {"items_started": 0}\n')
    ctx = _copilot_context()
    ctx.workflow_yaml = fixed_yaml

    record_pending_runtime_authoring_repair_context(ctx, packet["result"])
    from_draft = ctx.pending_code_authoring_runtime_repair_context
    record_pending_runtime_authoring_repair_context(ctx, packet["result"], workflow_yaml=packet["workflow_yaml"])
    from_snapshot = ctx.pending_code_authoring_runtime_repair_context

    assert from_draft is not None and from_draft.runtime_failure_class is None
    assert from_snapshot is not None and from_snapshot.runtime_failure_class == WRAPPER_SCOPE_FAILURE_CLASS


def _execution_result(result: dict[str, Any], executed_workflow_yaml: str) -> _ExecutionResult:
    workflow = Workflow.model_construct(workflow_id="wf_fixture", workflow_definition=SimpleNamespace(blocks=[]))
    execution = _RunExecution(
        snapshot=CopilotExecutionSnapshot(
            provenance="staged",
            workflow=workflow,
            workflow_parameters=(),
            output_parameters=(),
            workflow_yaml=executed_workflow_yaml,
        ),
        workflow_yaml=executed_workflow_yaml,
        metadata={},
        associations={},
        source_at_start=None,
        unbound_keys=[],
        explicit_blank=False,
    )
    return _ExecutionResult(result, execution)


def test_recording_an_execution_result_classifies_against_the_snapshot_it_ran_not_the_context_draft() -> None:
    packet = _wrapper_scope_packet("packet-name-error.json")
    ctx = _copilot_context()
    ctx.workflow_yaml = _with_block_code(packet["workflow_yaml"], 'items_started = 0\nreturn {"items_started": 0}\n')

    _record_run_blocks_result(ctx, _execution_result(packet["result"], packet["workflow_yaml"]))

    pending = ctx.pending_code_authoring_runtime_repair_context
    assert pending is not None and pending.runtime_failure_class == WRAPPER_SCOPE_FAILURE_CLASS
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None and outcome.reason_code == "runtime_block_failure"


@pytest.mark.parametrize(
    ("failure_reason", "code_edit"),
    [
        ("CodeBlock failed with NameError at line 48: name 'typo' is not defined.", None),
        (
            "CodeBlock failed with NameError at line 48: name 'typo_total' is not defined.",
            ("    global items_completed, items_started\n", "    global items_completed, items_started, typo_total\n"),
        ),
        ("CodeBlock failed with NameError at line 12: name 'items_started' is not defined.", None),
        (
            "CodeBlock failed with NameError at line 48: name 'never_bound' is not defined.",
            ("    global items_completed, items_started\n", "    global never_bound\n"),
        ),
        (
            "CodeBlock failed with UnboundLocalError at line 47: cannot access local variable 'items_started' "
            "where it is not associated with a value.",
            ("    global items_completed, items_started\n", "    nonlocal items_completed, items_started\n"),
        ),
        (
            "CodeBlock failed with UnboundLocalError at line 47: cannot access local variable 'never_bound' "
            "where it is not associated with a value.",
            None,
        ),
        (
            "CodeBlock failed with UnboundLocalError at line 29: cannot access local variable 'pages_visited' "
            "where it is not associated with a value.",
            (
                "    global items_completed, items_started\n",
                "    if link_count:\n        pages_visited = 1\n    print(pages_visited)\n",
            ),
        ),
    ],
    ids=[
        "unrelated_name_with_dormant_global",
        "never_bound_name_sharing_a_global_with_a_bound_one",
        "name_error_outside_the_helper",
        "never_bound_name",
        "nonlocal_present",
        "unbound_local_on_a_never_bound_name",
        "conditionally_initialized_local_sharing_a_top_level_name",
    ],
)
def test_scope_class_needs_the_failing_name_and_line_to_land_in_the_helper(
    failure_reason: str, code_edit: tuple[str, str] | None
) -> None:
    packet = _wrapper_scope_packet("packet-name-error.json")
    workflow_yaml = packet["workflow_yaml"]
    if code_edit is not None:
        code = code_block_available_contracts_by_label(workflow_yaml)[_WRAPPER_SCOPE_LABEL].code
        assert code_edit[0] in code
        workflow_yaml = _with_block_code(workflow_yaml, code.replace(code_edit[0], code_edit[1]))

    pending = _pending_after(_wrapper_scope_result(failure_reason), workflow_yaml)

    assert pending.reason_code == "runtime_block_failure"
    assert pending.runtime_failure_class is None
    assert "wrapper" not in pending.repair_instruction


def test_a_completed_sibling_row_keeps_its_page_facts_beside_a_failed_rows_page_state() -> None:
    result = _generated_browser_failure()
    data = result["data"]
    assert isinstance(data, dict)
    blocks = data["blocks"]
    assert isinstance(blocks, list)
    blocks.insert(
        0,
        {
            "workflow_run_block_id": "wrb_sign_in",
            "label": "sign_in",
            "block_type": "code",
            "status": "completed",
            "output": {"current_url": "https://analytics.fixture.test/login", "page_evidence": ""},
        },
    )
    evidence = data["post_run_page_evidence"]
    assert isinstance(evidence, dict)

    outcome = recorded_outcome_from_run_blocks_result(result, page_evidence=evidence)
    assert outcome is not None

    ctx = _copilot_context()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.latest_recorded_build_test_outcome = outcome
    rows = [line for line in _recorded_build_test_outcome_prompt(ctx).splitlines() if line.startswith("- label=")]

    assert rows == [
        (
            "- label=sign_in; status=completed; output.current_url=https://analytics.fixture.test/login; "
            "output.page_evidence=(empty)"
        ),
        "- label=read_visitors; status=failed; recorded_output=(none recorded)",
    ]
