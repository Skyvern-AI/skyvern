from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.output_utils import format_tool_result_for_user
from skyvern.forge.sdk.copilot.tools import (
    BROWSER_BOUND_TOOL_NAMES,
    copilot_native_tools,
    get_organization_usage_quota_tool,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from tests.unit.conftest import make_copilot_context

TOOL_NAME = "get_organization_usage_quota"


@pytest.mark.asyncio
async def test_a_deployment_without_billing_reports_every_field_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    ctx = make_copilot_context()

    raw = await get_organization_usage_quota_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name=TOOL_NAME),
        "{}",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    data = payload["data"]
    assert data["credits_note"]
    assert data["billing_page_path"] is None
    assert [value for key, value in data.items() if key != "credits_note"] == [None] * (len(data) - 1)
    assert format_tool_result_for_user(TOOL_NAME, payload) == ""


def test_the_tool_takes_no_arguments_and_needs_no_browser() -> None:
    schema = get_organization_usage_quota_tool.params_json_schema

    assert schema.get("properties", {}) == {}
    assert schema.get("required", []) == []
    assert schema["additionalProperties"] is False
    assert TOOL_NAME not in BROWSER_BOUND_TOOL_NAMES


# Frozen on purpose: the previous ordered catalog is the only thing that witnesses "the existing
# tool surface is unchanged" when a sibling is dropped, renamed or reordered.
CATALOG_WITHOUT_THIS_TOOL = [
    "ask_user",
    "set_work_plan",
    "update_workflow",
    "edit_block",
    "edit_block_and_run",
    "add_block",
    "delete_block",
    "list_credentials",
    "list_integrations",
    "run_blocks_and_collect_debug",
    "test_workflow_from_blank_browser",
    "get_run_results",
    "update_and_run_blocks",
    "discover_workflow_entrypoint",
    "search_web",
    "inspect_page_for_composition",
    "inspect_locator_matches",
    "fill_credential_field",
    "request_credential",
    "run_browser_code",
]


@pytest.mark.parametrize("supports_question_tool", [True, False])
@pytest.mark.parametrize("browser_code_available", [True, False])
def test_the_tool_joins_every_catalog_combination_without_displacing_one(
    supports_question_tool: bool,
    browser_code_available: bool,
) -> None:
    names = [
        tool.name
        for tool in copilot_native_tools(
            supports_question_tool=supports_question_tool,
            browser_code_available=browser_code_available,
        )
    ]

    assert names.count(TOOL_NAME) == 1
    expected = [
        name
        for name in CATALOG_WITHOUT_THIS_TOOL
        if (name != "ask_user" or supports_question_tool) and (name != "run_browser_code" or browser_code_available)
    ]
    assert [name for name in names if name != TOOL_NAME] == expected


@pytest.mark.asyncio
async def test_a_self_heal_turn_cannot_read_account_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    ctx = make_copilot_context()
    ctx.turn_origin = TurnOrigin.runtime_self_heal

    payload = json.loads(
        await get_organization_usage_quota_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name=TOOL_NAME),
            "{}",
        )
    )

    assert payload["ok"] is False
    assert payload["error"]
