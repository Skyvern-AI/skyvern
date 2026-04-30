from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.mcp_tools import mcp


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


def _fake_http_response(payload: dict[str, object], status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload,
        text=json.dumps(payload),
    )


def _fake_workflow_dict(**overrides: object) -> dict[str, object]:
    """Plain dict shape returned by `v1/workflows` — matches _fake_workflow_response()."""
    data: dict[str, object] = {
        "workflow_permanent_id": "wpid_test",
        "workflow_id": "wf_test",
        "title": "Example Workflow",
        "version": 1,
        "status": "published",
        "description": None,
        "is_saved_task": False,
        "folder_id": None,
        "created_at": "2026-04-23T10:00:00+00:00",
        "modified_at": "2026-04-23T10:00:00+00:00",
    }
    data.update(overrides)
    return data


def _patch_skyvern_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_payload: object,
    status_code: int = 200,
) -> AsyncMock:
    """Patch ``workflow_tools.get_skyvern`` to return a fake client whose httpx
    request returns the given payload. Returns the request AsyncMock so tests
    can assert on what was sent.
    """
    response = SimpleNamespace(
        status_code=status_code,
        json=lambda: response_payload,
        text=json.dumps(response_payload),
    )
    request_mock = AsyncMock(return_value=response)
    fake_skyvern = SimpleNamespace(
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request_mock)),
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_skyvern)
    return request_mock


def _google_sheets_definition(block_type: str) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": block_type,
        "label": f"{block_type}_step",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit",
        "sheet_name": "Sheet1",
        "range": "A1:D100",
        "credential_id": "{{ google_credential_id }}",
    }
    if block_type == "google_sheets_read":
        block["has_header_row"] = True
    elif block_type == "google_sheets_write":
        block["write_mode"] = "append"
        block["values"] = "{{ output_data | tojson }}"
    else:
        raise ValueError(f"Unsupported google sheets block type: {block_type}")

    return {
        "title": f"{block_type} workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": [block],
        },
    }


def _heavy_workflow_run_payload(*, include_expanded_outputs: bool = True) -> dict[str, object]:
    long_url = "https://artifacts.skyvern.example/" + ("x" * 1450)
    screenshot_urls = [f"{long_url}-{idx}" for idx in range(6)]
    task_artifact_ids = [f"art_task_{idx}" for idx in range(12)]
    workflow_artifact_ids = [f"art_workflow_{idx}" for idx in range(12)]
    outputs: dict[str, object] = {
        "collect_customer_data": {
            "task_screenshot_artifact_ids": task_artifact_ids,
            "workflow_screenshot_artifact_ids": workflow_artifact_ids,
            "extracted_information": [{"account_id": "acct_123", "status": "terminated"}],
            "status": "terminated",
        },
        "submit_case": [
            {
                "workflow_screenshot_artifact_ids": [f"art_followup_{idx}" for idx in range(6)],
                "task_screenshot_artifact_ids": [f"art_nested_{idx}" for idx in range(6)],
                "result": "retry_required",
            }
        ],
    }
    if include_expanded_outputs:
        outputs["collect_customer_data"] |= {
            "task_screenshots": screenshot_urls[:4],
            "workflow_screenshots": screenshot_urls[2:6],
        }
        outputs["submit_case"][0] |= {
            "task_screenshots": screenshot_urls[:3],
            "workflow_screenshots": screenshot_urls[1:5],
        }
        outputs["extracted_information"] = [{"duplicated_rollup": True}]

    return {
        "workflow_id": "wpid_heavy",
        "workflow_run_id": "wr_heavy",
        "status": "terminated",
        "failure_reason": "Execution terminated after repeated navigation failures",
        "workflow_title": "Heavy workflow",
        "recording_url": long_url,
        "screenshot_urls": screenshot_urls,
        "downloaded_files": [
            {
                "url": f"{long_url}-download",
                "filename": "case-export.csv",
            }
        ],
        "outputs": outputs,
        "run_with": "code",
    }


