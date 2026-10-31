"""Tests for the PDF/SOP import service's LLM-output sanitizer."""

from copy import deepcopy
from typing import Any

import pytest

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionHasDuplicateBlockLabels
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import BLOCK_NOTE_MAX_LENGTH, WorkflowCreateYAMLRequest
from skyvern.services import pdf_import_service as pdf_import_service_module
from skyvern.services.pdf_import_service import pdf_import_service


def _definition(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Imported SOP",
        "workflow_definition": {"parameters": [], "blocks": list(blocks)},
    }


def _code_block(**overrides: Any) -> dict[str, Any]:
    return {"label": "compute", "block_type": "code", "continue_on_failure": False, "code": "x = 1", **overrides}


def test_doc_notes_do_not_change_parameter_keys_or_prefixed_references() -> None:
    raw = _definition(
        {
            "label": "open_account",
            "block_type": "navigation",
            "url": "https://example.com",
            "navigation_goal": "Open account {{ parameters._account_id }}",
            "parameter_keys": ["_account_id"],
        }
    )
    raw["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": "_account_id", "workflow_parameter_type": "string"}
    ]

    disabled = pdf_import_service._sanitize_workflow_json(deepcopy(raw))
    enabled = pdf_import_service._sanitize_workflow_json(deepcopy(raw), doc_editor_enabled=True)

    assert enabled == disabled
    block = enabled["workflow_definition"]["blocks"][0]
    assert block["navigation_goal"] == "Open account {{ _account_id }}"
    assert block["parameter_keys"] == ["_account_id"]
    WorkflowCreateYAMLRequest.model_validate(enabled)


def test_nested_label_cleanup_preserves_declared_parameter_keys() -> None:
    raw = _definition(
        {
            "label": "each",
            "block_type": "for_loop",
            "loop_over_parameter_key": "items",
            "loop_blocks": [
                {
                    "label": "sign in",
                    "block_type": "login",
                    "url": "https://example.com",
                    "navigation_goal": "Log into {{ region }}",
                    "parameter_keys": ["credential"],
                }
            ],
        }
    )
    raw["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": key, "workflow_parameter_type": "string"}
        for key in ("items", "region", "credential")
    ]

    sanitized = pdf_import_service._sanitize_workflow_json(raw, doc_editor_enabled=True)

    child = sanitized["workflow_definition"]["blocks"][0]["loop_blocks"][0]
    assert child["label"] == "sign_in"
    assert child["parameter_keys"] == ["credential"]
    WorkflowCreateYAMLRequest.model_validate(sanitized)


@pytest.mark.parametrize("loop_first", [True, False])
def test_cross_level_duplicate_labels_fail_conversion_instead_of_retargeting_consumers(loop_first: bool) -> None:
    loop = {
        "label": "each",
        "block_type": "for_loop",
        "loop_over_parameter_key": "items",
        "loop_blocks": [
            _code_block(label="extract_data"),
            _code_block(label="read_inner", prompt="Use {{ extract_data_output }}"),
        ],
    }
    producer = _code_block(label="extract_data")
    blocks = [loop, producer] if loop_first else [producer, loop]
    raw = _definition(*blocks, _code_block(label="consume", prompt="Use {{ extract_data_output }}"))
    raw["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": "items", "workflow_parameter_type": "json", "default_value": []}
    ]

    for enabled in (False, True):
        sanitized = pdf_import_service._sanitize_workflow_json(deepcopy(raw), doc_editor_enabled=enabled)
        request = WorkflowCreateYAMLRequest.model_validate(sanitized)
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            convert_workflow_definition(request.workflow_definition, workflow_id="w_test")


def test_code_block_without_a_prompt_gets_the_code_first_default() -> None:
    sanitized = pdf_import_service._sanitize_workflow_json(_definition(_code_block()))

    # A non-null prompt is what makes the editor render the code-first node; the LLM never
    # writes one, so an imported code block would otherwise land as a legacy node.
    assert sanitized["workflow_definition"]["blocks"][0]["prompt"] == ""


def test_an_authored_prompt_is_preserved() -> None:
    sanitized = pdf_import_service._sanitize_workflow_json(_definition(_code_block(prompt="Total the invoice rows")))

    assert sanitized["workflow_definition"]["blocks"][0]["prompt"] == "Total the invoice rows"


def test_explicit_null_prompt_opts_out() -> None:
    sanitized = pdf_import_service._sanitize_workflow_json(_definition(_code_block(prompt=None)))

    assert sanitized["workflow_definition"]["blocks"][0]["prompt"] is None


def test_non_code_blocks_are_not_given_a_prompt() -> None:
    block = {"label": "wait_a_bit", "block_type": "wait", "continue_on_failure": False, "wait_sec": 1}

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(block), doc_editor_enabled=True)

    assert "prompt" not in sanitized["workflow_definition"]["blocks"][0]


def test_imported_notes_are_repaired_before_schema_validation() -> None:
    block = {
        "label": "wait_a_bit",
        "block_type": "wait",
        "continue_on_failure": False,
        "wait_sec": 1,
        "notes": "x" * BLOCK_NOTE_MAX_LENGTH + "{{ credential_input }}",
        "trailing_notes": {"not": "text"},
    }

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(block), doc_editor_enabled=True)
    imported = WorkflowCreateYAMLRequest.model_validate(sanitized)
    imported_block = imported.workflow_definition.blocks[0]

    assert imported_block.notes == "x" * BLOCK_NOTE_MAX_LENGTH
    assert imported_block.trailing_notes is None


