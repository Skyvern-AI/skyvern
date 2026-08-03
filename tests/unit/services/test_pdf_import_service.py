"""Tests for the PDF/SOP import service's LLM-output sanitizer."""

from typing import Any

from skyvern.services.pdf_import_service import pdf_import_service


def _definition(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Imported SOP",
        "workflow_definition": {"parameters": [], "blocks": list(blocks)},
    }


def _code_block(**overrides: Any) -> dict[str, Any]:
    return {"label": "compute", "block_type": "code", "continue_on_failure": False, "code": "x = 1", **overrides}


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

    sanitized = pdf_import_service._sanitize_workflow_json(_definition(block))

    assert "prompt" not in sanitized["workflow_definition"]["blocks"][0]
