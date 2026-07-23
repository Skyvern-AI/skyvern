from __future__ import annotations

import inspect

import pytest

import skyvern.cli.mcp_tools._common as mcp_common
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.blocks import skyvern_block_schema
from skyvern.cli.mcp_tools.inspection import skyvern_get_html
from skyvern.cli.mcp_tools.prompts import BUILD_WORKFLOW_CONTENT, EXTRACT_DATA_CONTENT
from skyvern.cli.mcp_tools.session import skyvern_browser_session_create, skyvern_browser_session_list
from skyvern.cli.mcp_tools.workflow import (
    skyvern_workflow_create,
    skyvern_workflow_get,
    skyvern_workflow_retry,
    skyvern_workflow_run,
    skyvern_workflow_update,
)


def test_build_workflow_prompt_guides_text_prompt_defaults() -> None:
    assert "Default to Skyvern Optimized for text_prompt blocks by omitting both `model` and `llm_key`." in (
        BUILD_WORKFLOW_CONTENT
    )
    assert "Do NOT invent internal `llm_key` strings like `ANTHROPIC_CLAUDE_3_5_SONNET`." in BUILD_WORKFLOW_CONTENT


def test_mcp_instructions_guide_text_prompt_defaults() -> None:
    # text_prompt guidance moved to build_workflow prompt; instructions now focus on task classification.
    # Verify the classification table (the core of the rewritten instructions) is present.
    assert "Task Classification" in mcp.instructions
    assert "If a browser session is already open, keep using it." in mcp.instructions
    assert '"try this once"' in mcp.instructions
    assert "skyvern_run_task" in mcp.instructions
    assert "skyvern_workflow_create" in mcp.instructions
    assert "skyvern_validate" in mcp.instructions
    assert "skyvern_extract" in mcp.instructions
    assert "skyvern_act" in mcp.instructions
    assert "skyvern_observe" in mcp.instructions


def test_workflow_create_guides_code_only_policy() -> None:
    assert mcp_common.CODE_ONLY_SCHEMA_GUIDANCE in (skyvern_workflow_create.__doc__ or "")


def test_mcp_instructions_guide_code_only_policy() -> None:
    assert mcp_common.CODE_ONLY_SCHEMA_GUIDANCE in mcp.instructions


def test_workflow_prompts_guide_code_only_policy_at_each_authoring_surface() -> None:
    assert BUILD_WORKFLOW_CONTENT.count(mcp_common.CODE_ONLY_SCHEMA_GUIDANCE) == 2
    assert EXTRACT_DATA_CONTENT.count(mcp_common.CODE_ONLY_SCHEMA_GUIDANCE) == 1


@pytest.mark.asyncio
async def test_expected_prompts_registered() -> None:
    prompts = await mcp.list_prompts()
    prompt_names = {prompt.name for prompt in prompts}
    # Deliberately additive-only: this guards that core prompts remain
    # registered without breaking when new prompts are introduced later.
    assert {"build_workflow", "debug_automation", "extract_data", "qa_test"} <= prompt_names


@pytest.mark.asyncio
async def test_text_prompt_block_schema_example_omits_raw_llm_key() -> None:
    result = await skyvern_block_schema(block_type="text_prompt")

    assert result["ok"] is True
    assert "llm_key" not in result["data"]["example"]
    assert "model" not in result["data"]["example"]


# --- Tool-routing hints in tool descriptions (mechanism (c): wrong-tool / missing-arg calls) ---
# These guard the cross-references that steer an MCP client to the right tool. Tool names are stable
# API identifiers, so asserting they appear in the routing docstrings is a contract check, not prose.


def test_workflow_get_routes_search_and_browse_to_workflow_list() -> None:
    """get fetches ONE workflow by known id; search/browse/paginate belong on workflow_list (SKY-12087/89/90/91)."""
    doc = skyvern_workflow_get.__doc__ or ""
    assert "skyvern_workflow_list" in doc
    assert "skyvern workflow get --id <wpid> --definition-file wf.json" in doc
    assert "skyvern workflow update --id <wpid> --definition @wf.json" in doc
    params = inspect.signature(skyvern_workflow_get).parameters
    assert params["workflow_id"].default is inspect.Parameter.empty


def test_workflow_create_routes_list_intent_and_keeps_definition() -> None:
    """create builds from a serialized `definition`; list intent (only_workflows) belongs on workflow_list
    (SKY-12088), and the whole workflow (incl. title) serializes INTO `definition` (SKY-12072/12107/12108/12109)."""
    doc = skyvern_workflow_create.__doc__ or ""
    assert "skyvern_workflow_list" in doc
    assert "definition" in doc


def test_workflow_update_keeps_serialized_definition() -> None:
    """update takes the whole workflow serialized into `definition`; flat fields are rejected (SKY-12072)."""
    doc = skyvern_workflow_update.__doc__ or ""
    assert "definition" in doc
    assert "skyvern workflow get --id <wpid> --definition-file wf.json" in doc
    assert "skyvern workflow update --id <wpid> --definition @wf.json" in doc
    params = inspect.signature(skyvern_workflow_update).parameters
    assert params["definition"].default is inspect.Parameter.empty


def test_workflow_run_routes_retry_intent_to_workflow_retry() -> None:
    """run starts a NEW run (needs workflow_id); a workflow_run_id means retry an existing run (SKY-12051)."""
    doc = skyvern_workflow_run.__doc__ or ""
    assert "skyvern_workflow_retry" in doc
    params = inspect.signature(skyvern_workflow_run).parameters
    assert params["workflow_id"].default is inspect.Parameter.empty
    assert "workflow_run_id" not in params


def test_workflow_retry_cross_refs_workflow_run() -> None:
    doc = skyvern_workflow_retry.__doc__ or ""
    assert "skyvern_workflow_run" in doc


def test_browser_session_create_makes_session_and_routes_url_and_steps() -> None:
    """create MAKES a session; a session_id/url/steps/selector means act on an EXISTING session via
    navigate/execute instead (SKY-12092/12093/12095/12103)."""
    doc = skyvern_browser_session_create.__doc__ or ""
    assert "skyvern_navigate" in doc
    assert "skyvern_execute" in doc
    params = set(inspect.signature(skyvern_browser_session_create).parameters)
    assert not ({"session_id", "url", "steps", "selector"} & params)


def test_browser_session_list_takes_no_pagination() -> None:
    """session_list returns ALL sessions in one call; it has no page/page_size (SKY-12110)."""
    params = set(inspect.signature(skyvern_browser_session_list).parameters)
    assert not ({"page", "page_size"} & params)


def test_get_html_reads_current_page_by_selector_not_by_url() -> None:
    """get_html reads an element by selector on the CURRENT page; no HTML-by-URL — navigate first (SKY-12104)."""
    doc = skyvern_get_html.__doc__ or ""
    assert "skyvern_navigate" in doc
    params = set(inspect.signature(skyvern_get_html).parameters)
    assert "selector" in params
    assert "url" not in params
