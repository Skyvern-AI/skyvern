from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import skyvern.cli.mcp_tools.folder as folder_tools
import skyvern.cli.mcp_tools.workflow as workflow_tools
from skyvern.client import AsyncSkyvern, Folder, FolderCreate, FolderUpdate, Skyvern, UpdateWorkflowFolderRequest
from skyvern.client.errors import BadRequestError
from skyvern.client.raw_client import AsyncRawSkyvern, RawSkyvern


def _fake_folder_response() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        folder_id="fld_test",
        organization_id="org_test",
        title="Important Workflows",
        description="Folder description",
        workflow_count=3,
        created_at=now,
        modified_at=now,
    )


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
        folder_id="fld_test",
        created_at=now,
        modified_at=now,
    )


def test_sdk_exports_folder_types_and_methods() -> None:
    assert Folder.__name__ == "Folder"
    assert FolderCreate.__name__ == "FolderCreate"
    assert FolderUpdate.__name__ == "FolderUpdate"
    assert UpdateWorkflowFolderRequest.__name__ == "UpdateWorkflowFolderRequest"
    assert hasattr(Skyvern, "create_folder")
    assert hasattr(Skyvern, "update_workflow_folder")
    assert hasattr(AsyncSkyvern, "create_folder")
    assert hasattr(AsyncSkyvern, "update_workflow_folder")


def test_raw_client_delete_folder_raises_not_found_on_empty_404() -> None:
    response = SimpleNamespace(
        status_code=404,
        text="",
        headers={},
        json=Mock(side_effect=AssertionError("json() should not be called for empty 404 delete responses")),
    )
    client = RawSkyvern(
        client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=Mock(return_value=response)))
    )

    with pytest.raises(folder_tools.NotFoundError):
        client.delete_folder("fld_missing")


@pytest.mark.asyncio
async def test_async_raw_client_delete_folder_raises_not_found_on_empty_404() -> None:
    response = SimpleNamespace(
        status_code=404,
        text="",
        headers={},
        json=Mock(side_effect=AssertionError("json() should not be called for empty 404 delete responses")),
    )
    client = AsyncRawSkyvern(
        client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=AsyncMock(return_value=response)))
    )

    with pytest.raises(folder_tools.NotFoundError):
        await client.delete_folder("fld_missing")


@pytest.mark.asyncio
async def test_folder_create_calls_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(create_folder=AsyncMock(return_value=_fake_folder_response()))
    monkeypatch.setattr(folder_tools, "get_skyvern", lambda: fake_client)

    result = await folder_tools.skyvern_folder_create("Important Workflows", "Folder description")

    fake_client.create_folder.assert_awaited_once_with(
        title="Important Workflows",
        description="Folder description",
    )
    assert result["ok"] is True
    assert result["data"]["folder_id"] == "fld_test"
    assert result["data"]["title"] == "Important Workflows"


@pytest.mark.asyncio
async def test_folder_delete_requires_force() -> None:
    result = await folder_tools.skyvern_folder_delete("fld_test")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "force=true" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_folder_delete_handles_non_dict_sdk_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(delete_folder=AsyncMock(return_value=["unexpected", "shape"]))
    monkeypatch.setattr(folder_tools, "get_skyvern", lambda: fake_client)

    result = await folder_tools.skyvern_folder_delete("fld_test", force=True)

    assert result["ok"] is True
    assert "sdk_equivalent" in result["data"]


@pytest.mark.asyncio
async def test_workflow_update_folder_calls_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(update_workflow_folder=AsyncMock(return_value=_fake_workflow_response()))
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    result = await workflow_tools.skyvern_workflow_update_folder("wpid_test", "fld_test")

    fake_client.update_workflow_folder.assert_awaited_once_with("wpid_test", folder_id="fld_test")
    assert result["ok"] is True
    assert result["data"]["workflow_permanent_id"] == "wpid_test"
    assert result["data"]["folder_id"] == "fld_test"


@pytest.mark.asyncio
async def test_workflow_update_folder_rejects_invalid_folder_id() -> None:
    result = await workflow_tools.skyvern_workflow_update_folder("wpid_test", "not_a_folder")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "Folder IDs start with fld_" in result["error"]["hint"]


@pytest.mark.asyncio
async def test_workflow_update_folder_surfaces_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(
        update_workflow_folder=AsyncMock(side_effect=BadRequestError(body={"detail": "Folder fld_missing not found"}))
    )
    monkeypatch.setattr(workflow_tools, "get_skyvern", lambda: fake_client)

    result = await workflow_tools.skyvern_workflow_update_folder("wpid_test", "fld_missing")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "skyvern_folder_list" in result["error"]["hint"]
