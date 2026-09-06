from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _type_text_pre_hook


@pytest.mark.asyncio
async def test_type_text_uses_exact_registered_secret_fact_and_stashes_ordinary_value_privately() -> None:
    ctx = SimpleNamespace(
        organization_id="o",
        browser_session_id=None,
        last_run_blocks_workflow_run_id=None,
        pending_scout_source_url=None,
        pending_scout_input_value=None,
        discovery_mcp_server=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        secret_scrub_values=["known-vault-value"],
    )

    rejected = await _type_text_pre_hook({"selector": "#user", "text": "known-vault-value"}, ctx)
    assert rejected is not None
    assert rejected["ok"] is False
    assert "known-vault-value" not in rejected["error"]
    assert "exact value already registered as a secret" in rejected["error"]
    assert "password-like" not in rejected["error"]
    assert "OTP/TOTP" not in rejected["error"]
    assert ctx.pending_scout_input_value is None

    allowed = await _type_text_pre_hook({"selector": "#search", "text": "any ordinary value"}, ctx)
    assert allowed is None
    assert ctx.pending_scout_input_value == "any ordinary value"


@pytest.mark.asyncio
async def test_type_text_pre_hook_does_not_infer_secret_status_from_text_selector_or_intent() -> None:
    ctx = SimpleNamespace(
        organization_id="o",
        browser_session_id=None,
        last_run_blocks_workflow_run_id=None,
        pending_scout_source_url=None,
        pending_scout_input_value=None,
        discovery_mcp_server=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        workflow_persisted=True,
        last_full_workflow_test_ok=True,
    )

    params = {"selector": "#search", "intent": "search catalog", "text": "example_sku_123"}
    allowed = await _type_text_pre_hook(params, ctx)

    assert allowed is None
    assert params["intent"] == "search catalog"
    assert ctx.pending_scout_input_value == "example_sku_123"

    selectorless = await _type_text_pre_hook({"intent": "search catalog", "text": "example_sku_123"}, ctx)

    assert selectorless is None
    assert ctx.pending_scout_input_value == "example_sku_123"

    # These strings are not registered secret facts. Copilot must not infer a
    # credential policy from prose-like values, selectors, or intent text.
    password_word = await _type_text_pre_hook(
        {"selector": "#search", "intent": "search catalog", "text": "password"}, ctx
    )
    password_shaped_selector = await _type_text_pre_hook(
        {"selector": "input[type=password]", "intent": "enter supplied text", "text": "hunter2"}, ctx
    )

    assert password_word is None
    assert password_shaped_selector is None
    assert ctx.pending_scout_input_value == "hunter2"


@pytest.mark.asyncio
async def test_code_block_schema_carries_the_steps_already_demonstrated() -> None:
    """The code schema gives the model ordered observations, not generated browser source."""
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        scout_trajectory=[
            {"tool_name": "click", "selector": 'button[aria-label="Log in"]', "source_url": "https://example.com/a"}
        ],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    demonstrated = result["data"]["demonstrated_steps"]
    assert demonstrated[0]["tool_name"] == "click"
    assert demonstrated[0]["executed_selector"] == 'button[aria-label="Log in"]'
    assert demonstrated[0]["source_url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_demonstrated_steps_preserve_trajectory_order_without_synthesizing_source() -> None:
    """Repeated and ambiguous observations remain model-owned facts in encounter order."""
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    trajectory = [
        {"tool_name": "click", "selector": 'button[aria-label="Log in"]', "source_url": "https://example.com/a"},
        {"tool_name": "click", "selector": "button", "source_url": "https://example.com/a"},
        {"tool_name": "press_key", "key": "Enter", "source_url": "https://example.com/a"},
    ]
    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        scout_trajectory=list(trajectory),
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    demonstrated = result["data"]["demonstrated_steps"]
    assert [step["tool_name"] for step in demonstrated] == ["click", "click", "press_key"]
    assert [step.get("executed_selector") for step in demonstrated] == [
        'button[aria-label="Log in"]',
        "button",
        None,
    ]


@pytest.mark.asyncio
async def test_code_block_schema_exposes_opaque_input_id_but_never_private_value() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        scout_trajectory=[
            {
                "tool_name": "type_text",
                "selector": "#search",
                "input_id": "input_opaque_1",
                "input_value": "private run value",
                "typed_length": 17,
            }
        ],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    assert result["data"]["demonstrated_steps"] == [
        {
            "tool_name": "type_text",
            "executed_selector": "#search",
            "input_id": "input_opaque_1",
            "typed_length": 17,
            "selector_candidates": None,
            "role": None,
            "accessible_name": None,
            "role_name_match_count": None,
            "source_url": None,
            "result_url": None,
            "observed_effects": None,
            "observation_step": None,
        }
    ]
    assert "private run value" not in repr(result)


@pytest.mark.asyncio
async def test_code_block_schema_omits_demonstrated_steps_before_anything_is_demonstrated() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        scout_trajectory=[],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    assert "demonstrated_steps" not in result["data"]


@pytest.mark.asyncio
async def test_code_block_schema_exposes_download_claim_helper_before_scouting() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        reached_download_target=None,
        scout_trajectory=[],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    assert "code_block_runtime_helpers" not in result["data"]
    helper = result["data"]["download_claim_helper_contract"]["click_and_claim_download"]
    assert helper == {
        "call": "await click_and_claim_download(page, selector)",
        "parameters": {
            "page": {"accepted_type": "current_code_block_page"},
            "selector": {"accepted_types": ["selector_string"]},
        },
        "returns": {"type": "string", "value": "sanitized_suggested_filename"},
    }
    assert ctx.reached_download_target is None
    assert ctx.scout_trajectory == []


@pytest.mark.asyncio
async def test_download_claim_helper_contract_is_scoped_to_code_only_code_schema() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    standard_ctx = SimpleNamespace(block_authoring_policy=BlockAuthoringPolicy.STANDARD)
    standard = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, standard_ctx)
    code_only_ctx = SimpleNamespace(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    non_code = await _get_block_schema_post_hook({"data": {"block_type": "conditional"}}, {}, code_only_ctx)

    assert "download_claim_helper_contract" not in standard["data"]
    assert "download_claim_helper_contract" not in non_code["data"]


@pytest.mark.asyncio
async def test_oss_code_only_code_schema_omits_cloud_page_operation_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import app
    from skyvern.forge.agent_functions import AgentFunction
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        scout_trajectory=[],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    assert "page_operation_contracts" not in result["data"]


def test_code_only_evaluate_guidance_supports_grounded_download_authoring() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_overlay_description

    description = _evaluate_overlay_description(BlockAuthoringPolicy.CODE_ONLY_BROWSER)

    assert "rather than authoring the download yourself" not in description
    assert "capture a stable selector" in description
    assert "author the terminal download step from the code-block schema contract" in description
