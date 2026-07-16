from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.core.result import set_concise_responses
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.response import MCP_MAX_RESPONSE_CHARS, size_capped
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowRequest
from tests.unit._mcp_test_helpers import patch_get_workflow_by_id as _patch_get_workflow_by_id
from tests.unit._mcp_test_helpers import patch_skyvern_client as _patch_skyvern_client


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


def _patch_workflow_get_definition(
    monkeypatch: pytest.MonkeyPatch,
    workflow_definition: dict[str, object],
) -> None:
    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(workflow_definition=workflow_definition)

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)


def _large_runtime_workflow_definition() -> dict[str, object]:
    runtime_parameters: list[dict[str, object]] = []
    for index in range(30):
        parameter: dict[str, object] = {
            "parameter_type": "workflow",
            "key": f"input_{index:02d}",
            "workflow_parameter_type": "json" if index < 10 else "string",
            "default_value": [f"item-{index}"] if index < 10 else f"value-{index}",
        }
        runtime_parameters.append(
            {
                **parameter,
                "workflow_parameter_id": f"wp_{index:02d}",
                "workflow_id": "wf_test",
            }
        )

    def code_block(index: int, label: str) -> dict[str, object]:
        parameter = runtime_parameters[index % len(runtime_parameters)]
        return {
            "block_type": "code",
            "label": label,
            "code": f"source = {parameter['key']!r}\nresult = {'x' * 1800!r}",
            "parameters": [parameter],
            "output_parameter": {
                "parameter_type": "output",
                "key": f"{label}_output",
                "output_parameter_id": f"op_{index:03d}",
                "workflow_id": "wf_test",
            },
        }

    blocks = [code_block(index, f"portal_step_{index:03d}") for index in range(68)]
    blocks.extend(
        [
            {
                "block_type": "navigation",
                "label": "open_portal",
                "url": "https://example.com",
                "navigation_goal": "Open the portal landing page.",
                "parameters": [runtime_parameters[28]],
            },
            {
                "block_type": "extraction",
                "label": "extract_portal_summary",
                "data_extraction_goal": "Extract a concise portal summary.",
                "data_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
                "parameters": [runtime_parameters[29]],
            },
        ]
    )
    for loop_index in range(10):
        blocks.append(
            {
                "block_type": "for_loop",
                "label": f"portal_loop_{loop_index:02d}",
                "loop_over": runtime_parameters[loop_index],
                "loop_blocks": [
                    code_block(
                        68 + loop_index * 2 + child_index,
                        f"portal_loop_{loop_index:02d}_step_{child_index}",
                    )
                    for child_index in range(2)
                ],
            }
        )

    return {"parameters": runtime_parameters, "blocks": blocks}


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
    _patch_skyvern_client(monkeypatch, fake_skyvern)
    return request_mock


def _known_drift_definition(block_type: str) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": block_type,
        "label": f"{block_type}_step",
    }
    if block_type == "google_sheets_read":
        block["spreadsheet_url"] = "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"
        block["sheet_name"] = "Sheet1"
        block["range"] = "A1:D100"
        block["credential_id"] = "{{ google_credential_id }}"
        block["has_header_row"] = True
    elif block_type == "google_sheets_write":
        block["spreadsheet_url"] = "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"
        block["sheet_name"] = "Sheet1"
        block["range"] = "A1:D100"
        block["credential_id"] = "{{ google_credential_id }}"
        block["write_mode"] = "append"
        block["values"] = "{{ output_data | tojson }}"
    elif block_type == "pdf_fill":
        block["file_url"] = "{{ source_pdf }}"
        block["prompt"] = "Fill the PDF using the payload."
        block["payload"] = {"name": "{{ applicant.name }}"}
    else:
        raise ValueError(f"Unsupported known drift block type: {block_type}")

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


@pytest.mark.parametrize("block_type", ["google_sheets_read", "google_sheets_write", "pdf_fill"])
@pytest.mark.asyncio
async def test_workflow_create_sends_known_drift_json_definition_as_raw_dict(
    monkeypatch: pytest.MonkeyPatch, block_type: str
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())
    definition = _known_drift_definition(block_type)

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    assert result["ok"] is True, result
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert type(sent_definition) is dict
    assert not hasattr(sent_definition, "model_dump")
    sent_block = sent_definition["workflow_definition"]["blocks"][0]
    assert sent_block["block_type"] == block_type
    if block_type.startswith("google_sheets"):
        assert sent_block["spreadsheet_url"] == "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"
    else:
        assert sent_block["file_url"] == "{{ source_pdf }}"


