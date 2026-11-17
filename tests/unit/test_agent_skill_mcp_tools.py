from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.cli.mcp_tools import agent_skill as tools

SKILL: dict[str, Any] = {
    "name": "portal-login",
    "description": "Sign in to the portal",
    "workflow_permanent_id": None,
    "version": 2,
    "author_kind": "curator",
    "author_id": "curator",
    "created_at": "2026-11-15T00:00:00",
    "source_run_ids": ["wr_2"],
    "files": {"SKILL.md": "# Portal login\n", "notes/steps.md": "Step one\n"},
    "files_withheld": False,
    "history": [
        {"version": 2, "author_kind": "curator", "author_id": "curator", "source_run_ids": ["wr_2"]},
        {"version": 1, "author_kind": "person", "author_id": "u_1", "source_run_ids": None},
    ],
    "history_truncated": False,
}


def _serve(monkeypatch: pytest.MonkeyPatch, status_code: int, body: dict[str, Any] | None = None) -> AsyncMock:
    response = SimpleNamespace(status_code=status_code, text="", json=lambda: body)
    request = AsyncMock(return_value=response)
    client = SimpleNamespace(_client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request)))
    monkeypatch.setattr("skyvern.cli.mcp_tools._session.get_skyvern", lambda: client)
    return request


@pytest.mark.asyncio
async def test_a_404_names_every_cause_the_call_cannot_tell_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, 404)

    unscoped = await tools.skyvern_agent_skill_list()
    scoped = await tools.skyvern_agent_skill_list(workflow_permanent_id="wpid_1")
    read = await tools.skyvern_agent_skill_get("portal-login", workflow_permanent_id="wpid_1")
    deleted = await tools.skyvern_agent_skill_delete("portal-login")

    not_available = "agent skills are not available on this Skyvern server"
    assert unscoped["error"]["message"] == "Agent skills are not available on this Skyvern server."
    assert "wpid_1" in scoped["error"]["message"] and not_available in scoped["error"]["message"]
    assert "skyvern_workflow_list" in scoped["error"]["hint"]
    for result in (read, deleted):
        assert "portal-login" in result["error"]["message"] and not_available in result["error"]["message"]
    assert "wpid_1" in read["error"]["message"]


@pytest.mark.asyncio
async def test_get_returns_one_file_with_the_file_list_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, 200, {**SKILL, "files": dict(SKILL["files"])})

    result = await tools.skyvern_agent_skill_get("portal-login", path="notes/steps.md")

    data = result["data"]
    assert data["file"] == {"path": "notes/steps.md", "text": "Step one\n"}
    assert data["file_paths"] == ["SKILL.md", "notes/steps.md"]
    assert "files" not in data
    assert data["source_run_ids"] == ["wr_2"]
    assert [(entry["version"], entry["source_run_ids"]) for entry in data["history"]] == [(2, ["wr_2"]), (1, None)]


@pytest.mark.asyncio
async def test_get_of_a_missing_file_lists_the_files_there_are(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, 200, {**SKILL, "files": dict(SKILL["files"])})

    result = await tools.skyvern_agent_skill_get("portal-login", path="missing.md")

    assert result["ok"] is False
    assert "SKILL.md, notes/steps.md" in result["error"]["message"]


@pytest.mark.asyncio
async def test_get_of_withheld_curator_text_returns_metadata_without_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, 200, {**SKILL, "files": None, "files_withheld": True})

    result = await tools.skyvern_agent_skill_get("portal-login")

    assert result["ok"] is True
    assert (result["data"]["file"], result["data"]["file_paths"], result["data"]["files_withheld"]) == (
        None,
        None,
        True,
    )
    assert result["data"]["source_run_ids"] == ["wr_2"]


# An empty id must reach the route (which answers 404); dropping it would delete the org-wide skill instead.
@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_permanent_id", ["wpid_1", ""])
async def test_delete_addresses_the_workflow_skill_by_query(
    monkeypatch: pytest.MonkeyPatch, workflow_permanent_id: str
) -> None:
    request = _serve(monkeypatch, 204)

    result = await tools.skyvern_agent_skill_delete("portal-login", workflow_permanent_id=workflow_permanent_id)

    assert result["data"] == {"deleted": "portal-login", "workflow_permanent_id": workflow_permanent_id}
    assert request.await_args is not None
    assert request.await_args.kwargs["method"] == "DELETE"
    assert request.await_args.kwargs["params"] == {"workflow_permanent_id": workflow_permanent_id}
