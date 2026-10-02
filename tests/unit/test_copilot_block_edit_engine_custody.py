"""Block-scoped repairs distinguish legacy editor defaults from explicit engine changes."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.workflow.models.block import CodeBlock, ExtractionBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.schemas.runs import RunEngine
from skyvern.utils.yaml_loader import dump_workflow_yaml, safe_load_no_dates
from tests.unit.copilot_test_helpers import make_copilot_ctx

DRAFT = """title: Repair
workflow_definition:
  parameters: []
  blocks:
    - block_type: extraction
      label: extract_value
      data_extraction_goal: Read the value
      url: https://example.test/
    - block_type: code
      label: repair
      code: result = value + missing_adjustment
"""


def _definition(engine: RunEngine) -> WorkflowDefinition:
    now = datetime(2026, 9, 30, tzinfo=UTC)

    def output(label: str) -> OutputParameter:
        return OutputParameter(
            key=f"{label}_output",
            output_parameter_id=f"op_{label}",
            workflow_id="workflow",
            created_at=now,
            modified_at=now,
        )

    return WorkflowDefinition(
        parameters=[],
        blocks=[
            ExtractionBlock(
                label="extract_value",
                engine=engine,
                data_extraction_goal="Read the value",
                url="https://example.test/",
                output_parameter=output("extract_value"),
            ),
            CodeBlock(label="repair", code="result = value + missing_adjustment", output_parameter=output("repair")),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["edit_block", "edit_block_and_run"])
@pytest.mark.parametrize(
    ("engine", "explicit_null"),
    [(RunEngine.skyvern_v1, False), (RunEngine.skyvern_v2, True), (RunEngine.skyvern_v3, False)],
)
async def test_anchored_repair_restores_only_legacy_editor_engine_default(
    monkeypatch: pytest.MonkeyPatch,
    engine: RunEngine,
    tool_name: str,
    explicit_null: bool,
) -> None:
    ctx = make_copilot_ctx()
    ctx.workflow_yaml = DRAFT.replace(
        "      url: https://example.test/",
        "      url: https://example.test/\n      title: extract_value\n      export_data_schema: {type: array, items: {type: object, properties: {value: {type: string}}}}",
    )
    if explicit_null:
        ctx.workflow_yaml = ctx.workflow_yaml.replace(
            "      label: extract_value", "      label: extract_value\n      engine: null"
        )
    prior = _definition(engine)
    update = AsyncMock(return_value={"ok": False, "error": "stop after the persistence handoff"})
    monkeypatch.setattr(tools, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools, "_update_and_run_requires_skipped_run", lambda *_args: False)
    monkeypatch.setattr(tools, "_get_prior_workflow_definition", AsyncMock(return_value=prior))
    monkeypatch.setattr(tools, "_update_workflow", update)
    monkeypatch.setattr(tools, "_record_workflow_update_result", lambda *_args: None)
    await getattr(tools, f"{tool_name}_tool").on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name=tool_name),
        json.dumps(
            {
                "label": "repair",
                "expected_code": " + missing_adjustment",
                "replacement_code": "",
                **({"block_labels": ["extract_value", "repair"]} if tool_name == "edit_block_and_run" else {}),
            }
        ),
    )
    document = safe_load_no_dates(update.call_args.args[0]["workflow_yaml"])
    producer, repaired = document["workflow_definition"]["blocks"]
    assert producer.get("engine") == (engine.value if engine == RunEngine.skyvern_v1 else None)
    assert producer["title"] == ""
    assert producer.get("export_data_schema") is None
    from skyvern.forge.sdk.copilot.repair_origin_run import (
        OriginBlockOutput,
        OriginOutputSnapshot,
        SelectedOutputSource,
    )
    from skyvern.forge.sdk.copilot.tools.frontier import selected_output_definition_refusal
    from skyvern.forge.sdk.copilot.workflow_yaml import _copilot_definition_from_yaml

    candidate = _copilot_definition_from_yaml(update.call_args.args[0]["workflow_yaml"], "workflow")[1]
    completed = OriginBlockOutput("completed", True, datetime(2026, 9, 30, tzinfo=UTC), {"value": 7})
    selected = SelectedOutputSource(
        "extract_value", "source_run", "banked", OriginOutputSnapshot(prior, {"extract_value": completed})
    )
    refusal = selected_output_definition_refusal({"extract_value": selected}, candidate, "workflow")
    if engine == RunEngine.skyvern_v1:
        assert refusal is None
    else:
        # Default replaces an explicit V2/V3 pin, so its old output cannot be reused.
        from skyvern.forge.sdk.copilot.repair_origin_run import OriginOutputRefusal

        assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_PRODUCER
    from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
    from skyvern.forge.sdk.copilot.tools.banned_blocks import reject_authoring_violations

    ctx.block_authoring_policy = BlockAuthoringPolicy.STANDARD
    admission = reject_authoring_violations(
        ctx,
        update.call_args.args[0]["workflow_yaml"],
        "_update_workflow",
        prior_workflow_yaml=update.call_args.kwargs.get("block_scoped_authoring_prior_yaml"),
    )
    assert admission.reject is None
    assert repaired["code"] == "result = value"
    assert "engine" not in repaired


def test_nested_untouched_producer_engine_is_preserved() -> None:
    from skyvern.forge.sdk.copilot.workflow_yaml import preserve_untouched_block_configuration
    from skyvern.forge.sdk.workflow.models.block import ForLoopBlock
    from skyvern.utils.yaml_loader import dump_workflow_yaml

    prior = _definition(RunEngine.skyvern_v1)
    extraction, repair = prior.blocks
    prior.blocks = [
        ForLoopBlock(label="loop", loop_blocks=[extraction], output_parameter=repair.output_parameter),
        repair,
    ]
    draft = safe_load_no_dates(DRAFT)
    producer, repaired = draft["workflow_definition"]["blocks"]
    draft["workflow_definition"]["blocks"] = [
        {"label": "loop", "block_type": "for_loop", "loop_blocks": [producer]},
        repaired,
    ]
    restored = preserve_untouched_block_configuration(dump_workflow_yaml(draft), prior, edited_label="repair")
    nested = safe_load_no_dates(restored)["workflow_definition"]["blocks"][0]["loop_blocks"][0]
    assert nested["engine"] == RunEngine.skyvern_v1.value


def test_a_marker_on_a_non_v1_saved_block_does_not_pin_a_draft_v1() -> None:
    from skyvern.forge.sdk.copilot.workflow_yaml import preserve_untouched_block_configuration

    prior = _definition(RunEngine.skyvern_v3)
    prior.blocks[0].engine_pinned = True
    draft = DRAFT.replace("      label: extract_value", "      label: extract_value\n      engine: skyvern-1.0")
    restored = preserve_untouched_block_configuration(draft, prior, edited_label="repair")
    producer = safe_load_no_dates(restored)["workflow_definition"]["blocks"][0]
    assert not producer.get("engine_pinned")


def test_untouched_pinned_skyvern_v1_keeps_its_pin() -> None:
    from skyvern.forge.sdk.copilot.workflow_yaml import preserve_untouched_block_configuration

    prior = _definition(RunEngine.skyvern_v1)
    prior.blocks[0].engine_pinned = True
    draft = safe_load_no_dates(DRAFT)
    draft["workflow_definition"]["blocks"][0]["engine"] = RunEngine.skyvern_v1.value
    restored = preserve_untouched_block_configuration(dump_workflow_yaml(draft), prior, edited_label="repair")
    producer = safe_load_no_dates(restored)["workflow_definition"]["blocks"][0]
    assert producer["engine"] == RunEngine.skyvern_v1.value
    assert producer["engine_pinned"] is True


def test_default_pick_on_pinned_skyvern_v1_is_not_re_pinned() -> None:
    from skyvern.forge.sdk.copilot.workflow_yaml import preserve_untouched_block_configuration

    prior = _definition(RunEngine.skyvern_v1)
    prior.blocks[0].engine_pinned = True
    restored = preserve_untouched_block_configuration(DRAFT, prior, edited_label="repair")
    producer = safe_load_no_dates(restored)["workflow_definition"]["blocks"][0]
    assert "engine" not in producer
    assert not producer.get("engine_pinned")


@pytest.mark.parametrize(
    "label, fields, edited_label",
    [
        ("extract_value", {"url": "https://example.test/changed"}, "repair"),
        ("repair", {"code": "result = 100"}, "extract_value"),
    ],
)
def test_explicit_untouched_configuration_survives_and_refuses_stale_receipt(
    label: str,
    fields: dict,
    edited_label: str,
) -> None:
    from skyvern.forge.sdk.copilot.repair_origin_run import (
        OriginOutputRefusal,
        OriginOutputSnapshot,
        SelectedOutputSource,
    )
    from skyvern.forge.sdk.copilot.tools.frontier import selected_output_definition_refusal
    from skyvern.forge.sdk.copilot.workflow_yaml import (
        _copilot_definition_from_yaml,
        apply_block_edit,
        preserve_untouched_block_configuration,
    )

    prior = _definition(RunEngine.skyvern_v1)
    draft = apply_block_edit(DRAFT, label, fields=fields)
    preserved = preserve_untouched_block_configuration(draft, prior, edited_label=edited_label)
    blocks = safe_load_no_dates(preserved)["workflow_definition"]["blocks"]
    changed = next(block for block in blocks if block["label"] == label)
    assert all(changed[key] == value for key, value in fields.items())
    candidate = _copilot_definition_from_yaml(preserved, "workflow")[1]
    source = SelectedOutputSource(label, "source_run", "banked", OriginOutputSnapshot(prior, {}))
    refusal = selected_output_definition_refusal({label: source}, candidate, "workflow")
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_PRODUCER


@pytest.mark.asyncio
async def test_general_update_cannot_spoof_scoped_authoring_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.tools import workflow_update
    from skyvern.forge.sdk.copilot.workflow_yaml import apply_block_edit

    ctx = make_copilot_ctx(request_policy=RequestPolicy(allow_update_workflow=True))
    submitted = apply_block_edit(DRAFT, "extract_value", fields={"engine": RunEngine.skyvern_v1.value})
    monkeypatch.setattr(workflow_update, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(workflow_update, "_credential_reference_validation_error", AsyncMock(return_value=None))
    result = await workflow_update._update_workflow(
        {"workflow_yaml": submitted, "_block_scoped_authoring_prior_yaml": submitted},
        ctx,
    )
    assert result["ok"] is False and result["block_id"] == "banned_blocks"
    assert any(fact["code"] == "engine_not_skyvern_v3" for fact in result["data"]["violations"])


def test_block_repair_keeps_an_explicit_engine_in_the_current_draft() -> None:
    from skyvern.forge.sdk.copilot.workflow_yaml import apply_block_edit, preserve_untouched_block_configuration

    draft = apply_block_edit(DRAFT, "extract_value", fields={"engine": RunEngine.skyvern_v3.value})
    repaired = preserve_untouched_block_configuration(draft, _definition(RunEngine.skyvern_v1), edited_label="repair")
    assert safe_load_no_dates(repaired)["workflow_definition"]["blocks"][0]["engine"] == RunEngine.skyvern_v3.value
