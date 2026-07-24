from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastmcp.tools import FunctionTool

import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools._common import CODE_ONLY_FIELD_DESCRIPTION, CODE_ONLY_POLICY_HINT
from tests.unit._mcp_test_helpers import patch_get_workflow_by_id as _patch_get_workflow_by_id
from tests.unit._mcp_test_helpers import patch_skyvern_client as _patch_skyvern_client


def _fake_workflow_dict(**overrides: object) -> dict[str, object]:
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


def _patch_skyvern_http(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    response = SimpleNamespace(
        status_code=200,
        json=lambda: _fake_workflow_dict(),
        text=json.dumps(_fake_workflow_dict()),
    )
    request_mock = AsyncMock(return_value=response)
    fake_skyvern = SimpleNamespace(
        _client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request_mock)),
    )
    _patch_skyvern_client(monkeypatch, fake_skyvern)
    return request_mock


def _patch_existing_workflow(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    existing_workflow = {
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": [],
            "blocks": [],
        },
    }
    get_workflow_by_id = AsyncMock(return_value=existing_workflow)
    _patch_get_workflow_by_id(monkeypatch, get_workflow_by_id)
    return get_workflow_by_id


def _navigation_block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": "navigation",
        "label": "visit_page",
        "url": "https://example.com",
        "navigation_goal": "Open the page",
    }
    block.update(overrides)
    return block


def _code_block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "block_type": "code",
        "label": "visit_in_code",
        "code": 'await page.goto("https://example.com")\nreturn {"ok": True}',
    }
    block.update(overrides)
    return block


def _workflow_definition(blocks: list[object], **overrides: object) -> dict[str, object]:
    definition: dict[str, object] = {
        "title": "Code only workflow",
        "workflow_definition": {
            "parameters": [],
            "blocks": blocks,
        },
    }
    definition.update(overrides)
    return definition


def _serialized_definition(definition: dict[str, object], serialized_as: str) -> str:
    if serialized_as == "json":
        return json.dumps(definition)
    return yaml.safe_dump(definition, sort_keys=False)


def _sent_definition(request_mock: AsyncMock) -> dict[str, object]:
    body = request_mock.await_args.kwargs["json"]
    raw = body.get("json_definition")
    if raw is None:
        raw = yaml.safe_load(body["yaml_definition"])
    assert isinstance(raw, dict)
    return raw


def _assert_code_only_rejection(result: dict[str, object], *, label: str) -> None:
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == workflow_tools.ErrorCode.INVALID_INPUT
    assert "not allowed in code-only mode" in str(error["message"])
    assert label in str(error["message"])
    assert "use a `code` block" in str(error["hint"])
    # Exact-string on purpose: the guidance text is the behavior surface under test — a
    # keyword check would pass on inverted advice ("pass code_only=false to continue").
    assert CODE_ONLY_POLICY_HINT in str(error["hint"])


@pytest.mark.asyncio
async def test_registered_code_only_tool_schemas_are_nullable_without_false_default() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    for tool_name in ("skyvern_block_validate", "skyvern_workflow_create", "skyvern_workflow_update"):
        tool = tools[tool_name]
        assert isinstance(tool, FunctionTool)
        code_only_schema = tool.parameters["properties"]["code_only"]
        assert {choice["type"] for choice in code_only_schema["anyOf"]} == {"boolean", "null"}
        assert code_only_schema.get("default") is None
        assert code_only_schema["description"] == CODE_ONLY_FIELD_DESCRIPTION


