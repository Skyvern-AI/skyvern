from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _type_text_pre_hook
from skyvern.forge.sdk.copilot.typed_value_policy import safe_typed_default_value, should_reject_type_text_value


@pytest.mark.asyncio
async def test_type_text_policy_rejects_sensitive_targets_and_stashes_safe_defaults() -> None:
    ctx = SimpleNamespace(
        organization_id="o",
        browser_session_id=None,
        pending_scout_source_url=None,
        pending_scout_typed_value=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
    )

    rejected = await _type_text_pre_hook({"selector": "input[type=password]", "text": "hunter2"}, ctx)
    assert rejected is not None
    assert rejected["ok"] is False
    assert "hunter2" not in rejected["error"]
    assert ctx.pending_scout_typed_value is None

    allowed = await _type_text_pre_hook({"selector": "#search", "text": "example_sku_123"}, ctx)
    assert allowed is None
    assert ctx.pending_scout_typed_value == "example_sku_123"


@pytest.mark.asyncio
async def test_type_text_pre_hook_clears_stashed_default_only_on_guard_short_circuit() -> None:
    ctx = SimpleNamespace(
        organization_id="o",
        browser_session_id=None,
        pending_scout_source_url=None,
        pending_scout_typed_value=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        workflow_persisted=True,
        last_full_workflow_test_ok=True,
    )

    params = {"selector": "#search", "intent": "search catalog", "text": "example_sku_123"}
    allowed = await _type_text_pre_hook(params, ctx)

    assert allowed is None
    assert params["intent"] is None
    assert ctx.pending_scout_typed_value == "example_sku_123"

    rejected = await _type_text_pre_hook({"intent": "search catalog", "text": "example_sku_123"}, ctx)

    assert rejected is not None
    assert rejected["ok"] is False
    assert ctx.pending_scout_typed_value is None


def test_safe_typed_default_policy_allows_search_tokens_not_pii_like_values() -> None:
    def default(value: str, selector: str, name: str) -> str | None:
        return safe_typed_default_value(value, selector=selector, role="textbox", accessible_name=name)

    assert default("example_sku_123", "#search", "Search") == "example_sku_123"
    assert default("Jane Doe", "#search", "Search") is None
    assert default("jane@example.com", "#search", "Search") is None
    assert default("example_sku_123", "#email", "Email") is None


def test_type_text_rejection_allows_benign_secret_word_search_phrases() -> None:
    assert should_reject_type_text_value(value="password manager", selector="#search", intent="search catalog") is False
    assert should_reject_type_text_value(value="2fa setup", selector="#search", intent="search help") is False
    assert should_reject_type_text_value(value="password", selector="#search", intent="search catalog") is True
    assert should_reject_type_text_value(value="password manager", selector="input[type=password]", intent="") is True


@pytest.mark.asyncio
async def test_type_text_refuses_a_value_already_registered_as_a_credential() -> None:
    """A real password need not look like one, and this one reached a plaintext username field.
    The value is known to be secret because a credential fill registered it."""
    ctx = SimpleNamespace(
        organization_id="o",
        browser_session_id=None,
        pending_scout_source_url=None,
        pending_scout_typed_value=None,
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        secret_scrub_values=["Tuesday Harbor 41"],
    )

    rejected = await _type_text_pre_hook({"selector": "#username", "text": "Tuesday Harbor 41"}, ctx)

    assert rejected is not None
    assert rejected["ok"] is False
    assert "Tuesday Harbor 41" not in rejected["error"]
    assert ctx.pending_scout_typed_value is None


@pytest.mark.asyncio
async def test_code_block_schema_carries_the_steps_already_demonstrated() -> None:
    """The synthesizer that checks a draft against the demonstrated steps also answers what to
    write, so the block is authored from the trajectory instead of corrected against it."""
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        reached_download_target=None,
        scout_trajectory=[
            {"tool_name": "click", "selector": 'button[aria-label="Log in"]', "source_url": "https://example.com/a"}
        ],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    demonstrated = result["data"]["demonstrated_steps"]
    assert "aria-label=" in demonstrated and "Log in" in demonstrated
    assert ".click()" in demonstrated


@pytest.mark.asyncio
async def test_demonstrated_steps_match_the_synthesis_the_draft_is_judged_against() -> None:
    """The payload is only worth reading if it is the source the judge compares the draft to.
    Synthesized with different arguments it is a laxer variant, and authoring from it reproduces
    the correction round-trip this exists to remove."""
    from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    trajectory = [
        {"tool_name": "click", "selector": 'button[aria-label="Log in"]', "source_url": "https://example.com/a"},
        {"tool_name": "click", "selector": "button", "source_url": "https://example.com/a"},
        {"tool_name": "press_key", "key": "Enter", "source_url": "https://example.com/a"},
    ]
    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        reached_download_target=None,
        scout_trajectory=list(trajectory),
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    judged = synthesize_code_block(list(trajectory), strict_selectors=True, reached_download_target=None)
    assert judged is not None
    assert result["data"]["demonstrated_steps"] == judged.code


@pytest.mark.asyncio
async def test_code_block_schema_omits_demonstrated_steps_before_anything_is_demonstrated() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

    ctx = SimpleNamespace(
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        code_only_code_schema_seen=False,
        reached_download_target=None,
        scout_trajectory=[],
    )

    result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

    assert "demonstrated_steps" not in result["data"]
