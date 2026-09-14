"""Tests for MCP block tools (skyvern_block_schema, skyvern_block_validate)."""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skyvern.cli.mcp_tools.blocks import (
    CODE_BLOCK_RUNTIME_TOPIC,
    WORKFLOW_KNOWLEDGE_TOPIC_HEADERS,
    _parse_knowledge_topics,
    skyvern_block_schema,
    skyvern_block_validate,
    skyvern_workflow_knowledge,
)
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.models.block import CodeBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


@pytest.mark.asyncio
async def test_workflow_knowledge_lists_available_topics_without_returning_the_document() -> None:
    result = await skyvern_workflow_knowledge()

    assert result["ok"] is True
    assert result["data"]["topics"] == [*WORKFLOW_KNOWLEDGE_TOPIC_HEADERS, CODE_BLOCK_RUNTIME_TOPIC]
    assert result["data"]["count"] == len(WORKFLOW_KNOWLEDGE_TOPIC_HEADERS) + 1
    assert "A Skyvern workflow is defined" not in json.dumps(result)


@pytest.mark.asyncio
async def test_workflow_knowledge_renders_the_code_block_runtime_names_from_the_executor() -> None:
    result = await skyvern_workflow_knowledge(topics=[CODE_BLOCK_RUNTIME_TOPIC])

    assert result["ok"] is True
    content = result["data"]["sections"][CODE_BLOCK_RUNTIME_TOPIC]["content"]
    safe_vars = CodeBlock.build_safe_vars()
    for name in ("zip", "ValueError", "sorted"):
        assert name in safe_vars["__builtins__"]
        assert name in content
    assert "open" not in safe_vars["__builtins__"]
    assert "- datetime: UTC, date, datetime, timedelta, timezone" in content
    assert "- html: escape" in content
    assert "{{current_date}}" in content
    assert "solve_captcha" in content


@pytest.mark.asyncio
async def test_workflow_knowledge_returns_only_the_requested_authoritative_sections() -> None:
    result = await skyvern_workflow_knowledge(topics=["workflow_parameters", "error_handling_and_retries"])

    assert result["ok"] is True
    sections = result["data"]["sections"]
    assert list(sections) == [
        "workflow_parameters",
        "error_handling_and_retries",
    ]
    assert all(section["content"] for section in sections.values())
    assert "complete_workflow_example" not in sections


@pytest.mark.asyncio
async def test_workflow_knowledge_rejects_unknown_topics_with_the_catalog() -> None:
    result = await skyvern_workflow_knowledge(topics=["does_not_exist"])

    assert result["ok"] is False
    assert "does_not_exist" in result["error"]["message"]
    assert "workflow_parameters" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_workflow_knowledge_rejects_empty_topic_selection() -> None:
    result = await skyvern_workflow_knowledge(topics=[])

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_workflow_knowledge_reports_a_missing_document_as_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from skyvern.cli.mcp_tools import blocks

    monkeypatch.setattr(blocks, "_KB_PATH", tmp_path / "missing.txt")
    monkeypatch.setattr(blocks, "_knowledge_topic_cache", None)

    result = await blocks.skyvern_workflow_knowledge()

    assert result["ok"] is False
    assert result["error"]["code"] == "SDK_ERROR"


@pytest.mark.asyncio
async def test_workflow_knowledge_rejects_an_incomplete_topic_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from skyvern.cli.mcp_tools import blocks

    incomplete = tmp_path / "knowledge.txt"
    incomplete.write_text("** WORKFLOW PARAMETERS **\nOnly one section")
    monkeypatch.setattr(blocks, "_KB_PATH", incomplete)
    monkeypatch.setattr(blocks, "_knowledge_topic_cache", None)

    result = await blocks.skyvern_workflow_knowledge()

    assert result["ok"] is False
    assert result["error"]["code"] == "SDK_ERROR"


