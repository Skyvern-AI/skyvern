"""Live MCP server tests for script/caching tools.

Tests call tools through the actual FastMCP Client, exactly as Claude Code would.
API responses are mocked at the HTTP layer so we test the full MCP pipeline:
  Client → FastMCP → tool function → raw_http_get/SDK → (mocked) API
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

import skyvern.cli.mcp_tools.scripts as script_tools
from skyvern.cli.mcp_tools import mcp
from skyvern.client.types import ScriptFileCreate
from tests.unit._mcp_test_helpers import patch_skyvern_client as _patch_skyvern_client

# ---------------------------------------------------------------------------
# Fake API payloads
# ---------------------------------------------------------------------------

FAKE_SCRIPTS = {
    "scripts": [
        {
            "script_id": "s_abc",
            "cache_key": "hash",
            "cache_key_value": "default",
            "status": "published",
            "latest_version": 2,
            "version_count": 2,
            "total_runs": 5,
            "success_rate": 0.8,
            "is_pinned": False,
        }
    ]
}

FAKE_CODE = {
    "blocks": {
        "fill_form": "async def fill_form(page, ctx):\n    await page.fill('xpath=//input', ctx.parameters['name'])\n",
    },
    "main_script": "import skyvern\n\n@skyvern.workflow(title='Test')\nasync def run(params):\n    pass\n",
    "script_id": "s_abc",
    "version": 2,
}

FAKE_VERSIONS = {
    "versions": [
        {"version": 1, "script_revision_id": "srev_1", "created_at": "2026-03-20T10:00:00Z", "run_id": "wr_001"},
        {"version": 2, "script_revision_id": "srev_2", "created_at": "2026-03-22T14:00:00Z", "run_id": "wr_002"},
    ]
}

FAKE_EPISODES = {
    "episodes": [
        {
            "episode_id": "ep_1",
            "block_label": "fill_form",
            "fallback_type": "selector_miss",
            "error_message": "Element not found: site redesigned",
            "classify_result": None,
            "fallback_succeeded": True,
            "workflow_run_id": "wr_002",
            "page_url": "https://example.com/form",
            "reviewed": True,
            "created_at": "2026-03-22T14:01:00Z",
        }
    ],
    "total_count": 1,
    "page": 1,
    "page_size": 20,
}


def _mock_raw_http(responses: dict):
    """Return a mock raw_http_get that routes by path substring."""

    async def mock_get(path, params=None):
        for key, val in responses.items():
            if key in path:
                return val
        raise RuntimeError(f"Unmocked path: {path}")

    return mock_get


# ---------------------------------------------------------------------------
# Scenario 1: "Show me the scripts for this workflow"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_scripts_via_mcp(monkeypatch):
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "scripts/workflows/": FAKE_SCRIPTS,
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_list_for_workflow",
            {
                "workflow_id": "wpid_test",
            },
        )

    assert result.data["ok"] is True
    scripts = result.data["data"]["scripts"]
    assert len(scripts) == 1
    assert scripts[0]["script_id"] == "s_abc"
    assert scripts[0]["success_rate"] == 0.8
    assert scripts[0]["version"] == 2


@pytest.mark.parametrize(
    ("payload", "expected_scripts"),
    [
        ({"scripts": None}, None),
        ({"scripts": {"unexpected": "shape"}}, {"unexpected": "shape"}),
    ],
)
@pytest.mark.asyncio
async def test_list_scripts_handles_missing_script_list_via_mcp(monkeypatch, payload, expected_scripts):
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "scripts/workflows/": payload,
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_list_for_workflow",
            {
                "workflow_id": "wpid_test",
            },
        )

    assert result.data["ok"] is True
    assert result.data["data"]["scripts"] == expected_scripts
    assert result.data["data"]["count"] == 0


# ---------------------------------------------------------------------------
# Scenario 2: "Print the script that was made"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_script_code_via_mcp(monkeypatch):
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "scripts/s_abc/versions/2": FAKE_CODE,
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_get_code",
            {
                "script_id": "s_abc",
                "version": 2,
            },
        )

    assert result.data["ok"] is True
    data = result.data["data"]
    assert "fill_form" in data["blocks"]
    # Semgrep false positive: this checks a script code path, not a user-supplied URL.
    assert "page.fill" in data["blocks"]["fill_form"]  # nosemgrep: incomplete-url-substring-sanitization
    assert "@skyvern.workflow" in data["main_script"]


@pytest.mark.asyncio
async def test_get_script_code_resolves_latest_via_mcp(monkeypatch):
    """When version is omitted, tool fetches metadata first to find latest."""
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "v1/scripts/s_abc/versions/2": FAKE_CODE,
                "v1/scripts/s_abc": {"script_id": "s_abc", "version": 2},
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_get_code",
            {
                "script_id": "s_abc",
            },
        )

    assert result.data["ok"] is True
    assert result.data["data"]["version"] == 2


# ---------------------------------------------------------------------------
# Scenario 3: "How did the script evolve?"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_versions_via_mcp(monkeypatch):
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "versions": FAKE_VERSIONS,
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_versions",
            {
                "script_id": "s_abc",
            },
        )

    assert result.data["ok"] is True
    versions = result.data["data"]["versions"]
    assert len(versions) == 2
    assert versions[0]["version"] == 1
    assert versions[1]["version"] == 2


# ---------------------------------------------------------------------------
# Scenario 4: "Why did it fall back to AI?"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_episodes_via_mcp(monkeypatch):
    monkeypatch.setattr(
        script_tools,
        "raw_http_get",
        _mock_raw_http(
            {
                "fallback-episodes": FAKE_EPISODES,
            }
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_fallback_episodes",
            {
                "workflow_id": "wpid_test",
            },
        )

    assert result.data["ok"] is True
    data = result.data["data"]
    assert data["total_count"] == 1
    ep = data["episodes"][0]
    assert ep["fallback_type"] == "selector_miss"
    assert "site redesigned" in ep["error_message"]
    assert ep["fallback_succeeded"] is True


@pytest.mark.asyncio
async def test_fallback_episodes_rejects_invalid_workflow_run_id_via_mcp(monkeypatch):
    raw_http_get = AsyncMock(return_value=FAKE_EPISODES)
    monkeypatch.setattr(script_tools, "raw_http_get", raw_http_get)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_fallback_episodes",
            {
                "workflow_id": "wpid_test",
                "workflow_run_id": "bad_run_id",
            },
        )

    assert result.data["ok"] is False
    assert result.data["error"]["code"] == script_tools.ErrorCode.INVALID_INPUT
    raw_http_get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 5: "Edit the script"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_script_via_mcp(monkeypatch):
    deploy_resp = SimpleNamespace(
        script_id="s_abc",
        version=3,
        script_revision_id="srev_3",
        model_dump=lambda mode="python": {"script_id": "s_abc", "version": 3, "script_revision_id": "srev_3"},
    )
    fake_client = SimpleNamespace(deploy_script=AsyncMock(return_value=deploy_resp))
    monkeypatch.setattr(script_tools, "get_skyvern", lambda: fake_client)

    import base64

    files = json.dumps([{"path": "main.py", "content": base64.b64encode(b"# edited").decode(), "encoding": "base64"}])

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_deploy",
            {
                "script_id": "s_abc",
                "files": files,
            },
        )

    assert result.data["ok"] is True
    assert result.data["data"]["version"] == 3
    fake_client.deploy_script.assert_awaited_once()
    called_files = fake_client.deploy_script.await_args.kwargs["files"]
    assert len(called_files) == 1
    assert isinstance(called_files[0], ScriptFileCreate)
    assert called_files[0].path == "main.py"


# ---------------------------------------------------------------------------
# Scenario 6: Workflow create shows caching defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_create_surfaces_caching_fields_via_mcp(monkeypatch):
    payload = {
        "workflow_permanent_id": "wpid_new",
        "workflow_id": "wf_1",
        "title": "Test",
        "version": 1,
        "status": "published",
        "description": None,
        "is_saved_task": False,
        "folder_id": None,
        "created_at": "2026-04-23T10:00:00+00:00",
        "modified_at": "2026-04-23T10:00:00+00:00",
        "code_version": 2,
        "adaptive_caching": True,
        "run_with": "code",
    }
    response = SimpleNamespace(status_code=200, json=lambda: payload, text="")
    request_mock = AsyncMock(return_value=response)
    fake_client = SimpleNamespace(_client_wrapper=SimpleNamespace(httpx_client=SimpleNamespace(request=request_mock)))
    _patch_skyvern_client(monkeypatch, fake_client)

    definition = json.dumps(
        {
            "title": "Test",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {
                        "block_type": "navigation",
                        "label": "s1",
                        "url": "https://example.com",
                        "navigation_goal": "Click",
                    }
                ],
            },
        }
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_workflow_create",
            {
                "definition": definition,
                "format": "json",
            },
        )

    assert result.data["ok"] is True
    data = result.data["data"]
    assert data["code_version"] == 2
    assert data["run_with"] == "code"
    assert data["adaptive_caching"] is True


# ---------------------------------------------------------------------------
# Scenario 7: Run status shows script_run + ai_fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_status_shows_script_run_via_mcp(monkeypatch):
    payload = {
        "workflow_run_id": "wr_test",
        "status": "completed",
        "run_with": "code",
        "workflow_title": "Test",
        "script_run": {"ai_fallback_triggered": True, "script_id": "s_abc"},
        "outputs": {"result": "ok"},
    }
    fake_resp = SimpleNamespace(status_code=200, json=lambda: payload, text="")
    fake_client = SimpleNamespace(
        _client_wrapper=SimpleNamespace(
            httpx_client=SimpleNamespace(request=AsyncMock(return_value=fake_resp)),
        ),
    )
    _patch_skyvern_client(monkeypatch, fake_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_workflow_status",
            {
                "run_id": "wr_test",
                "verbosity": "full",
            },
        )

    assert result.data["ok"] is True
    data = result.data["data"]
    assert data["run_with"] == "code"
    assert data["script_run"]["ai_fallback_triggered"] is True


# ---------------------------------------------------------------------------
# Validation: bad inputs get clear errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_workflow_id_returns_error_via_mcp():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_list_for_workflow",
            {
                "workflow_id": "not_a_wpid",
            },
        )

    assert result.data["ok"] is False
    assert "wpid_" in str(result.data["error"])


@pytest.mark.asyncio
async def test_bad_script_id_returns_error_via_mcp():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_get_code",
            {
                "script_id": "wrong_prefix",
            },
        )

    assert result.data["ok"] is False
    assert "s_" in str(result.data["error"])


@pytest.mark.asyncio
async def test_bad_deploy_json_returns_error_via_mcp():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "skyvern_script_deploy",
            {
                "script_id": "s_abc",
                "files": "not json",
            },
        )

    assert result.data["ok"] is False
    assert "JSON" in result.data["error"]["message"]