def test_nested_loop_notes_are_repaired_when_doc_editor_is_enabled() -> None:
    child = {
        "label": "wait_a_bit",
        "block_type": "wait",
        "continue_on_failure": False,
        "wait_sec": 1,
        "notes": "x" * (BLOCK_NOTE_MAX_LENGTH + 1),
    }
    loop = {
        "label": "for_each_item",
        "block_type": "for_loop",
        "continue_on_failure": False,
        "loop_over_parameter_key": "items",
        "loop_blocks": [child],
    }

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(loop), doc_editor_enabled=True)

    assert sanitized["workflow_definition"]["blocks"][0]["loop_blocks"][0]["notes"] == "x" * BLOCK_NOTE_MAX_LENGTH


def test_flag_off_pdf_import_drops_notes_from_nested_blocks_before_save() -> None:
    child = {
        "label": "wait_a_bit",
        "block_type": "wait",
        "continue_on_failure": False,
        "wait_sec": 1,
        "notes": "model-generated note",
        "trailing_notes": "model-generated trailing note",
    }
    loop = {
        "label": "for_each_item",
        "block_type": "for_loop",
        "continue_on_failure": False,
        "loop_over_parameter_key": "items",
        "loop_blocks": [child],
        "notes": "top-level model-generated note",
    }

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(loop))
    saved_workflow = WorkflowCreateYAMLRequest.model_validate(sanitized)
    saved_loop = saved_workflow.workflow_definition.blocks[0]
    saved_child = saved_loop.loop_blocks[0]

    assert saved_loop.notes is None
    assert saved_child.notes is None
    assert saved_child.trailing_notes is None


def test_pdf_import_prompt_omits_notes_guidance_when_doc_editor_field_is_absent() -> None:
    prompt = prompt_engine.load_prompt("build-workflow-from-pdf", sop_text="Navigate to the dashboard.")

    assert "KEEP EXPLANATIONS AS NOTES" not in prompt


def test_nested_whitespace_repair_keeps_duplicate_labels_for_conversion() -> None:
    def loop() -> dict[str, Any]:
        child = {"label": "wait a bit", "block_type": "wait", "continue_on_failure": False, "wait_sec": 1}
        return {
            "label": "for_each_item",
            "block_type": "for_loop",
            "continue_on_failure": False,
            "loop_over_parameter_key": "items",
            "loop_blocks": [child, dict(child)],
        }

    flag_off = pdf_import_service._sanitize_workflow_json(_definition(loop()))
    flag_on = pdf_import_service._sanitize_workflow_json(_definition(loop()), doc_editor_enabled=True)

    off_labels = [child["label"] for child in flag_off["workflow_definition"]["blocks"][0]["loop_blocks"]]
    on_labels = [child["label"] for child in flag_on["workflow_definition"]["blocks"][0]["loop_blocks"]]
    assert off_labels == ["wait a bit", "wait a bit"]
    assert on_labels == ["wait_a_bit", "wait_a_bit"]
    request = WorkflowCreateYAMLRequest.model_validate(flag_on)
    with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
        convert_workflow_definition(request.workflow_definition, workflow_id="w_test")


def test_nested_label_cleanup_does_not_redirect_parent_output_references() -> None:
    nested_producer = _code_block(label="extract_data", next_block_label="read result")
    nested_consumer = _code_block(label="read result", prompt="Use {{ extract_data_output }}")
    loop = {
        "label": "for_each_item",
        "block_type": "for_loop",
        "continue_on_failure": False,
        "loop_over_parameter_key": "items",
        "loop_blocks": [nested_producer, nested_consumer],
    }

    sanitized = pdf_import_service._sanitize_workflow_json(
        _definition(
            _code_block(label="extract_data"),
            loop,
            _code_block(label="read_outer", prompt="Use {{ extract_data_output }}"),
        ),
        doc_editor_enabled=True,
    )
    imported_loop = sanitized["workflow_definition"]["blocks"][1]
    imported_producer, imported_consumer = imported_loop["loop_blocks"]

    assert imported_producer["label"] == "extract_data"
    assert imported_consumer["label"] == "read_result"
    assert sanitized["workflow_definition"]["blocks"][2]["prompt"] == "Use {{ extract_data_output }}"


def test_doc_notes_do_not_redirect_top_level_output_references() -> None:
    raw = _definition(
        _code_block(label="extract_data"),
        _code_block(label="read_first", prompt="Use {{ extract_data_output }}"),
        _code_block(label="extract_data"),
        _code_block(label="read_second", prompt="Use {{ extract_data_output }}"),
    )

    disabled = pdf_import_service._sanitize_workflow_json(deepcopy(raw))
    enabled = pdf_import_service._sanitize_workflow_json(deepcopy(raw), doc_editor_enabled=True)

    assert enabled == disabled
    first, first_consumer, second, second_consumer = enabled["workflow_definition"]["blocks"]
    assert first["label"] == "extract_data"
    assert second["label"] == "extract_data_2"
    assert first_consumer["prompt"] == second_consumer["prompt"] == "Use {{ extract_data_output }}"


def test_the_import_notes_switch_drops_notes_but_keeps_the_nested_clean_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_import_service_module.settings, "WORKFLOW_PDF_IMPORT_NOTES_ENABLED", False)
    child = {"label": "wait a bit", "block_type": "wait", "continue_on_failure": False, "wait_sec": 1}
    loop = {
        "label": "for_each_item",
        "block_type": "for_loop",
        "continue_on_failure": False,
        "loop_over_parameter_key": "items",
        "notes": "Rows may repeat.",
        "loop_blocks": [child],
    }

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(loop), doc_editor_enabled=True)

    block = sanitized["workflow_definition"]["blocks"][0]
    assert "notes" not in block
    assert block["loop_blocks"][0]["label"] == "wait_a_bit"