@pytest.mark.asyncio
async def test_block_schema_task_redirects_to_navigation() -> None:
    """Requesting schema for 'task' should return navigation info with a deprecation warning."""
    result = await skyvern_block_schema(block_type="task")

    assert result["ok"] is True
    assert result["data"]["block_type"] == "navigation"
    assert "navigation_goal" in result["data"]["schema"].get("properties", {})
    assert len(result["warnings"]) > 0
    assert any("deprecated" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_block_schema_unknown_type_returns_error() -> None:
    """Requesting schema for a nonexistent type should return an error with available types."""
    result = await skyvern_block_schema(block_type="invalid_xyz")

    assert result["ok"] is False
    assert result["error"] is not None
    assert "invalid_xyz" in result["error"]["message"]
    assert "navigation" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_block_validate_task_type_warns_deprecated() -> None:
    """Validating a 'task' block should succeed with a deprecation warning."""
    block = {
        "block_type": "task",
        "label": "test",
        "url": "https://example.com",
        "navigation_goal": "do something",
    }
    result = await skyvern_block_validate(block_json=json.dumps(block))

    assert result["ok"] is True
    assert result["data"]["valid"] is True
    assert len(result["warnings"]) > 0
    assert any("deprecated" in w.lower() for w in result["warnings"])


@pytest.mark.asyncio
async def test_block_validate_code_without_prompt_warns_without_mutating_response() -> None:
    block = {
        "block_type": "code",
        "label": "transform",
        "code": "return 1",
    }
    result = await skyvern_block_validate(block_json=json.dumps(block))

    assert result["ok"] is True
    assert result["data"] == {
        "valid": True,
        "block_type": "code",
        "label": "transform",
        "field_count": 2,
    }
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert "prompt" in warning
    assert "Workflow create" in warning
    assert "new label" in warning
    assert "not migrated" in warning


@pytest.mark.asyncio
async def test_block_validate_code_with_explicit_null_prompt_does_not_warn() -> None:
    block = {
        "block_type": "code",
        "label": "transform",
        "code": "return 1",
        "prompt": None,
    }
    result = await skyvern_block_validate(block_json=json.dumps(block))

    assert result["ok"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["field_count"] == 3
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_block_schema_no_type_lists_all() -> None:
    """Calling without a block_type should list all available types."""
    result = await skyvern_block_schema(block_type=None)

    assert result["ok"] is True
    block_types = result["data"]["block_types"]
    assert "navigation" in block_types
    assert "extraction" in block_types
    assert "pdf_fill" in block_types
    assert "task" not in block_types
    assert result["data"]["count"] > 0


@pytest.mark.asyncio
async def test_block_validate_pdf_fill() -> None:
    block = {
        "block_type": "pdf_fill",
        "label": "fill_pdf",
        "file_url": "{{ source_pdf }}",
        "prompt": "Fill the PDF using the payload.",
        "payload": {"name": "{{ applicant.name }}"},
        "parameter_keys": ["source_pdf", "applicant"],
    }
    result = await skyvern_block_validate(block_json=json.dumps(block))

    assert result["ok"] is True
    assert result["data"]["valid"] is True


def test_block_schema_takes_block_type_only_not_a_definition() -> None:
    """block_schema accepts only a block_type string; a full block definition belongs in block_validate.

    Guards the routing contract (SKY-12140/12141): callers that send a `definition`/`format` payload
    to block_schema are misrouted. The fix is the tool description, NOT adding those params here — so
    the function must keep rejecting them at the Python boundary.
    """
    params = inspect.signature(skyvern_block_schema).parameters
    assert set(params) == {"block_type"}

    with pytest.raises(TypeError):
        skyvern_block_schema(definition="{}", format="json")  # type: ignore[call-arg]


def test_block_schema_docstring_routes_full_definitions_to_block_validate() -> None:
    doc = skyvern_block_schema.__doc__ or ""
    assert "block_type" in doc
    assert "skyvern_block_validate" in doc


def test_block_validate_docstring_cross_refs_block_schema() -> None:
    doc = skyvern_block_validate.__doc__ or ""
    assert "skyvern_block_schema" in doc


@pytest.mark.asyncio
async def test_block_validate_data_export() -> None:
    result = await skyvern_block_validate(
        block_json=json.dumps(
            {
                "block_type": "data_export",
                "label": "export_records",
                "data": "{{ extract_output.extracted_information }}",
                "data_schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                    },
                },
            }
        )
    )

    assert result["ok"] is True
    assert result["data"]["block_type"] == "data_export"


_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}")