@pytest.mark.parametrize("block_type", ["google_sheets_read", "google_sheets_write"])
@pytest.mark.asyncio
async def test_workflow_create_sends_google_sheets_json_definition_as_raw_dict(
    monkeypatch: pytest.MonkeyPatch, block_type: str
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())
    definition = _google_sheets_definition(block_type)

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    assert result["ok"] is True, result
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert type(sent_definition) is dict
    assert not hasattr(sent_definition, "model_dump")
    sent_block = sent_definition["workflow_definition"]["blocks"][0]
    assert sent_block["block_type"] == block_type
    assert sent_block["spreadsheet_url"] == "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"


@pytest.mark.parametrize("block_type", ["google_sheets_read", "google_sheets_write"])
@pytest.mark.asyncio
async def test_workflow_update_sends_google_sheets_json_definition_as_raw_dict(
    monkeypatch: pytest.MonkeyPatch, block_type: str
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [],
                "blocks": [],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)
    definition = _google_sheets_definition(block_type)

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True, result
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert type(sent_definition) is dict
    assert not hasattr(sent_definition, "model_dump")
    sent_block = sent_definition["workflow_definition"]["blocks"][0]
    assert sent_block["block_type"] == block_type
    assert sent_block["spreadsheet_url"] == "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"


@pytest.mark.asyncio
async def test_workflow_create_normalizes_invalid_text_prompt_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    sent_block = sent_definition["workflow_definition"]["blocks"][0]

    assert result["ok"] is True
    assert sent_block.get("llm_key") is None
    assert sent_block.get("model") is None
    assert "ANTHROPIC_CLAUDE_3_5_SONNET" not in json.dumps(sent_definition)


@pytest.mark.asyncio
async def test_workflow_create_preserves_explicit_internal_text_prompt_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    sent_block = sent_definition["workflow_definition"]["blocks"][0]

    assert result["ok"] is True
    assert sent_block.get("model") is None
    assert sent_block.get("llm_key") == "SPECIAL_INTERNAL_KEY"


