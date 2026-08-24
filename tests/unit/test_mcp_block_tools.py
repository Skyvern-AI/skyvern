"""Tests for MCP block tools (skyvern_block_schema, skyvern_block_validate)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from skyvern.cli.mcp_tools.blocks import (
    WORKFLOW_KNOWLEDGE_TOPIC_HEADERS,
    skyvern_block_schema,
    skyvern_block_validate,
    skyvern_workflow_knowledge,
)


@pytest.mark.asyncio
async def test_workflow_knowledge_lists_available_topics_without_returning_the_document() -> None:
    result = await skyvern_workflow_knowledge()

    assert result["ok"] is True
    assert result["data"]["topics"] == list(WORKFLOW_KNOWLEDGE_TOPIC_HEADERS)
    assert result["data"]["count"] == len(WORKFLOW_KNOWLEDGE_TOPIC_HEADERS)
    assert "A Skyvern workflow is defined" not in json.dumps(result)


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
