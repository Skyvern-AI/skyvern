from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import skyvern.cli.mcp_tools.workflow as workflow_tools


def _fake_workflow_response() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
        title="Example Workflow",
        version=1,
        status="published",
        description=None,
        is_saved_task=False,
        folder_id=None,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_workflow_create_normalizes_invalid_text_prompt_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Normalize invalid llm_key",
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize the result",
                    "llm_key": "ANTHROPIC_CLAUDE_3_5_SONNET",
                }
            ],
            "parameters": [],
        },
    }

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    sent_definition = fake_client.create_workflow.await_args.kwargs["json_definition"]
    sent_block = sent_definition.workflow_definition.blocks[0]

    assert result["ok"] is True
    assert sent_block.llm_key is None
    assert sent_block.model is None
    assert "ANTHROPIC_CLAUDE_3_5_SONNET" not in json.dumps(sent_definition.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_workflow_create_preserves_explicit_internal_text_prompt_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Preserve explicit internal llm_key",
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize the result",
                    "llm_key": "SPECIAL_INTERNAL_KEY",
                }
            ],
            "parameters": [],
        },
    }

    with patch(
        "skyvern.schemas.workflows.LLMConfigRegistry.get_model_names",
        return_value=["SPECIAL_INTERNAL_KEY"],
    ):
        result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    sent_definition = fake_client.create_workflow.await_args.kwargs["json_definition"]
    sent_block = sent_definition.workflow_definition.blocks[0]

    assert result["ok"] is True
    assert sent_block.model is None
    assert sent_block.llm_key == "SPECIAL_INTERNAL_KEY"


# ---------------------------------------------------------------------------
# Prove the exact Slack scenario: MCP agent hallucinates various model strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hallucinated_key",
    [
        "ANTHROPIC_CLAUDE_3_5_SONNET",  # exact key from the Slack thread
        "ANTHROPIC_CLAUDE3.5_SONNET",  # the "correct" key Pedro mentioned — still not public
        "ANTHROPIC_CLAUDE_3_5_HAIKU",
        "OPENAI_GPT4_TURBO",
        "VERTEX_GEMINI_2_FLASH",
        "claude-3-opus-20240229",
        "gpt-4o-mini",
        "gemini-pro",
    ],
)
@pytest.mark.asyncio
async def test_mcp_strips_all_common_hallucinated_llm_keys(
    monkeypatch: pytest.MonkeyPatch, hallucinated_key: str
) -> None:
    """MCP workflow creation must strip ANY hallucinated llm_key and default to Skyvern Optimized (null)."""
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Agent-generated workflow",
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "limit_pages",
                    "prompt": "Extract only the first 10 results",
                    "llm_key": hallucinated_key,
                }
            ],
            "parameters": [],
        },
    }

    with patch(
        "skyvern.schemas.workflows.LLMConfigRegistry.get_model_names",
        return_value=[],  # simulate: none of these are registered
    ):
        result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="auto")

    sent_def = fake_client.create_workflow.await_args.kwargs["json_definition"]
    sent_block = sent_def.workflow_definition.blocks[0]

    assert result["ok"] is True
    assert sent_block.llm_key is None, f"hallucinated key {hallucinated_key!r} was NOT stripped"
    assert sent_block.model is None, "should default to Skyvern Optimized (null model)"