@pytest.mark.parametrize("block_type", ["google_sheets_read", "google_sheets_write", "pdf_fill"])
@pytest.mark.asyncio
async def test_workflow_update_sends_known_drift_json_definition_as_raw_dict(
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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)
    definition = _known_drift_definition(block_type)

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
    if block_type.startswith("google_sheets"):
        assert sent_block["spreadsheet_url"] == "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit"
    else:
        assert sent_block["file_url"] == "{{ source_pdf }}"


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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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
async def test_workflow_update_fetches_existing_workflow_once_for_all_injectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_workflow = _fake_workflow_dict(
        proxy_location="RESIDENTIAL_AU",
        run_sequentially=True,
        sequential_key="existing-sequential-key",
        workflow_definition={
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "report_url",
                    "workflow_parameter_type": "string",
                }
            ],
            "blocks": [
                {
                    "block_type": "code",
                    "label": "build_report",
                    "parameter_keys": ["report_url"],
                    "code": "result = report_url",
                }
            ],
        },
    )
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=existing_workflow)
    definition = {
        "title": "Updated workflow",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "report_url",
                    "workflow_parameter_type": "string",
                }
            ],
            "blocks": [
                {
                    "block_type": "code",
                    "label": "build_report",
                    "code": "result = report_url",
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
    methods = [call.kwargs["method"] for call in request_mock.await_args_list]
    assert methods.count("GET") == 1
    assert methods.count("POST") == 1
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert sent_definition["proxy_location"] == "RESIDENTIAL_AU"
    assert sent_definition["run_sequentially"] is True
    assert sent_definition["sequential_key"] == "existing-sequential-key"
    assert sent_definition["workflow_definition"]["blocks"][0]["parameter_keys"] == ["report_url"]


@pytest.mark.asyncio
async def test_workflow_update_does_not_fetch_existing_workflow_for_unparseable_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition="{",
        format="json",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == workflow_tools.ErrorCode.INVALID_INPUT
    assert request_mock.await_count == 0


@pytest.mark.asyncio
async def test_workflow_update_preserves_not_found_error_from_lazy_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(
        monkeypatch,
        response_payload={"detail": "Workflow not found"},
        status_code=404,
    )
    definition = {
        "title": "Updated workflow",
        "workflow_definition": {"parameters": [], "blocks": []},
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == workflow_tools.ErrorCode.WORKFLOW_NOT_FOUND
    assert request_mock.await_count == 1
    assert request_mock.await_args.kwargs["method"] == "GET"


@pytest.mark.asyncio
async def test_workflow_update_preserves_sequential_settings_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {
            "proxy_location": "RESIDENTIAL",
            "run_sequentially": True,
            "sequential_key": "existing-sequential-key",
            "workflow_definition": {
                "parameters": [],
                "blocks": [],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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
    assert sent_definition.get("run_sequentially") is True
    assert sent_definition.get("sequential_key") == "existing-sequential-key"


@pytest.mark.asyncio
async def test_workflow_update_preserves_all_update_safe_settings_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Required fields are caller-owned; proxy has its own injector; folder is retained by the service;
    # enable_self_healing/code_version already inherit when normalized to None.
    excluded_fields = {
        "title",
        "workflow_definition",
        "proxy_location",
        "folder_id",
        "enable_self_healing",
        "code_version",
    }
    assert set(workflow_tools._WORKFLOW_UPDATE_PRESERVED_TOP_LEVEL_FIELDS) == (
        set(WorkflowCreateYAMLRequest.model_fields) - excluded_fields
    )

    preserved_values: dict[str, object] = {
        "description": "Existing description",
        "webhook_callback_url": "https://example.com/webhook",
        "totp_verification_url": "https://example.com/totp",
        "totp_identifier": "existing@example.com",
        "persist_browser_session": True,
        "pin_saved_session_ip": True,
        "browser_profile_id": "bp_existing",
        "browser_profile_key": "{{ account_id }}",
        "model": {"name": "existing-model"},
        "is_saved_task": True,
        "max_screenshot_scrolls": 7,
        "max_elapsed_time_minutes": 10,
        "extra_http_headers": {"X-Existing": "value"},
        "cdp_connect_headers": {"X-CDP-Existing": "***"},
        "status": "draft",
        "run_with": "code",
        "ai_fallback": False,
        "cache_key": None,
        "adaptive_caching": True,
        "generate_script_on_terminal": True,
        "run_sequentially": True,
        "sequential_key": "existing-sequential-key",
    }
    existing_workflow = _fake_workflow_dict(
        proxy_location="RESIDENTIAL",
        workflow_definition={"parameters": [], "blocks": []},
        **preserved_values,
    )
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=existing_workflow)

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return existing_workflow

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(
            {
                "title": "Updated workflow",
                "workflow_definition": {"parameters": [], "blocks": []},
            }
        ),
        format="json",
    )

    assert result["ok"] is True
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert {field: sent_definition[field] for field in preserved_values} == preserved_values


@pytest.mark.asyncio
async def test_workflow_update_respects_explicit_run_sequentially_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {
            "proxy_location": "RESIDENTIAL",
            "run_sequentially": True,
            "sequential_key": "existing-sequential-key",
            "workflow_definition": {
                "parameters": [],
                "blocks": [],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "run_sequentially": False,
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
    assert sent_definition.get("run_sequentially") is False
    assert sent_definition.get("sequential_key") == "existing-sequential-key"


@pytest.mark.asyncio
async def test_workflow_update_preserves_overlap_and_credentials_via_mcp_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise workflow update through the registered FastMCP tool boundary."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        assert workflow_id == "wpid_test"
        assert version is None
        return {
            "proxy_location": "RESIDENTIAL",
            "run_sequentially": True,
            "sequential_key": "existing-sequential-key",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_abc123",
                        "credential_parameter_id": "cp_xyz",
                        "workflow_id": "wf_test",
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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
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

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_workflow_update",
            {
                "workflow_id": "wpid_test",
                "definition": json.dumps(definition),
                "format": "json",
            },
        )

    assert result.is_error is False
    assert isinstance(result.data, dict)
    assert result.data["ok"] is True

    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]
    assert sent_definition.get("run_sequentially") is True
    assert sent_definition.get("sequential_key") == "existing-sequential-key"

    params = sent_definition["workflow_definition"]["parameters"]
    cred_params = [p for p in params if p.get("parameter_type") == "credential"]
    assert len(cred_params) == 1
    assert cred_params[0]["key"] == "credentials"
    assert cred_params[0]["credential_id"] == "cred_abc123"

    blocks = sent_definition["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    assert "credentials" in login_block.get("parameter_keys", [])


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
    _patch_skyvern_client(monkeypatch, fake_client)

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
    _patch_skyvern_client(monkeypatch, fake_client)

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
    _patch_skyvern_client(monkeypatch, fake_client)

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
    _patch_skyvern_client(monkeypatch, fake_client)

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
    _patch_skyvern_client(monkeypatch, fake_client)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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
async def test_workflow_update_login_label_fallback_excludes_non_login_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "aws_secret",
                        "key": "api_secret",
                        "aws_key": "my-secret-key",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "task",
                        "label": "api_call",
                        "parameter_keys": ["api_secret"],
                        "navigation_goal": "Make API call",
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "api_call",
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
    login_block = sent_def["workflow_definition"]["blocks"][0]
    assert "api_secret" not in (login_block.get("parameter_keys") or [])


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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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


@pytest.mark.asyncio
async def test_workflow_update_reattaches_credential_when_login_block_type_and_label_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": ["credentials"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "new_login",
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
    workflow_tools.WorkflowCreateYAMLRequestSchema.model_validate(sent_def)
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "new_login")
    assert login_block.get("block_type") == "login"
    assert "credentials" in login_block.get("parameter_keys", [])


@pytest.mark.asyncio
async def test_workflow_update_reattaches_credential_when_login_block_type_dropped_label_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "login_block",
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
    workflow_tools.WorkflowCreateYAMLRequestSchema.model_validate(sent_def)
    blocks = sent_def["workflow_definition"]["blocks"]
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    assert login_block.get("block_type") == "login"
    assert "credentials" in login_block.get("parameter_keys", [])


@pytest.mark.asyncio
async def test_workflow_update_does_not_cross_contaminate_multiple_login_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "cred_a",
                        "credential_id": "credential_a",
                    },
                    {
                        "parameter_type": "credential",
                        "key": "cred_b",
                        "credential_id": "credential_b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_a",
                        "parameter_keys": ["cred_a"],
                    },
                    {
                        "block_type": "login",
                        "label": "login_b",
                        "parameter_keys": ["cred_b"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_a",
                    "parameter_keys": [],
                    "navigation_goal": "Login A",
                },
                {
                    "block_type": "login",
                    "label": "login_b",
                    "parameter_keys": [],
                    "navigation_goal": "Login B",
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
    login_a = next(b for b in blocks if b.get("label") == "login_a")
    login_b = next(b for b in blocks if b.get("label") == "login_b")
    assert login_a.get("parameter_keys", []) == ["cred_a"]
    assert login_b.get("parameter_keys", []) == ["cred_b"]


@pytest.mark.asyncio
async def test_workflow_update_prefers_login_label_match_when_blocks_reordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "cred_a",
                        "credential_id": "credential_a",
                    },
                    {
                        "parameter_type": "credential",
                        "key": "cred_b",
                        "credential_id": "credential_b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_a",
                        "parameter_keys": ["cred_a"],
                    },
                    {
                        "block_type": "login",
                        "label": "login_b",
                        "parameter_keys": ["cred_b"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "login_b",
                    "parameter_keys": [],
                    "navigation_goal": "Login B",
                },
                {
                    "block_type": "login",
                    "label": "login_a",
                    "parameter_keys": [],
                    "navigation_goal": "Login A",
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
    login_b = next(b for b in blocks if b.get("label") == "login_b")
    login_a = next(b for b in blocks if b.get("label") == "login_a")
    assert login_b.get("parameter_keys", []) == ["cred_b"]
    assert login_a.get("parameter_keys", []) == ["cred_a"]


@pytest.mark.asyncio
async def test_workflow_update_does_not_guess_multi_login_credentials_when_labels_renamed_and_reordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "cred_a",
                        "credential_id": "credential_a",
                    },
                    {
                        "parameter_type": "credential",
                        "key": "cred_b",
                        "credential_id": "credential_b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_a",
                        "parameter_keys": ["cred_a"],
                    },
                    {
                        "block_type": "login",
                        "label": "login_b",
                        "parameter_keys": ["cred_b"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "renamed_login_b",
                    "parameter_keys": [],
                    "navigation_goal": "Login B",
                },
                {
                    "block_type": "login",
                    "label": "renamed_login_a",
                    "parameter_keys": [],
                    "navigation_goal": "Login A",
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
    login_b = next(b for b in blocks if b.get("label") == "renamed_login_b")
    login_a = next(b for b in blocks if b.get("label") == "renamed_login_a")
    assert login_b.get("parameter_keys", []) == []
    assert login_a.get("parameter_keys", []) == []


@pytest.mark.asyncio
async def test_workflow_update_does_not_overattach_single_key_when_multiple_login_blocks_renamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "primary_login_cred",
                        "credential_id": "credential_a",
                    },
                    {
                        "parameter_type": "credential",
                        "key": "secondary_login_cred",
                        "credential_id": "credential_b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_a",
                        "parameter_keys": ["primary_login_cred"],
                    },
                    {
                        "block_type": "login",
                        "label": "login_b",
                        "parameter_keys": [],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "renamed_login_a",
                    "parameter_keys": [],
                    "navigation_goal": "Login A",
                },
                {
                    "block_type": "login",
                    "label": "renamed_login_b",
                    "parameter_keys": [],
                    "navigation_goal": "Login B",
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
    login_a = next(b for b in blocks if b.get("label") == "renamed_login_a")
    login_b = next(b for b in blocks if b.get("label") == "renamed_login_b")
    assert login_a.get("parameter_keys", []) == []
    assert login_b.get("parameter_keys", []) == []


@pytest.mark.asyncio
async def test_workflow_update_prefers_label_match_when_untyped_blocks_reordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
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

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "api_call",
                    "navigation_goal": "Make API call",
                },
                {
                    "label": "login_block",
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
    workflow_tools.WorkflowCreateYAMLRequestSchema.model_validate(sent_def)
    blocks = sent_def["workflow_definition"]["blocks"]
    task_block = next(b for b in blocks if b.get("label") == "api_call")
    login_block = next(b for b in blocks if b.get("label") == "login_block")
    assert task_block.get("block_type") == "task"
    assert task_block.get("parameter_keys", []) == ["api_secret"]
    assert login_block.get("block_type") == "login"
    assert login_block.get("parameter_keys", []) == ["credentials"]


@pytest.mark.asyncio
async def test_workflow_update_does_not_guess_non_login_secrets_when_untyped_labels_renamed_and_reordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "aws_secret",
                        "key": "secret_a",
                        "aws_key": "secret-a",
                    },
                    {
                        "parameter_type": "aws_secret",
                        "key": "secret_b",
                        "aws_key": "secret-b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "task",
                        "label": "task_a",
                        "parameter_keys": ["secret_a"],
                        "navigation_goal": "Task A",
                    },
                    {
                        "block_type": "task",
                        "label": "task_b",
                        "parameter_keys": ["secret_b"],
                        "navigation_goal": "Task B",
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "renamed_task_b",
                    "navigation_goal": "Task B",
                },
                {
                    "label": "renamed_task_a",
                    "navigation_goal": "Task A",
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
    task_b = next(b for b in blocks if b.get("label") == "renamed_task_b")
    task_a = next(b for b in blocks if b.get("label") == "renamed_task_a")
    assert task_b.get("parameter_keys", []) == []
    assert task_a.get("parameter_keys", []) == []
    assert "block_type" not in task_b
    assert "block_type" not in task_a


@pytest.mark.asyncio
async def test_workflow_update_does_not_restore_untyped_login_from_renamed_reordered_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": ["credentials"],
                    },
                    {
                        "block_type": "task",
                        "label": "old_task",
                        "parameter_keys": [],
                        "navigation_goal": "Do task",
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "renamed_task",
                    "navigation_goal": "Do task",
                },
                {
                    "label": "renamed_login",
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
    blocks = sent_def["workflow_definition"]["blocks"]
    task_block = next(b for b in blocks if b.get("label") == "renamed_task")
    login_block = next(b for b in blocks if b.get("label") == "renamed_login")
    assert task_block.get("parameter_keys", []) == []
    assert login_block.get("parameter_keys", []) == []
    assert "block_type" not in task_block
    assert "block_type" not in login_block


@pytest.mark.asyncio
async def test_workflow_update_does_not_attach_all_login_credentials_when_mapping_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "cred_a",
                        "credential_id": "credential_a",
                    },
                    {
                        "parameter_type": "credential",
                        "key": "cred_b",
                        "credential_id": "credential_b",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "login_a",
                        "parameter_keys": ["cred_a"],
                    },
                    {
                        "block_type": "login",
                        "label": "login_b",
                        "parameter_keys": ["cred_b"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "renamed_login",
                    "parameter_keys": [],
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
    login_block = sent_def["workflow_definition"]["blocks"][0]
    assert login_block.get("parameter_keys", []) == []


@pytest.mark.asyncio
async def test_workflow_update_login_in_nested_loop_reattaches_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "loop_block",
                        "loop_blocks": [
                            {
                                "block_type": "login",
                                "label": "old_login",
                                "parameter_keys": ["credentials"],
                            },
                        ],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

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
                            "label": "new_login",
                            "navigation_goal": "Login",
                        },
                    ],
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
    workflow_tools.WorkflowCreateYAMLRequestSchema.model_validate(sent_def)
    loop_block = sent_def["workflow_definition"]["blocks"][0]
    login_block = loop_block["loop_blocks"][0]
    assert login_block.get("block_type") == "login"
    assert "credentials" in login_block.get("parameter_keys", [])


@pytest.mark.asyncio
async def test_workflow_update_single_login_block_still_gets_all_login_keys_when_unmatchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": [],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "new_login",
                    "parameter_keys": [],
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
    login_block = sent_def["workflow_definition"]["blocks"][0]
    assert "credentials" in login_block.get("parameter_keys", [])


@pytest.mark.asyncio
async def test_workflow_update_reattaches_unique_login_block_key_when_other_login_capable_params_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "login_cred",
                        "credential_id": "cred_login",
                    },
                    {
                        "parameter_type": "workflow",
                        "key": "api_cred",
                        "workflow_parameter_type": "credential_id",
                        "default_value": "cred_api",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": ["login_cred"],
                    },
                    {
                        "block_type": "task",
                        "label": "api_task",
                        "parameter_keys": ["api_cred"],
                        "navigation_goal": "Call API",
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "login",
                    "label": "renamed_login",
                    "parameter_keys": [],
                    "navigation_goal": "Login",
                },
                {
                    "block_type": "task",
                    "label": "api_task",
                    "parameter_keys": [],
                    "navigation_goal": "Call API",
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
    login_block = next(b for b in blocks if b.get("label") == "renamed_login")
    task_block = next(b for b in blocks if b.get("label") == "api_task")
    assert login_block.get("parameter_keys", []) == ["login_cred"]
    assert task_block.get("parameter_keys", []) == ["api_cred"]


@pytest.mark.asyncio
async def test_workflow_update_untyped_single_login_block_still_gets_single_login_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": [],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "new_login",
                    "parameter_keys": [],
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
    workflow_tools.WorkflowCreateYAMLRequestSchema.model_validate(sent_def)
    login_block = sent_def["workflow_definition"]["blocks"][0]
    assert login_block.get("block_type") == "login"
    assert login_block.get("parameter_keys", []) == ["credentials"]


@pytest.mark.asyncio
async def test_workflow_update_does_not_duplicate_keys_on_positional_reinjection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "credential",
                        "key": "credentials",
                        "credential_id": "cred_a",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "login",
                        "label": "old_login",
                        "parameter_keys": ["credentials"],
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [
                {
                    "label": "new_login",
                    "parameter_keys": ["credentials"],
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
    login_block = sent_def["workflow_definition"]["blocks"][0]
    assert login_block.get("parameter_keys", []).count("credentials") == 1


# --- Non-credential parameter detachment (MCP get->edit->update round trip) ---


@pytest.mark.asyncio
async def test_workflow_update_preserves_noncredential_parameter_keys_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer B: a still-declared non-credential parameter that the update drops from a block's
    parameter_keys (block matched by stable label) is re-attached, and the reattachment is surfaced
    as a warning. Mirrors the credential reattachment but for plain workflow params."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "report_url",
                        "workflow_parameter_type": "string",
                        "default_value": "https://example.com/report",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "code",
                        "label": "build_report",
                        "parameter_keys": ["report_url"],
                        "code": "x = report_url",
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    # Keeps the param declared, but the regenerated block dropped it from parameter_keys.
    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "report_url",
                    "workflow_parameter_type": "string",
                    "default_value": "https://example.com/report",
                },
            ],
            "blocks": [
                {"block_type": "code", "label": "build_report", "code": "x = report_url  # edited"},
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
    block = sent_def["workflow_definition"]["blocks"][0]
    assert block.get("parameter_keys") == ["report_url"]
    warnings = result.get("data", {}).get("warnings") or []
    assert any("report_url" in w and "build_report" in w for w in warnings)


@pytest.mark.asyncio
async def test_workflow_update_does_not_reinject_intentionally_removed_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer B guard: if the update removes the parameter DECLARATION too (not just the block link),
    the link is not re-injected — that is the explicit-removal path."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {"parameter_type": "workflow", "key": "report_url", "workflow_parameter_type": "string"},
                ],
                "blocks": [
                    {"block_type": "code", "label": "build_report", "parameter_keys": ["report_url"], "code": "x"},
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [{"block_type": "code", "label": "build_report", "code": "x"}],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True
    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    block = sent_def["workflow_definition"]["blocks"][0]
    assert "report_url" not in (block.get("parameter_keys") or [])


@pytest.mark.asyncio
async def test_workflow_get_advertises_persisted_code_blocks_without_mutating_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocks = [
        {
            "block_type": "code",
            "label": f"reusable_step_{index:02d}",
            "code": f'return {{"step": {index}}}',
        }
        for index in range(22)
    ]
    workflow_definition: dict[str, object] = {"parameters": [], "blocks": blocks}
    before = json.dumps(workflow_definition, ensure_ascii=False, separators=(",", ":")).encode()
    _patch_workflow_get_definition(monkeypatch, workflow_definition)

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")

    assert result["ok"] is True
    data = result["data"]
    pointer = data["code_block_pointer"]
    assert pointer["code_block_count"] == 22
    assert pointer["block_labels"] == [f"reusable_step_{index:02d}" for index in range(20)]
    # The normal path carries the code inline, so it must point at the response — not at the CLI
    # fallback. Asserting the absence keeps the overflow hint from satisfying this test.
    assert "workflow_definition" in pointer["hint"]
    assert "--definition-file" not in pointer["hint"]
    after = json.dumps(data["workflow_definition"], ensure_ascii=False, separators=(",", ":")).encode()
    assert after == before
    assert "code_block_pointer" not in data["workflow_definition"]


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param(
            [
                {
                    "block_type": "navigation",
                    "label": "open_page",
                    "url": "https://example.com",
                    "navigation_goal": "Open the page",
                }
            ],
            id="agent-only",
        ),
        pytest.param([{"block_type": "code", "label": "empty_shell", "code": ""}], id="empty-code"),
        pytest.param([{"block_type": "code", "label": "blank_shell", "code": "   \n"}], id="blank-code"),
        pytest.param([], id="no-blocks"),
    ],
)
@pytest.mark.asyncio
async def test_workflow_get_omits_code_block_pointer_without_reusable_code(
    monkeypatch: pytest.MonkeyPatch,
    blocks: list[dict[str, object]],
) -> None:
    workflow_definition: dict[str, object] = {"parameters": [], "blocks": blocks}
    _patch_workflow_get_definition(monkeypatch, workflow_definition)

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")

    assert result["ok"] is True
    assert "code_block_pointer" not in result["data"]
    assert result["data"]["workflow_definition"] == workflow_definition


@pytest.mark.asyncio
async def test_workflow_get_counts_nested_code_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_definition: dict[str, object] = {
        "parameters": [],
        "blocks": [
            {
                "block_type": "for_loop",
                "label": "each_item",
                "loop_blocks": [
                    {
                        "block_type": "code",
                        "label": "reuse_nested_code",
                        "code": 'return {"ok": True}',
                    }
                ],
            }
        ],
    }
    _patch_workflow_get_definition(monkeypatch, workflow_definition)

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")

    assert result["data"]["code_block_pointer"]["code_block_count"] == 1
    assert result["data"]["code_block_pointer"]["block_labels"] == ["reuse_nested_code"]


@pytest.mark.asyncio
async def test_wrapped_large_workflow_get_preserves_code_block_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workflow_get_definition(monkeypatch, _large_runtime_workflow_definition())
    wrapped_get = size_capped(workflow_tools.guard_definition_size(workflow_tools.skyvern_workflow_get))

    result = await wrapped_get(workflow_id="wpid_test")

    assert result["ok"] is False
    pointer = result["data"]["code_block_pointer"]
    assert pointer["code_block_count"] == 88
    assert pointer["block_labels"] == [f"portal_step_{index:03d}" for index in range(20)]
    # The overflow branch drops the definition, so the hint must route to the CLI and must NOT tell the
    # caller to read a workflow_definition that is not in this response.
    assert "skyvern workflow get --id wpid_test --definition-file wf.json" in pointer["hint"]
    assert "workflow_definition" not in pointer["hint"]


@pytest.mark.asyncio
async def test_wrapped_large_workflow_get_pointer_survives_oversized_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_definition: dict[str, object] = {
        "parameters": [],
        "blocks": [
            {"block_type": "code", "label": "l" * 20_000, "code": f'return {{"step": {index}}}'} for index in range(30)
        ],
    }
    _patch_workflow_get_definition(monkeypatch, workflow_definition)
    wrapped_get = size_capped(workflow_tools.guard_definition_size(workflow_tools.skyvern_workflow_get))

    result = await wrapped_get(workflow_id="wpid_test")

    # 600K of labels must not re-inflate the oversize response past the cap and get the whole pointer
    # stripped by the outer size_capped. No label is previewable here, but the count stays exact.
    pointer = result["data"]["code_block_pointer"]
    assert pointer["code_block_count"] == 30
    assert pointer["block_labels"] == []
    assert workflow_tools._response_size(result) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_code_block_pointer_survives_concise_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concise is the MCP server's default response mode and it drops every key in `_DATA_STRIP_KEYS` —
    which already contains `sdk_equivalent`, the additive sibling this pointer sits beside and is modelled
    on. That neighbour is disposable chatter; this pointer is the whole feature. Adding it to that strip
    list would kill discovery in the only mode that ships, and nothing else would fail.
    """
    _patch_workflow_get_definition(
        monkeypatch,
        {"parameters": [], "blocks": [{"block_type": "code", "label": "reuse_me", "code": 'return {"ok": True}'}]},
    )

    set_concise_responses(True)
    try:
        result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    finally:
        set_concise_responses(False)

    assert result["data"]["code_block_pointer"]["code_block_count"] == 1
    # The contrast that gives this test its point: the neighbouring key IS stripped here.
    assert "sdk_equivalent" not in result["data"]


@pytest.mark.asyncio
async def test_code_bearing_workflow_inside_the_pointer_shifted_band_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pointer counts against the cap it is measured by, so a code-bearing workflow that would have
    fit without it now takes the oversize path. That shift has to fail closed: definition dropped, but
    the pointer and a working CLI route survive and the response stays under the cap. Not measuring the
    pointer would be worse — the over-cap response would reach size_capped, which drops `data` entirely.
    """

    def _definition(pad: int) -> dict[str, object]:
        return {"parameters": [], "blocks": [{"block_type": "code", "label": "near_cap", "code": "x" * pad}]}

    probe_pad = 1_000
    _patch_workflow_get_definition(monkeypatch, _definition(probe_pad))
    probe = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    pointer_chars = workflow_tools._response_size({"code_block_pointer": probe["data"]["code_block_pointer"]})
    # Response size is linear in the padding, so solve for a payload landing mid-band.
    base = workflow_tools._response_size(probe) - probe_pad - pointer_chars
    pad = MCP_MAX_RESPONSE_CHARS - base - pointer_chars // 2

    _patch_workflow_get_definition(monkeypatch, _definition(pad))
    unwrapped = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    with_pointer = workflow_tools._response_size(unwrapped)
    without_pointer = with_pointer - pointer_chars
    # Self-check: the case is only meaningful if it is genuinely in-band. Without this the test would
    # silently pass on any merely-oversized workflow and prove nothing.
    assert without_pointer <= MCP_MAX_RESPONSE_CHARS < with_pointer

    wrapped_get = size_capped(workflow_tools.guard_definition_size(workflow_tools.skyvern_workflow_get))
    result = await wrapped_get(workflow_id="wpid_test")

    assert result["ok"] is False
    assert "workflow_definition" not in result["data"]
    assert result["data"]["code_block_pointer"]["code_block_count"] == 1
    assert "--definition-file" in result["data"]["code_block_pointer"]["hint"]
    assert workflow_tools._response_size(result) <= MCP_MAX_RESPONSE_CHARS


@pytest.mark.asyncio
async def test_workflow_get_previews_labels_up_to_the_budget_while_count_stays_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Labels long enough that the CHAR budget binds before the 20-label count cap does — otherwise this
    # only re-tests the count cap and says nothing about the budget.
    blocks = [
        {
            "block_type": "code",
            "label": f"portal_step_{index:03d}_submit_and_verify_the_result_page_loaded",
            "code": 'return {"ok": True}',
        }
        for index in range(25)
    ]
    _patch_workflow_get_definition(monkeypatch, {"parameters": [], "blocks": blocks})

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")

    pointer = result["data"]["code_block_pointer"]
    assert pointer["code_block_count"] == 25
    assert pointer["block_labels"]
    # Fewer than the count cap ⇒ the char budget is what stopped it, not _POINTER_MAX_LABELS.
    assert len(pointer["block_labels"]) < workflow_tools._POINTER_MAX_LABELS
    assert len("".join(pointer["block_labels"])) <= workflow_tools._POINTER_LABEL_BUDGET_CHARS
    assert set(pointer["block_labels"]) <= {str(block["label"]) for block in blocks}


@pytest.mark.asyncio
async def test_workflow_get_emits_parameter_keys_and_strips_runtime_block_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer A: skyvern_workflow_get returns the authoring shape — derive block parameter_keys from
    resolved runtime `parameters`, strip `parameters`/`output_parameter`, and drop auto output params
    from the top-level parameter list."""

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(
            workflow_definition={
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "report_url",
                        "workflow_parameter_type": "string",
                        "default_value": "https://e.com",
                        "workflow_parameter_id": "wp_1",
                        "workflow_id": "wf_test",
                    },
                    {
                        "parameter_type": "output",
                        "key": "build_report_output",
                        "output_parameter_id": "op_1",
                        "workflow_id": "wf_test",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "code",
                        "label": "build_report",
                        "code": "x = report_url",
                        "parameters": [
                            {
                                "parameter_type": "workflow",
                                "key": "report_url",
                                "workflow_parameter_type": "string",
                                "default_value": "https://e.com",
                                "workflow_parameter_id": "wp_1",
                                "workflow_id": "wf_test",
                            },
                        ],
                        "output_parameter": {
                            "parameter_type": "output",
                            "key": "build_report_output",
                            "output_parameter_id": "op_1",
                            "workflow_id": "wf_test",
                        },
                    },
                ],
            }
        )

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    assert result["ok"] is True
    wf_def = result["data"]["workflow_definition"]
    block = wf_def["blocks"][0]
    assert block.get("parameter_keys") == ["report_url"]
    assert "parameters" not in block
    assert "output_parameter" not in block
    top_keys = [p["key"] for p in wf_def["parameters"]]
    assert "report_url" in top_keys
    assert "build_report_output" not in top_keys


@pytest.mark.asyncio
async def test_workflow_get_maps_for_loop_and_context_source_to_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer A: for-loop runtime `loop_over` object maps to authoring `loop_over_parameter_key`, and a
    context parameter's runtime `source` object maps to `source_parameter_key`."""

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(
            workflow_definition={
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "urls",
                        "workflow_parameter_type": "json",
                        "default_value": [],
                    },
                    {
                        "parameter_type": "context",
                        "key": "current_url",
                        "source": {"parameter_type": "workflow", "key": "urls", "workflow_parameter_type": "json"},
                    },
                ],
                "blocks": [
                    {
                        "block_type": "for_loop",
                        "label": "each_url",
                        "loop_over": {
                            "parameter_type": "workflow",
                            "key": "urls",
                            "workflow_parameter_type": "json",
                            "workflow_parameter_id": "wp_urls",
                        },
                        "loop_blocks": [
                            {"block_type": "code", "label": "inner", "code": "y", "parameters": []},
                        ],
                    },
                ],
            }
        )

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    result = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    assert result["ok"] is True
    wf_def = result["data"]["workflow_definition"]
    loop_block = wf_def["blocks"][0]
    assert loop_block.get("loop_over_parameter_key") == "urls"
    assert "loop_over" not in loop_block
    context_param = next(p for p in wf_def["parameters"] if p["key"] == "current_url")
    assert context_param.get("source_parameter_key") == "urls"
    assert "source" not in context_param


@pytest.mark.asyncio
async def test_workflow_update_preserves_noncredential_link_from_runtime_shaped_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer B (prod shape): the existing workflow from GET is in RUNTIME shape — block links live in
    resolved `parameters` objects, not `parameter_keys`. Step 3 must read that shape to detect the
    prior link and re-attach the omitted key."""

    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return {
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "report_url",
                        "workflow_parameter_type": "string",
                        "default_value": "https://e.com",
                        "workflow_parameter_id": "wp_1",
                        "workflow_id": "wf_test",
                    },
                ],
                "blocks": [
                    {
                        "block_type": "code",
                        "label": "build_report",
                        "code": "x = report_url",
                        "parameters": [
                            {
                                "parameter_type": "workflow",
                                "key": "report_url",
                                "workflow_parameter_type": "string",
                                "default_value": "https://e.com",
                                "workflow_parameter_id": "wp_1",
                                "workflow_id": "wf_test",
                            },
                        ],
                        "output_parameter": {
                            "parameter_type": "output",
                            "key": "build_report_output",
                            "output_parameter_id": "op_1",
                            "workflow_id": "wf_test",
                        },
                    },
                ],
            },
        }

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    definition = {
        "title": "Updated workflow",
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [
                {
                    "parameter_type": "workflow",
                    "key": "report_url",
                    "workflow_parameter_type": "string",
                    "default_value": "https://e.com",
                },
            ],
            "blocks": [{"block_type": "code", "label": "build_report", "code": "x = report_url  # edited"}],
        },
    }

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True
    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    block = sent_def["workflow_definition"]["blocks"][0]
    assert block.get("parameter_keys") == ["report_url"]
    warnings = result.get("data", {}).get("warnings") or []
    assert any("report_url" in w and "build_report" in w for w in warnings)


@pytest.mark.asyncio
async def test_get_then_update_round_trip_preserves_block_parameter_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: GET returns the authoring shape; feeding it straight back into update preserves the
    block<->parameter link with no manual re-add, and leaks no runtime fields to the backend."""

    runtime_definition = {
        "parameters": [
            {
                "parameter_type": "workflow",
                "key": "report_url",
                "workflow_parameter_type": "string",
                "default_value": "https://e.com",
                "workflow_parameter_id": "wp_1",
                "workflow_id": "wf_test",
            },
        ],
        "blocks": [
            {
                "block_type": "code",
                "label": "build_report",
                "code": "x = report_url",
                "parameters": [
                    {
                        "parameter_type": "workflow",
                        "key": "report_url",
                        "workflow_parameter_type": "string",
                        "default_value": "https://e.com",
                        "workflow_parameter_id": "wp_1",
                        "workflow_id": "wf_test",
                    },
                ],
                "output_parameter": {
                    "parameter_type": "output",
                    "key": "build_report_output",
                    "output_parameter_id": "op_1",
                    "workflow_id": "wf_test",
                },
            },
        ],
    }

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(proxy_location="RESIDENTIAL", workflow_definition=runtime_definition)

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    got = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    assert got["ok"] is True
    authoring_wf_def = got["data"]["workflow_definition"]
    echo = {"title": "Round trip", "proxy_location": "RESIDENTIAL", "workflow_definition": authoring_wf_def}

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(echo),
        format="json",
    )

    assert result["ok"] is True
    sent_def = request_mock.await_args.kwargs["json"]["json_definition"]
    block = sent_def["workflow_definition"]["blocks"][0]
    assert block.get("parameter_keys") == ["report_url"]
    assert "parameters" not in block
    assert "output_parameter" not in block


@pytest.mark.asyncio
async def test_large_workflow_surgical_edit_preserves_every_other_field(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_definition = _large_runtime_workflow_definition()

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(proxy_location="RESIDENTIAL", workflow_definition=runtime_definition)

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)
    request_mock = _patch_skyvern_http(monkeypatch, response_payload=_fake_workflow_dict())

    got = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")
    assert got["ok"] is True
    assert len(json.dumps(got, ensure_ascii=False)) > MCP_MAX_RESPONSE_CHARS
    authoring_definition = got["data"]["workflow_definition"]
    serialized_definition = json.dumps(
        {
            "title": got["data"]["title"],
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": authoring_definition,
        }
    )
    authoring_blocks = workflow_tools._iter_blocks_flat(authoring_definition["blocks"])

    assert len(serialized_definition.encode()) >= 150_000
    assert len(authoring_blocks) == 100
    assert len(authoring_definition["parameters"]) == 30
    assert {"code", "navigation", "extraction", "for_loop"} <= {block["block_type"] for block in authoring_blocks}
    WorkflowRequest.model_validate(
        {
            "json_definition": {
                "title": got["data"]["title"],
                "proxy_location": "RESIDENTIAL",
                "workflow_definition": authoring_definition,
            }
        }
    )

    normalized_definition = workflow_tools._normalize_json_definition(
        {
            "title": got["data"]["title"],
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": authoring_definition,
        }
    )["workflow_definition"]
    expected_definition = json.loads(json.dumps(normalized_definition))
    edited_block = next(block for block in authoring_blocks if block["block_type"] == "code")
    edited_block["code"] = f"{edited_block['code']}\nresult = 'surgical edit'"
    expected_edited_block = next(
        block
        for block in workflow_tools._iter_blocks_flat(expected_definition["blocks"])
        if block["label"] == edited_block["label"]
    )
    expected_edited_block["code"] = edited_block["code"]
    serialized_definition = json.dumps(
        {
            "title": got["data"]["title"],
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": authoring_definition,
        }
    )

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=serialized_definition,
        format="json",
    )

    assert result["ok"] is True
    sent_definition = request_mock.await_args.kwargs["json"]["json_definition"]["workflow_definition"]
    assert sent_definition == expected_definition


@pytest.mark.parametrize(("version", "expected_version_hint"), [(7, "--version 7"), (None, None)])
@pytest.mark.asyncio
async def test_registered_large_workflow_get_returns_structured_cli_fallback(
    monkeypatch: pytest.MonkeyPatch,
    version: int | None,
    expected_version_hint: str | None,
) -> None:
    runtime_definition = _large_runtime_workflow_definition()

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return _fake_workflow_dict(
            proxy_location="RESIDENTIAL",
            workflow_definition=runtime_definition,
            version=version or 1,
        )

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)

    arguments: dict[str, object] = {"workflow_id": "wpid_test"}
    if version is not None:
        arguments["version"] = version
    async with Client(mcp) as client:
        result = await client.call_tool("skyvern_workflow_get", arguments)

    assert result.is_error is False
    assert isinstance(result.data, dict)
    assert result.data["ok"] is False
    assert "_truncated" not in result.data
    assert str(MCP_MAX_RESPONSE_CHARS) in result.data["error"]["message"]
    get_hint = result.data["error"]["hint"].split(", edit wf.json", 1)[0]
    assert (expected_version_hint in get_hint) if expected_version_hint is not None else "--version" not in get_hint
    assert "skyvern workflow update --id wpid_test --definition @wf.json" in result.data["error"]["hint"]
    data = result.data["data"]
    assert (data["workflow_permanent_id"], data["title"], data["version"]) == (
        "wpid_test",
        "Example Workflow",
        version or 1,
    )
    assert (data["block_count"], data["parameter_count"]) == (100, 30)
    assert data["definition_chars"] >= 150_000


@pytest.mark.asyncio
async def test_registered_small_workflow_get_passes_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _fake_workflow_dict(
        proxy_location="RESIDENTIAL",
        workflow_definition={"parameters": [], "blocks": []},
    )

    async def fake_get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, object]:
        return workflow

    _patch_get_workflow_by_id(monkeypatch, fake_get_workflow_by_id)
    direct = await workflow_tools.skyvern_workflow_get(workflow_id="wpid_test")

    async with Client(mcp) as client:
        registered = await client.call_tool("skyvern_workflow_get", {"workflow_id": "wpid_test"})

    assert registered.is_error is False
    assert isinstance(registered.data, dict)
    assert registered.data["data"] == direct["data"]
    assert registered.data["error"] is None and "_truncated" not in registered.data
