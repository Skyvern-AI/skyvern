from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.agent import _request_policy_agent_inputs
from skyvern.forge.sdk.copilot.request_policy import build_request_policy_trust_floor


async def _build(message: str, response: object) -> tuple[object, AsyncMock]:
    handler = AsyncMock(return_value=response)
    policy = await build_request_policy_trust_floor(
        user_message=message,
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )
    return policy, handler


@pytest.mark.asyncio
async def test_semantic_secret_is_cited_redacted_and_discarded() -> None:
    literal = "Hunter2Portal!"
    policy, handler = await _build(
        f"The password is {literal}",
        {"version": "1", "state": "detected", "handling": "block", "citations": [literal]},
    )

    handler.assert_awaited_once()
    prompt = handler.await_args.kwargs["prompt"]
    assert literal in prompt
    assert policy.raw_secret_detected is True
    assert policy.raw_secret_handling == "block"
    assert policy.user_response_policy == "ask_clarification"
    assert policy.allow_run_blocks is False
    assert literal not in policy.canonical_user_message
    assert policy.raw_secret_evidence is None
    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_citation_count == 1


@pytest.mark.asyncio
async def test_multiple_semantic_secret_citations_discard_the_entire_turn() -> None:
    first = "Hunter2Portal!"
    second = "BillingKey-8391!"
    policy, _ = await _build(
        f"Draft this config with {first} and {second}",
        {
            "version": "1",
            "state": "detected",
            "handling": "redacted_draft",
            "citations": [first, second],
        },
    )

    assert first not in policy.canonical_user_message
    assert second not in policy.canonical_user_message
    assert policy.canonical_user_message == "[INPUT_BLOCKED_BY_SECRET_SAFETY]"
    assert policy.raw_secret_handling == "block"
    assert policy.raw_secret_safety_citation_count == 2


@pytest.mark.asyncio
async def test_partial_semantic_secret_citation_fails_closed() -> None:
    policy, _ = await _build(
        "The password is Hunter2Portal1234!",
        {"version": "1", "state": "detected", "handling": "redacted_draft", "citations": ["1234"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == "[INPUT_BLOCKED_BY_SECRET_SAFETY]"


@pytest.mark.asyncio
async def test_boundary_delimited_partial_semantic_secret_cannot_leak_the_remainder() -> None:
    policy, _ = await _build(
        "The password is correct horse battery 1234!",
        {"version": "1", "state": "detected", "handling": "redacted_draft", "citations": ["1234!"]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_handling == "block"
    assert policy.canonical_user_message == "[INPUT_BLOCKED_BY_SECRET_SAFETY]"


@pytest.mark.asyncio
async def test_secret_ending_in_punctuation_can_precede_sentence_punctuation() -> None:
    literal = "BillingKey-8391!"
    policy, _ = await _build(
        f"Use {literal}.",
        {"version": "1", "state": "detected", "handling": "block", "citations": [literal]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == "[INPUT_BLOCKED_BY_SECRET_SAFETY]"


@pytest.mark.asyncio
async def test_deterministic_and_semantic_redactions_merge_before_downstream_use() -> None:
    deterministic = "password=known-secret"
    semantic = "Hunter2Portal!"
    policy, handler = await _build(
        f"Draft with {deterministic} and {semantic}",
        {"version": "1", "state": "detected", "handling": "redacted_draft", "citations": [semantic]},
    )

    prompt = handler.await_args.kwargs["prompt"]
    assert deterministic not in prompt
    assert semantic in prompt
    assert deterministic not in policy.canonical_user_message
    assert semantic not in policy.canonical_user_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (
            {"version": "1", "state": "clean", "handling": "none", "citations": ["Hunter2Portal!"]},
            "contradictory_verdict",
        ),
        (
            {"version": "1", "state": "detected", "handling": "block", "citations": ["not-in-turn-8391!"]},
            "invalid_citation",
        ),
        ({"state": "detected", "handling": "block", "citations": ["Hunter2Portal!"]}, "malformed_output"),
        ("not-json", "malformed_output"),
    ],
)
async def test_invalid_safety_states_block_before_turn_intent(response: object, failure: str) -> None:
    policy, _ = await _build("The password is Hunter2Portal!", response)

    assert policy.user_response_policy == "ask_clarification"
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False
    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == failure


@pytest.mark.asyncio
async def test_missing_dedicated_handler_blocks() -> None:
    policy = await build_request_policy_trust_floor(
        user_message="Hello",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=None,
    )

    assert policy.user_response_policy == "ask_clarification"
    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "missing_handler"


@pytest.mark.asyncio
async def test_safety_timeout_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns(**_: object) -> object:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS", 0.001)
    policy = await build_request_policy_trust_floor(
        user_message="Hello",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=_never_returns,
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "timeout"


@pytest.mark.asyncio
async def test_canonical_safe_turn_is_the_only_agent_input() -> None:
    literal = "Hunter2Portal!"
    policy, _ = await _build(
        f"Draft with {literal}",
        {"version": "1", "state": "detected", "handling": "redacted_draft", "citations": [literal]},
    )

    agent_message, _ = _request_policy_agent_inputs(
        policy,
        user_message=f"Draft with {literal}",
        chat_history_text="",
        previous_user_message=None,
    )

    assert agent_message == policy.canonical_user_message
    assert literal not in agent_message