# ---------------------------------------------------------------------------
# Prove the exact Slack scenario: MCP agent hallucinates various model strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hallucinated_key",
    [
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
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    sent_block = sent_def["workflow_definition"]["blocks"][0]

    assert result["ok"] is True
    assert sent_block.get("llm_key") is None, f"hallucinated key {hallucinated_key!r} was NOT stripped"
    assert sent_block.get("model") is None, "should default to Skyvern Optimized (null model)"


@pytest.mark.asyncio
async def test_workflow_create_preserves_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fields not in the internal schema should survive normalization.

    Our normalization deep-merges the original raw dict with the backend schema
    output so fields not yet mirrored in that schema are preserved at any
    nesting depth.
    """
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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

    sent = request_mock.await_args.kwargs["json"]["json_definition"]
    # Top-level unknown field preserved
    assert sent.get("some_future_sdk_field") == "should_survive"
    # Nested unknown field inside workflow_definition also preserved via deep merge
    wd = sent["workflow_definition"]
    assert wd.get("some_nested_future_field") == "also_survives"


@pytest.mark.asyncio
async def test_workflow_create_defaults_proxy_location_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    definition = {
        "title": "Default proxy workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "navigation",
                    "label": "visit",
                    "url": "https://example.com",
                    "title": "Visit",
                    "navigation_goal": "Open the page",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert result["ok"] is True
    assert sent_definition.get("proxy_location") == "RESIDENTIAL"


@pytest.mark.asyncio
async def test_workflow_create_preserves_block_level_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown fields inside individual block dicts survive normalization via deep merge."""
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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

    sent = request_mock.await_args.kwargs["json"]["json_definition"]
    sent_block = sent["workflow_definition"]["blocks"][0]
    assert sent_block.get("some_future_block_field") == 42


@pytest.mark.asyncio
async def test_workflow_update_preserves_existing_proxy_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {"proxy_location": "RESIDENTIAL_AU"}

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "navigation",
                    "label": "visit",
                    "url": "https://example.com",
                    "title": "Visit",
                    "navigation_goal": "Open the page",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert result["ok"] is True
    assert sent_definition.get("proxy_location") == "RESIDENTIAL_AU"


@pytest.mark.asyncio
async def test_workflow_update_defaults_proxy_when_existing_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {"proxy_location": None}

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "navigation",
                    "label": "visit",
                    "url": "https://example.com",
                    "title": "Visit",
                    "navigation_goal": "Open the page",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert result["ok"] is True
    assert sent_definition.get("proxy_location") == "RESIDENTIAL"


@pytest.mark.asyncio
async def test_workflow_create_falls_back_on_schema_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the internal schema rejects the payload, normalization is skipped and the raw dict is forwarded."""
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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
    sent = request_mock.await_args.kwargs["json"]["json_definition"]
    # Normalization was skipped, so the hallucinated key passes through to the SDK
    sent_block = sent["workflow_definition"]["blocks"][0]
    assert sent_block.get("llm_key") == "HALLUCINATED_KEY"


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
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

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
    sent_block = request_mock.await_args.kwargs["json"]["json_definition"]["workflow_definition"]["blocks"][0]

    assert result["ok"] is True
    assert sent_block.get("llm_key") is None
    assert sent_block.get("model") is None


@pytest.mark.asyncio
async def test_workflow_status_uses_workflow_run_route_for_wr_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _heavy_workflow_run_payload(include_expanded_outputs=False)
    request = AsyncMock(return_value=_fake_http_response(payload))
    fake_client = SimpleNamespace(
        get_run=AsyncMock(),
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request)),
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    result = await workflow_tools.skyvern_workflow_status(run_id="wr_heavy")

    assert result["ok"] is True
    request.assert_awaited_once_with(
        "api/v1/workflows/runs/wr_heavy",
        method="GET",
        params={"include_output_details": False},
    )
    fake_client.get_run.assert_not_awaited()
    data = result["data"]
    assert data["run_id"] == "wr_heavy"
    assert data["run_type"] == "workflow_run"
    assert "recording_url" not in data
    assert "output" not in data
    assert data["artifact_summary"]["recording_available"] is True
    assert data["artifact_summary"]["artifact_id_count"] == 36
    assert data["output_summary"]["nested_screenshot_count"] == 0
    assert data["output_summary"]["has_extracted_information"] is True


@pytest.mark.asyncio
async def test_workflow_status_full_preserves_expanded_workflow_details(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _heavy_workflow_run_payload(include_expanded_outputs=True)
    request = AsyncMock(return_value=_fake_http_response(payload))
    fake_client = SimpleNamespace(
        get_run=AsyncMock(),
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request)),
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    result = await workflow_tools.skyvern_workflow_status(run_id="wr_heavy", verbosity="full")

    assert result["ok"] is True
    request.assert_awaited_once_with(
        "api/v1/workflows/runs/wr_heavy",
        method="GET",
        params={"include_output_details": True},
    )
    data = result["data"]
    assert data["run_id"] == "wr_heavy"
    assert data["recording_url"] == payload["recording_url"]
    assert data["output"] == payload["outputs"]
    assert data["workflow_title"] == "Heavy workflow"


@pytest.mark.asyncio
async def test_workflow_status_task_runs_still_use_get_run(monkeypatch: pytest.MonkeyPatch) -> None:
    task_run = SimpleNamespace(
        run_id="tsk_v2_123",
        status="completed",
        run_type="task_v2",
        output={"answer": "42"},
        failure_reason=None,
        step_count=4,
        recording_url=None,
        app_url=None,
        browser_session_id=None,
        run_with=None,
        created_at=None,
        modified_at=None,
        started_at=None,
        finished_at=None,
        queued_at=None,
    )
    fake_client = SimpleNamespace(get_run=AsyncMock(return_value=task_run))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    result = await workflow_tools.skyvern_workflow_status(run_id="tsk_v2_123")

    fake_client.get_run.assert_awaited_once_with("tsk_v2_123")
    data = result["data"]
    assert data["run_id"] == "tsk_v2_123"
    assert data["step_count"] == 4
    assert data["output_summary"]["scalar_preview"] == {"answer": "42"}


@pytest.mark.asyncio
async def test_workflow_status_summary_via_mcp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _heavy_workflow_run_payload(include_expanded_outputs=False)
    request = AsyncMock(return_value=_fake_http_response(payload))
    fake_client = SimpleNamespace(
        get_run=AsyncMock(),
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request)),
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    async with Client(mcp) as client:
        result = await client.call_tool("skyvern_workflow_status", {"run_id": "wr_heavy"})

    assert result.is_error is False
    assert isinstance(result.data, dict)
    assert result.data["ok"] is True
    data = result.data["data"]
    assert data["run_id"] == "wr_heavy"
    assert data["artifact_summary"]["artifact_id_count"] == 36
    assert "recording_url" not in data
    assert "output" not in data


@pytest.mark.asyncio
async def test_workflow_status_full_via_mcp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _heavy_workflow_run_payload(include_expanded_outputs=True)
    request = AsyncMock(return_value=_fake_http_response(payload))
    fake_client = SimpleNamespace(
        get_run=AsyncMock(),
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request)),
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    async with Client(mcp) as client:
        result = await client.call_tool("skyvern_workflow_status", {"run_id": "wr_heavy", "verbosity": "full"})

    assert result.is_error is False
    assert isinstance(result.data, dict)
    assert result.data["ok"] is True
    data = result.data["data"]
    assert data["run_id"] == "wr_heavy"
    assert data["recording_url"] == payload["recording_url"]
    assert data["output"] == payload["outputs"]


# ---------------------------------------------------------------------------
# Credential parameter preservation during workflow update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_update_preserves_credential_parameters_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update definition omits credential parameters, they should be
    injected from the existing workflow so the login block keeps working."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                        "credential_parameter_id": "cp_xyz",
                        "workflow_id": "wf_test",
                        "description": None,
                    },
                    {
                        "parameter_type": "workflow",
                        "key": "url_input",
                        "workflow_parameter_type": "string",
                        "default_value": "https://example.com",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # The update definition includes the workflow parameter but omits the credential parameter
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "url_input",
                    "workflow_parameter_type": "string",
                    "default_value": "https://new-url.com",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": ["credentials"],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    # Verify credential parameter was injected
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["credential_id"] == "cred_abc123"
    assert cred_params[0]["key"] == "credentials"


@pytest.mark.asyncio
async def test_workflow_update_injects_credential_key_into_block_parameter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update definition omits the credential parameter key from a login
    block's parameter_keys, the key should be injected from the existing workflow."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                    {
                        "parameter_type": "workflow",
                        "key": "url_input",
                        "workflow_parameter_type": "string",
                        "default_value": "https://example.com",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                        "navigation_goal": "Login",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude regenerated the login block WITHOUT the credential key in parameter_keys
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "url_input",
                    "workflow_parameter_type": "string",
                    "default_value": "https://new-url.com",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": [],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    # Verify credential parameter was injected
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["credential_id"] == "cred_abc123"

    # Verify the login block now references the credential parameter key
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_injects_credential_key_when_parameter_keys_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update definition omits parameter_keys entirely from the block,
    credential keys should still be injected."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude regenerated the login block WITHOUT parameter_keys at all
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    # Verify credential parameter was injected
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1

    # Verify the login block now has the credential key
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_preserves_block_credential_when_param_already_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update already includes the credential parameter but the block omits
    the key from parameter_keys, the key should still be injected into the block."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude included the credential param but omitted it from the block's parameter_keys
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "credential",
                    "key": "credentials",
                    "credential_id": "cred_abc123",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": [],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys

    # Should not duplicate the credential parameter
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1


@pytest.mark.asyncio
async def test_workflow_update_does_not_duplicate_existing_credential_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update definition already includes the credential parameter,
    it should NOT be duplicated."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # The update definition already includes the credential parameter
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "credential",
                    "key": "credentials",
                    "credential_id": "cred_abc123",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": ["credentials"],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    # Should still be exactly 1, not duplicated
    assert len(cred_params) == 1


@pytest.mark.asyncio
async def test_workflow_update_credential_keys_injected_when_login_block_label_renamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Claude renames a login block label (e.g. 'login_block' -> 'login'),
    credential parameter_keys should still be injected via type-based matching."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                        "navigation_goal": "Login",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude renamed the login block from "login_block" to "login"
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login",
                    "parameter_keys": [],
                    "navigation_goal": "Log in to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    # Credential parameter must be injected
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["credential_id"] == "cred_abc123"

    # Login block must have credential key despite label mismatch
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_always_replaces_wrong_credential_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Claude includes a credential parameter with the wrong credential_id,
    the existing workflow's credential_id should always win."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_CORRECT",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude includes credential param but with a WRONG credential_id
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "credential",
                    "key": "credentials",
                    "credential_id": "cred_WRONG_STALE",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": ["credentials"],
                    "navigation_goal": "Login",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    # The existing (correct) credential_id must win
    assert cred_params[0]["credential_id"] == "cred_CORRECT"


@pytest.mark.asyncio
async def test_workflow_update_correct_credential_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Claude includes the credential parameter correctly, the always-replace
    strategy should still work (idempotent, no regression)."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                    {
                        "parameter_type": "workflow",
                        "key": "url_input",
                        "workflow_parameter_type": "string",
                        "default_value": "https://example.com",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude includes credential param correctly AND the block references it
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "credential",
                    "key": "credentials",
                    "credential_id": "cred_abc123",
                },
                {
                    "parameter_type": "workflow",
                    "key": "url_input",
                    "workflow_parameter_type": "string",
                    "default_value": "https://new-url.com",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": ["credentials"],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["credential_id"] == "cred_abc123"

    # Non-credential params should be preserved from the update
    wf_params = [p for p in params if p.get("parameter_type") == "workflow"]
    assert len(wf_params) == 1
    assert wf_params[0]["default_value"] == "https://new-url.com"

    # Login block should have the credential key
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_multiple_login_blocks_all_get_credential_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the update has multiple login blocks, ALL of them should get credential
    parameter_keys injected (even if labels don't match existing blocks)."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude split the workflow into two login blocks with new labels
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "initial_login",
                    "navigation_goal": "Login to the site",
                },
                {
                    "block_type": "task",
                    "label": "do_work",
                    "navigation_goal": "Do some work",
                },
                {
                    "block_type": "login",
                    "label": "reauth_login",
                    "navigation_goal": "Re-authenticate",
                },
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    blocks = sent_def["workflow_definition"]["blocks"]

    # Both login blocks should have credential keys
    login_blocks = [b for b in blocks if b.get("block_type") == "login"]
    assert len(login_blocks) == 2
    for lb in login_blocks:
        pkeys = lb.get("parameter_keys", [])
        assert "credentials" in pkeys, f"Login block {lb.get('label', '?')} missing credential key"

    # Task block should NOT have credential keys (no label match, not a login block)
    task_block = next(b for b in blocks if b.get("block_type") == "task")
    task_pkeys = task_block.get("parameter_keys") or []
    assert "credentials" not in task_pkeys


@pytest.mark.asyncio
async def test_workflow_update_credential_keys_injected_into_login_block_nested_in_for_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a login block is nested inside a for_loop block, credential parameter_keys
    should still be injected via type-based matching."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "loop_block",
                        "loop_blocks": [
                            {
                                "block_type": "login",
                                "label": "login_block",
                                "parameter_keys": ["credentials"],
                                "navigation_goal": "Login",
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude renamed the nested login block inside the for_loop
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "for_loop",
                    "label": "loop_block",
                    "loop_blocks": [
                        {
                            "block_type": "login",
                            "label": "login",
                            "navigation_goal": "Log in to the site",
                        }
                    ],
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    # Credential parameter must be injected
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["credential_id"] == "cred_abc123"

    # The nested login block inside for_loop must have credential keys
    blocks = sent_def["workflow_definition"]["blocks"]
    loop_block = next(b for b in blocks if b.get("block_type") == "for_loop")
    nested_blocks = loop_block.get("loop_blocks", [])
    login_block = next(b for b in nested_blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_login_block_only_gets_credential_type_keys_not_aws_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a workflow has both a credential param and an aws_secret param,
    login blocks should only get the credential key, not the aws_secret key."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                    },
                    {
                        "parameter_type": "aws_secret",
                        "key": "api_secret",
                        "aws_key": "my-secret-key",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    },
                    {
                        "block_type": "task",
                        "label": "api_call",
                        "parameter_keys": ["api_secret"],
                        "navigation_goal": "Make API call",
                    },
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    # Claude renamed login block and dropped all parameter info
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login",
                    "navigation_goal": "Login",
                },
                {
                    "block_type": "task",
                    "label": "api_call",
                    "navigation_goal": "Make API call",
                },
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    # Both params should be preserved
    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    aws_params = [p for p in params if p.get("parameter_type") == "aws_secret"]
    assert len(cred_params) == 1
    assert len(aws_params) == 1

    blocks = sent_def["workflow_definition"]["blocks"]

    # Login block should ONLY get credential key, NOT aws_secret
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    login_pkeys = login_block.get("parameter_keys", [])
    assert "credentials" in login_pkeys
    assert "api_secret" not in login_pkeys

    # Task block should get aws_secret via label fallback (label unchanged)
    task_block = next(b for b in blocks if b.get("block_type") == "task")
    task_pkeys = task_block.get("parameter_keys", [])
    assert "api_secret" in task_pkeys


@pytest.mark.asyncio
async def test_workflow_update_strips_runtime_fields_from_credential_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the existing workflow returns credential params with runtime fields
    (credential_parameter_id, workflow_id, created_at, modified_at, deleted_at),
    those fields must be stripped before re-injecting into the update definition."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                        "description": "Login credentials",
                        # Runtime fields from GET API response
                        "credential_parameter_id": "cp_runtime_123",
                        "workflow_id": "wpid_runtime_456",
                        "created_at": "2025-01-01T00:00:00Z",
                        "modified_at": "2025-06-15T12:00:00Z",
                        "deleted_at": None,
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["credentials"],
                    },
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login",
                    "navigation_goal": "Login",
                },
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    params = sent_def["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1

    cred = cred_params[0]
    # Business fields should be preserved
    assert cred.get("key") == "credentials"
    assert cred.get("credential_id") == "cred_abc123"
    assert cred.get("description") == "Login credentials"

    # Runtime fields must NOT be present
    assert "credential_parameter_id" not in cred
    assert "workflow_id" not in cred
    assert "created_at" not in cred
    assert "modified_at" not in cred
    assert "deleted_at" not in cred


@pytest.mark.asyncio
async def test_workflow_update_preserves_workflow_credential_id_params_and_injects_login_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow parameters with `credential_id` type should be preserved like direct credential params."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "my_creds",
                        "workflow_parameter_type": "credential_id",
                        "default_value": "cred_abc123",
                        "workflow_parameter_id": "wp_runtime_123",
                        "workflow_id": "wpid_runtime_456",
                        "created_at": "2025-01-01T00:00:00Z",
                        "modified_at": "2025-06-15T12:00:00Z",
                        "deleted_at": None,
                    },
                    {
                        "parameter_type": "workflow",
                        "key": "url_input",
                        "workflow_parameter_type": "string",
                        "default_value": "https://example.com",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["my_creds"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "url_input",
                    "workflow_parameter_type": "string",
                    "default_value": "https://new-url.com",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login",
                    "parameter_keys": [],
                    "navigation_goal": "Login to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    params = sent_def["workflow_definition"]["parameters"]
    workflow_cred_params = [
        p
        for p in params
        if p.get("parameter_type") == "workflow" and str(p.get("workflow_parameter_type")) == "credential_id"
    ]
    assert len(workflow_cred_params) == 1

    workflow_cred = workflow_cred_params[0]
    assert workflow_cred.get("key") == "my_creds"
    assert workflow_cred.get("default_value") == "cred_abc123"

    assert "workflow_parameter_id" not in workflow_cred
    assert "workflow_id" not in workflow_cred
    assert "created_at" not in workflow_cred
    assert "modified_at" not in workflow_cred
    assert "deleted_at" not in workflow_cred

    workflow_params = [p for p in params if p.get("parameter_type") == "workflow"]
    url_param = next(p for p in workflow_params if p.get("key") == "url_input")
    assert url_param.get("default_value") == "https://new-url.com"

    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "my_creds" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_always_replaces_wrong_workflow_credential_id_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential-id workflow params should keep the existing default credential on MCP updates."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "my_creds",
                        "workflow_parameter_type": "credential_id",
                        "default_value": "cred_CORRECT",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_block",
                        "parameter_keys": ["my_creds"],
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "my_creds",
                    "workflow_parameter_type": "credential_id",
                    "default_value": "cred_WRONG_STALE",
                },
            ],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_block",
                    "parameter_keys": ["my_creds"],
                    "navigation_goal": "Login",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    params = sent_def["workflow_definition"]["parameters"]
    workflow_cred_params = [
        p
        for p in params
        if p.get("parameter_type") == "workflow" and str(p.get("workflow_parameter_type")) == "credential_id"
    ]
    assert len(workflow_cred_params) == 1
    assert workflow_cred_params[0].get("default_value") == "cred_CORRECT"


@pytest.mark.asyncio
async def test_workflow_update_injects_onepassword_key_into_login_block_parameter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login blocks should keep external credential keys like onepassword after MCP edits."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "onepassword",
                        "key": "site_login_cred",
                        "vault_id": "vault_123",
                        "item_id": "item_456",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "Login_cont",
                        "parameter_keys": ["site_login_cred"],
                        "navigation_goal": "Login",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "Login_cont_renamed",
                    "parameter_keys": [],
                    "navigation_goal": "Log in to the site",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    params = sent_def["workflow_definition"]["parameters"]
    onepassword_params = [p for p in params if p.get("parameter_type") == "onepassword"]
    assert len(onepassword_params) == 1
    assert onepassword_params[0].get("key") == "site_login_cred"
    assert onepassword_params[0].get("vault_id") == "vault_123"
    assert onepassword_params[0].get("item_id") == "item_456"

    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "site_login_cred" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_injects_bitwarden_login_key_into_login_block_parameter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login blocks should keep bitwarden_login_credential keys after MCP edits."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "bitwarden_login_credential",
                        "key": "portal_login",
                        "bitwarden_login_credential_parameter_id": "blc_fake",
                        "workflow_id": "w_fake",
                        "bitwarden_client_id_aws_secret_key": "bw_client",
                        "bitwarden_client_secret_aws_secret_key": "bw_secret",
                        "bitwarden_master_password_aws_secret_key": "bw_master",
                        "bitwarden_collection_id": "col_123",
                        "url_filter": "https://portal.example.com",
                        "created_at": "2026-01-01T00:00:00Z",
                        "modified_at": "2026-01-01T00:00:00Z",
                        "deleted_at": None,
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "Portal_Login",
                        "parameter_keys": ["portal_login"],
                        "navigation_goal": "Log in to the portal",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "Portal_Login_Renamed",
                    "parameter_keys": [],
                    "navigation_goal": "Log in to the portal - updated",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    params = sent_def["workflow_definition"]["parameters"]
    bw_params = [p for p in params if p.get("parameter_type") == "bitwarden_login_credential"]
    assert len(bw_params) == 1
    assert bw_params[0].get("key") == "portal_login"

    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "portal_login" in pkeys


@pytest.mark.asyncio
async def test_workflow_update_injects_azure_vault_key_into_login_block_parameter_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login blocks should keep azure_vault_credential keys after MCP edits."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "azure_vault_credential",
                        "key": "azure_creds",
                        "azure_vault_credential_parameter_id": "avc_fake",
                        "workflow_id": "w_fake",
                        "vault_name": "my-vault",
                        "username_key": "portal-user",
                        "password_key": "portal-pass",
                        "totp_secret_key": None,
                        "created_at": "2026-01-01T00:00:00Z",
                        "modified_at": "2026-01-01T00:00:00Z",
                        "deleted_at": None,
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "Azure_Login",
                        "parameter_keys": ["azure_creds"],
                        "navigation_goal": "Log in with Azure credentials",
                    }
                ],
            },
        }

    monkeypatch.setattr(workflow_tools, "_get_workflow_by_id", fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "Azure_Login_Renamed",
                    "parameter_keys": [],
                    "navigation_goal": "Log in - updated",
                }
            ],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True

    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]

    params = sent_def["workflow_definition"]["parameters"]
    az_params = [p for p in params if p.get("parameter_type") == "azure_vault_credential"]
    assert len(az_params) == 1
    assert az_params[0].get("key") == "azure_creds"

    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("block_type") == "login")
    pkeys = login_block.get("parameter_keys", [])
    assert "azure_creds" in pkeys