@pytest.mark.asyncio
async def test_workflow_create_preserves_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fields not in the internal schema should survive normalization.

    The Fern-generated WorkflowCreateYamlRequest uses extra='allow', so unknown
    fields are accepted. Our normalization deep-merges the original raw dict with
    the normalized output so that future SDK fields not yet mirrored in the
    internal schema are preserved at any nesting depth.
    """
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Unknown fields test",
        "some_future_sdk_field": "should_survive",
        "workflow_definition": {
            "parameters": [],
            "some_nested_future_field": "also_survives",
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")
    assert result["ok"] is True

    sent = fake_client.create_workflow.await_args.kwargs["json_definition"]
    # Top-level unknown field preserved
    assert sent.some_future_sdk_field == "should_survive"
    # Nested unknown field inside workflow_definition also preserved via deep merge
    wd = sent.workflow_definition
    wd_dict = wd.model_dump(mode="json") if hasattr(wd, "model_dump") else wd.__dict__
    assert wd_dict.get("some_nested_future_field") == "also_survives"


@pytest.mark.asyncio
async def test_workflow_create_preserves_block_level_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown fields inside individual block dicts survive normalization via deep merge."""
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Block-level unknown fields test",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize",
                    "some_future_block_field": 42,
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")
    assert result["ok"] is True

    sent = fake_client.create_workflow.await_args.kwargs["json_definition"]
    sent_block = sent.workflow_definition.blocks[0]
    block_dict = sent_block.model_dump(mode="json") if hasattr(sent_block, "model_dump") else sent_block.__dict__
    assert block_dict.get("some_future_block_field") == 42


@pytest.mark.asyncio
async def test_workflow_create_falls_back_on_schema_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the internal schema rejects the payload, normalization is skipped and the raw dict is forwarded."""
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Schema rejection test",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize",
                    "llm_key": "HALLUCINATED_KEY",
                }
            ],
        },
    }

    with patch(
        "skyvern.cli.mcp_tools.workflow.WorkflowCreateYAMLRequestSchema.model_validate",
        side_effect=Exception("schema rejected"),
    ):
        result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    assert result["ok"] is True
    sent = fake_client.create_workflow.await_args.kwargs["json_definition"]
    # Normalization was skipped, so the hallucinated key passes through to the SDK
    sent_block = sent.workflow_definition.blocks[0]
    block_dict = sent_block.model_dump(mode="json") if hasattr(sent_block, "model_dump") else sent_block.__dict__
    assert block_dict.get("llm_key") == "HALLUCINATED_KEY"


@pytest.mark.parametrize("format_name", ["json", "auto"])
def test_parse_definition_returns_invalid_input_when_schema_fallback_still_fails(format_name: str) -> None:
    """If both the internal schema and Fern fallback reject the JSON, return a structured INVALID_INPUT error."""

    json_def, yaml_def, err = workflow_tools._parse_definition(
        json.dumps({"title": "Missing workflow_definition"}),
        format_name,
    )

    assert json_def is None
    assert yaml_def is None
    assert err is not None
    assert err["code"] == workflow_tools.ErrorCode.INVALID_INPUT
    assert "Invalid JSON definition" in err["message"]


def test_deep_merge_preserves_block_unknown_fields_when_list_lengths_differ() -> None:
    """Overlapping block data should still merge when normalization changes list length."""

    raw = {
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize",
                    "some_future_block_field": 42,
                }
            ]
        }
    }
    normalized = {
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize",
                },
                {
                    "block_type": "text_prompt",
                    "label": "synthetic_followup",
                    "prompt": "Follow up",
                },
            ]
        }
    }

    merged = workflow_tools._deep_merge(raw, normalized)
    merged_blocks = merged["workflow_definition"]["blocks"]

    assert len(merged_blocks) == 2
    assert merged_blocks[0]["some_future_block_field"] == 42
    assert merged_blocks[1]["label"] == "synthetic_followup"


@pytest.mark.asyncio
async def test_mcp_text_prompt_without_llm_key_stays_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """When MCP correctly omits llm_key (Skyvern Optimized), it stays null through the whole pipeline."""
    fake_client = SimpleNamespace(create_workflow=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    definition = {
        "title": "Well-behaved MCP workflow",
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "text_prompt",
                    "label": "summarize",
                    "prompt": "Summarize the extracted data",
                }
            ],
            "parameters": [],
        },
    }

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")
    sent_block = fake_client.create_workflow.await_args.kwargs["json_definition"].workflow_definition.blocks[0]

    assert result["ok"] is True
    assert sent_block.llm_key is None
    assert sent_block.model is None