_BROWSER_TASK_OUTPUT = {"status": "completed", "extracted_information": {"abstract": "a study abstract"}}
_NON_TASK_OUTPUT = {"summary": "a short summary"}
# A direct (non-website) download records file fields with no extracted_information, so no .output alias.
_DIRECT_FILE_DOWNLOAD_OUTPUT = {
    "file_name": "report.pdf",
    "downloaded_files": [{"url": "https://files.example.test/report.pdf", "filename": "report.pdf"}],
    "downloaded_file_urls": ["https://files.example.test/report.pdf"],
}
_TEMPLATING_FAMILY_OUTPUTS = {
    "label": _BROWSER_TASK_OUTPUT,
    "extract_items": _BROWSER_TASK_OUTPUT,
    "get_data": _BROWSER_TASK_OUTPUT,
    "summarize_notes": _NON_TASK_OUTPUT,
    "fetch_report": _DIRECT_FILE_DOWNLOAD_OUTPUT,
}


def _templating_context() -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_kb",
        workflow_permanent_id="wpid_kb",
        workflow_run_id="wr_kb",
        aws_client=MagicMock(),
    )
    for label, value in _TEMPLATING_FAMILY_OUTPUTS.items():
        ctx.register_block_reference_variable(label, value)
    return ctx


def _refuted_expressions(line: str) -> set[str]:
    """The section refutes a shape either as "X raises ..." or as "Y (NOT X)"."""
    if "raises" in line:
        return set(_JINJA_EXPRESSION.findall(line.split("raises", 1)[0]))
    if "NOT" in line:
        return set(_JINJA_EXPRESSION.findall(line.split("NOT", 1)[1]))
    return set()


def _documented_block_output_expressions() -> tuple[set[str], set[str]]:
    section = _parse_knowledge_topics()["parameter_templating"]["content"]
    taught: set[str] = set()
    refuted: set[str] = set()
    for line in section.splitlines():
        line_refuted = _refuted_expressions(line)
        for expression in _JINJA_EXPRESSION.findall(line):
            if "<" in expression:
                continue
            root = expression.strip("{} ").split(".")[0].split("|")[0].strip()
            if root not in _TEMPLATING_FAMILY_OUTPUTS:
                continue
            (refuted if expression in line_refuted else taught).add(expression)
    return taught - refuted, refuted


def _kb_block() -> TaskBlock:
    return TaskBlock(
        label="downstream",
        title="t",
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="downstream_output",
            output_parameter_id="op_kb",
            workflow_id="w_kb",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        ),
    )


def test_parameter_templating_block_output_examples_match_the_real_registrar() -> None:
    taught, refuted = _documented_block_output_expressions()
    taught_roots = {expression.strip("{} ").split(".")[0].split("|")[0].strip() for expression in taught}
    assert taught_roots == set(_TEMPLATING_FAMILY_OUTPUTS)
    assert len(taught) >= len(_TEMPLATING_FAMILY_OUTPUTS) + 2
    assert refuted

    ctx = _templating_context()
    block = _kb_block()

    for expression in sorted(taught):
        assert block.format_block_parameter_template_from_workflow_run_context(expression, ctx), expression

    for expression in sorted(refuted):
        with pytest.raises((FailedToFormatJinjaStyleParameter, MissingJinjaVariables)):
            block.format_block_parameter_template_from_workflow_run_context(expression, ctx)


def test_parameter_templating_block_output_examples_resolve_to_the_documented_values() -> None:
    ctx = _templating_context()
    block = _kb_block()

    def render(expression: str) -> str:
        return block.format_block_parameter_template_from_workflow_run_context(expression, ctx)

    assert render("{{ label.output }}") == render("{{ label.extracted_information }}")
    assert "a study abstract" in render("{{ label.output }}")
    assert "extracted_information" in render("{{ label }}")
    assert render("{{ summarize_notes.summary }}") == "a short summary"