@pytest.mark.asyncio
async def test_workflow_create_rejects_navigation_block_in_code_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition([_navigation_block()])

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    _assert_code_only_rejection(result, label="visit_page")
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_create_defaults_code_only_off_and_persists_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition([_navigation_block()])

    result = await workflow_tools.skyvern_workflow_create(definition=json.dumps(definition), format="json")

    assert result["ok"] is True, result
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_create_accepts_code_block_in_code_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition([_code_block()])

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    assert result["ok"] is True, result
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_create_rejects_task_block_nested_in_loop_in_code_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition(
        [
            {
                "block_type": "for_loop",
                "label": "for_each_item",
                "loop_over_parameter_key": "items",
                "loop_blocks": [
                    {
                        "block_type": "task",
                        "label": "legacy_task",
                        "url": "https://example.com",
                        "navigation_goal": "Open the page",
                    }
                ],
            }
        ]
    )

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    _assert_code_only_rejection(result, label="legacy_task")
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_create_rejects_label_less_navigation_block_in_code_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    navigation_block = _navigation_block()
    navigation_block.pop("label")
    definition = _workflow_definition([navigation_block])

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    _assert_code_only_rejection(result, label="(unlabeled)")
    request_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_update_rejects_navigation_block_in_code_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    get_workflow_by_id = _patch_existing_workflow(monkeypatch)
    definition = _workflow_definition([_navigation_block()])

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    _assert_code_only_rejection(result, label="visit_page")
    request_mock.assert_not_awaited()
    get_workflow_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_update_accepts_code_block_in_code_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    _patch_existing_workflow(monkeypatch)
    definition = _workflow_definition(
        [_code_block()],
        proxy_location="RESIDENTIAL",
        run_sequentially=True,
        sequential_key="code-only",
    )

    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    assert result["ok"] is True, result
    request_mock.assert_awaited_once()


@pytest.mark.parametrize(
    ("format_name", "serialized_as"),
    [("json", "json"), ("yaml", "yaml"), ("auto", "json"), ("auto", "yaml")],
    ids=["json", "yaml", "auto-json", "auto-yaml"],
)
@pytest.mark.asyncio
async def test_workflow_create_auto_wires_code_blocks_in_every_format(
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
    serialized_as: str,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition(
        [
            _code_block(label="top_level"),
            {
                "block_type": "for_loop",
                "label": "for_each_item",
                "loop_over_parameter_key": "items",
                "loop_blocks": [_code_block(label="nested")],
            },
        ],
        proxy_location="RESIDENTIAL",
        code_version=2,
        run_with="agent",
    )
    definition["workflow_definition"]["parameters"] = [
        {
            "parameter_type": "workflow",
            "key": "value",
            "workflow_parameter_type": "string",
            "default_value": "example",
        }
    ]

    result = await workflow_tools.skyvern_workflow_create(
        definition=_serialized_definition(definition, serialized_as),
        format=format_name,
        code_only=True,
    )

    assert result["ok"] is True, result
    blocks = _sent_definition(request_mock)["workflow_definition"]["blocks"]
    assert blocks[0]["parameter_keys"] == ["value"]
    assert blocks[1]["loop_blocks"][0]["parameter_keys"] == ["value"]


@pytest.mark.asyncio
async def test_workflow_create_auto_wire_respects_parameter_key_field_states_and_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    safe_types = ["string", "integer", "float", "boolean", "json", "file_url"]
    definition = _workflow_definition(
        [
            _code_block(label="missing"),
            _code_block(label="null", parameter_keys=None),
            _code_block(label="empty", parameter_keys=[]),
            _code_block(label="malformed_string", parameter_keys=""),
            _code_block(label="malformed_dict", parameter_keys={}),
            _code_block(label="malformed_zero", parameter_keys=0),
            _code_block(label="explicit", parameter_keys=["caller_supplied"]),
            {
                "block_type": "for_loop",
                "label": "loop",
                "loop_over_parameter_key": "items",
                "loop_blocks": [_code_block(label="nested_opt_out", parameter_keys=[])],
            },
        ],
        proxy_location="RESIDENTIAL",
        code_version=2,
        run_with="agent",
    )
    definition["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": f"input_{kind}", "workflow_parameter_type": kind} for kind in safe_types
    ]

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    assert result["ok"] is True, result
    blocks = _sent_definition(request_mock)["workflow_definition"]["blocks"]
    by_label = {block["label"]: block for block in blocks}
    eligible = [f"input_{kind}" for kind in safe_types]
    assert by_label["missing"]["parameter_keys"] == eligible
    assert by_label["null"]["parameter_keys"] == eligible
    assert by_label["missing"]["parameter_keys"] is not by_label["null"]["parameter_keys"]
    assert by_label["empty"]["parameter_keys"] == []
    assert by_label["loop"]["loop_blocks"][0]["parameter_keys"] == []
    assert by_label["malformed_string"]["parameter_keys"] == ""
    assert by_label["malformed_dict"]["parameter_keys"] == {}
    assert by_label["malformed_zero"]["parameter_keys"] == 0
    assert by_label["explicit"]["parameter_keys"] == ["caller_supplied"]


