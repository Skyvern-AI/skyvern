"""The trust floor must not report a sign-in fact the production build path cannot supply."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, build_request_policy_trust_floor
from tests.unit.copilot_test_helpers import make_copilot_ctx

SIGNIN_TRACE_KEYS = {"login_intent", "email_signin_intent", "signin_email_resolved"}


async def _clean_safety_handler(**_: object) -> dict[str, object]:
    return {"state": "clean", "citations": []}


@pytest.mark.asyncio
async def test_trust_floor_trace_data_carries_no_signin_intent_key() -> None:
    policy = await build_request_policy_trust_floor(
        user_message="Sign in to the reporting portal and download last week's report.",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-replay",
        handler=_clean_safety_handler,
    )

    assert SIGNIN_TRACE_KEYS.isdisjoint(policy.to_trace_data())


def test_dynamic_system_prompt_never_mentions_a_sign_in_address() -> None:
    instructions = agent_module._build_dynamic_system_prompt(tool_usage_guide="", config=agent_module.CopilotConfig())

    ctx = make_copilot_ctx(request_policy=RequestPolicy())

    prompt = instructions(SimpleNamespace(context=ctx), None)

    assert "signin" not in prompt.lower()


def test_stored_context_carrying_a_signin_email_still_loads() -> None:
    stored = json.dumps(
        {"user_goal": "sign in", "signin_email": "user@example.com", "signin_email_host": "example.com"}
    )

    structured = StructuredContext.from_json_str(stored)

    assert structured.user_goal == "sign in"
