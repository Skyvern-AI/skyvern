from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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


def _code_block() -> dict[str, object]:
    return {
        "block_type": "code",
        "label": "visit_in_code",
        "code": 'await page.goto("https://example.com")\nreturn {"ok": True}',
    }


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


def _assert_code_only_rejection(result: dict[str, object], *, label: str) -> None:
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == workflow_tools.ErrorCode.INVALID_INPUT
    assert "not allowed in code-only mode" in str(error["message"])
    assert label in str(error["message"])
    assert "use a `code` block" in str(error["hint"])


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
