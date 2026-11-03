from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import skyvern.cli.mcp_tools.workflow as workflow_tools
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
        "title": "Auto-wire workflow",
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
    """An MCP-created code block without a prompt persists with prompt "" (new code block experience)
    and steps derived from its code."""
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
    assert [step["action_type"] for step in blocks[0]["steps"]] == ["goto_url"]


@pytest.mark.asyncio
async def test_workflow_update_derives_steps_only_for_new_code_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: an update resubmitting an existing old code block leaves it untouched (no prompt, no
    steps), while a code block new to the workflow gets the new-block prompt default + derived steps."""
    request_mock = _patch_skyvern_http(monkeypatch)
    old_block = _code_block(label="old_block")
    existing_workflow = {
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {"parameters": [], "blocks": [dict(old_block)]},
    }
    _patch_get_workflow_by_id(monkeypatch, AsyncMock(return_value=existing_workflow))

    definition = _workflow_definition([dict(old_block), _code_block(label="new_block")])
    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True, result
    blocks = _sent_definition(request_mock)["workflow_definition"]["blocks"]
    by_label = {block["label"]: block for block in blocks}
    # The update pipeline normalizes blocks through the schema (explicit nulls); the AC is that
    # the old block stays legacy: null prompt, no steps.
    assert by_label["old_block"]["code"] == old_block["code"]
    assert by_label["old_block"].get("prompt") is None
    assert not by_label["old_block"].get("steps")
    assert by_label["new_block"]["prompt"] == ""
    assert [step["action_type"] for step in by_label["new_block"]["steps"]] == ["goto_url"]


_VALUE_PARAMETER = {"parameter_type": "workflow", "key": "value", "workflow_parameter_type": "string"}


@pytest.mark.parametrize(
    ("parameter_keys_field", "parameters"),
    [
        ({"parameter_keys": ["value"]}, [_VALUE_PARAMETER]),
        ({}, []),
    ],
    ids=["explicit_keys", "no_eligible_parameters"],
)
@pytest.mark.parametrize("format_name", ["yaml", "auto"])
@pytest.mark.asyncio
async def test_workflow_create_preserves_yaml_bytes_when_nothing_to_wire(
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
    parameter_keys_field: dict[str, list[str]],
    parameters: list[dict[str, str]],
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    # prompt and steps supplied as the code already derives them: omitted or stale values are
    # defaulted/rebuilt (new code block experience), which intentionally rewrites the definition.
    definition = _workflow_definition(
        [
            _code_block(
                **parameter_keys_field,
                prompt="",
                steps=[
                    {
                        "description": "Open https://example.com",
                        "action_type": "goto_url",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ],
            )
        ],
        proxy_location="RESIDENTIAL",
        code_version=2,
        run_with="agent",
    )
    definition["workflow_definition"]["parameters"] = parameters
    serialized = yaml.safe_dump(definition, sort_keys=False)

    result = await workflow_tools.skyvern_workflow_create(definition=serialized, format=format_name)

    assert result["ok"] is True, result
    assert request_mock.await_args.kwargs["json"]["yaml_definition"] == serialized


@pytest.mark.asyncio
async def test_workflow_update_leaves_code_block_unwired_when_no_parameter_is_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    existing_workflow = {
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {"parameters": [], "blocks": []},
    }
    _patch_get_workflow_by_id(monkeypatch, AsyncMock(return_value=existing_workflow))

    definition = _workflow_definition([_code_block()])
    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True, result
    block = _sent_definition(request_mock)["workflow_definition"]["blocks"][0]
    assert block.get("parameter_keys") is None


def _runtime_code_block(label: str) -> dict[str, object]:
    return _code_block(
        label=label,
        parameters=[],
        output_parameter={"parameter_type": "output", "key": f"{label}_output"},
    )


@pytest.mark.parametrize(
    ("tool", "nested", "serialized_as"),
    [
        ("update", False, "json"),
        ("update", True, "json"),
        ("update", False, "yaml"),
        ("create", False, "json"),
        ("create", False, "yaml"),
    ],
    ids=["update_top_level", "update_loop_blocks", "update_yaml", "create_top_level", "create_yaml"],
)
@pytest.mark.asyncio
async def test_runtime_shaped_code_block_without_parameters_keeps_auto_wire_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    nested: bool,
    serialized_as: str,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    code_block = _runtime_code_block("visit_in_code")
    block: dict[str, object] = (
        {"block_type": "for_loop", "label": "for_each_item", "loop_blocks": [code_block]} if nested else code_block
    )
    definition = _workflow_definition([])
    definition["workflow_definition"] = {"parameters": [dict(_VALUE_PARAMETER)], "blocks": [block]}

    serialized = _serialized_definition(definition, serialized_as)

    if tool == "update":
        _patch_get_workflow_by_id(monkeypatch, AsyncMock(return_value=definition))
        result = await workflow_tools.skyvern_workflow_update(
            workflow_id="wpid_test", definition=serialized, format=serialized_as
        )
    else:
        result = await workflow_tools.skyvern_workflow_create(definition=serialized, format=serialized_as)

    assert result["ok"] is True, result
    sent_blocks = _sent_definition(request_mock)["workflow_definition"]["blocks"]
    sent_block = sent_blocks[0]["loop_blocks"][0] if nested else sent_blocks[0]
    assert sent_block["parameter_keys"] == []


@pytest.mark.asyncio
async def test_workflow_update_keeps_prior_links_on_existing_code_block_and_wires_only_new_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_mock = _patch_skyvern_http(monkeypatch)
    parameters = [
        {"parameter_type": "workflow", "key": key, "workflow_parameter_type": "string"}
        for key in ("report_url", "start", "end", "note")
    ]
    existing_workflow = {
        "proxy_location": "RESIDENTIAL",
        "workflow_definition": {
            "parameters": parameters[:1],
            "blocks": [
                _code_block(label="old_block", parameter_keys=["report_url"]),
                {"block_type": "navigation", "label": "converted", "url": "https://example.com"},
            ],
        },
    }
    _patch_get_workflow_by_id(monkeypatch, AsyncMock(return_value=existing_workflow))

    definition = _workflow_definition(
        [_code_block(label="old_block"), _code_block(label="new_block"), _code_block(label="converted")]
    )
    definition["workflow_definition"]["parameters"] = parameters
    result = await workflow_tools.skyvern_workflow_update(
        workflow_id="wpid_test",
        definition=json.dumps(definition),
        format="json",
    )

    assert result["ok"] is True, result
    by_label = {block["label"]: block for block in _sent_definition(request_mock)["workflow_definition"]["blocks"]}
    assert by_label["old_block"]["parameter_keys"] == ["report_url"]
    assert by_label["new_block"]["parameter_keys"] == ["report_url", "start", "end", "note"]
    assert by_label["converted"]["parameter_keys"] == ["report_url", "start", "end", "note"]
    assert "Re-attached parameter 'report_url' to block 'old_block'" in json.dumps(result)