@pytest.mark.asyncio
async def test_workflow_create_auto_wire_excludes_unsafe_and_malformed_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition(
        [_code_block()],
        proxy_location="RESIDENTIAL",
        code_version=2,
        run_with="agent",
    )
    definition["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": "eligible", "workflow_parameter_type": "string"},
        {"parameter_type": "credential", "key": "direct_credential"},
        {"parameter_type": "aws_secret", "key": "direct_secret"},
        {"parameter_type": "workflow", "key": "credential_ref", "workflow_parameter_type": "credential_id"},
        {"parameter_type": "context", "key": "context_value"},
        {"parameter_type": "output", "key": "output_value"},
        {"parameter_type": "unknown", "key": "unknown_type"},
        {"parameter_type": "workflow", "key": "unknown_subtype", "workflow_parameter_type": "mystery"},
        {"parameter_type": "workflow", "key": "missing_subtype"},
        {"parameter_type": "workflow", "key": "duplicate", "workflow_parameter_type": "string"},
        {"parameter_type": "credential", "key": "duplicate"},
    ]

    result = await workflow_tools.skyvern_workflow_create(
        definition=json.dumps(definition),
        format="json",
        code_only=True,
    )

    assert result["ok"] is True, result
    block = _sent_definition(request_mock)["workflow_definition"]["blocks"][0]
    assert block["parameter_keys"] == ["eligible"]


@pytest.mark.parametrize("serialized_as", ["json", "yaml"])
@pytest.mark.asyncio
async def test_workflow_create_defaults_code_block_prompt(
    monkeypatch: pytest.MonkeyPatch,
    serialized_as: str,
) -> None:
    """An MCP-created code block without a prompt persists with prompt "" (new code block experience)."""
    request_mock = _patch_skyvern_http(monkeypatch)
    definition = _workflow_definition([_code_block()])

    result = await workflow_tools.skyvern_workflow_create(
        definition=_serialized_definition(definition, serialized_as),
        format=serialized_as,
    )

    assert result["ok"] is True, result
    sent = _sent_definition(request_mock)
    blocks = sent["workflow_definition"]["blocks"]
    assert blocks[0]["prompt"] == ""


@pytest.mark.parametrize("format_name", ["yaml", "auto"])
@pytest.mark.asyncio
async def test_workflow_create_code_only_false_preserves_yaml_bytes(
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    # prompt supplied explicitly: an omitted prompt is now defaulted (new code block
    # experience), which intentionally rewrites the definition.
    definition = _workflow_definition(
        [_code_block(prompt="")],
        proxy_location="RESIDENTIAL",
        code_version=2,
        run_with="agent",
    )
    definition["workflow_definition"]["parameters"] = [
        {"parameter_type": "workflow", "key": "value", "workflow_parameter_type": "string"}
    ]
    serialized = yaml.safe_dump(definition, sort_keys=False)

    result = await workflow_tools.skyvern_workflow_create(
        definition=serialized,
        format=format_name,
        code_only=False,
    )

    assert result["ok"] is True, result
    assert request_mock.await_args.kwargs["json"]["yaml_definition"] == serialized
