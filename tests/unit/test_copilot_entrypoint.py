from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.entrypoint import (
    anchor_recovers_entrypoint,
    extract_anchor_entry_url,
    extract_in_turn_entry_url,
    resolve_turn_entrypoint_url,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from tests.unit.copilot_test_helpers import stub_copilot_agent_loop


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open https://example.com/login", "https://example.com/login"),
        ("open *https://example.com/login*", "https://example.com/login"),
        ("open http://localhost:8080/login", "http://localhost:8080/login"),
        ("no url here", None),
        ("truncated https://exam…ple.com/login", None),
    ],
)
def test_extract_anchor_entry_url(text: str, expected: str | None) -> None:
    assert extract_anchor_entry_url(text) == expected


def test_extract_in_turn_entry_url_prefers_latest_message() -> None:
    workflow_yaml = """
workflow_definition:
  blocks:
    - block_type: goto_url
      label: open_site
      url: https://workflow.example/start
"""

    assert (
        extract_in_turn_entry_url("use https://message.example/start", "", workflow_yaml)
        == "https://message.example/start"
    )


def test_extract_in_turn_entry_url_falls_back_to_workflow() -> None:
    workflow_yaml = """
workflow_definition:
  blocks:
    - block_type: goto_url
      label: open_site
      url: https://workflow.example/start
"""

    assert extract_in_turn_entry_url("continue", "", workflow_yaml) == "https://workflow.example/start"


def test_anchor_recovery_does_not_override_a_current_url() -> None:
    assert (
        anchor_recovers_entrypoint(
            "open https://current.example/start",
            "",
            "",
            "earlier https://anchor.example/start",
        )
        is None
    )


def test_anchor_recovery_supplies_missing_current_url() -> None:
    assert (
        anchor_recovers_entrypoint("continue", "", "", "earlier https://anchor.example/start")
        == "https://anchor.example/start"
    )


def test_the_eval_seed_outranks_every_other_rung() -> None:
    assert (
        resolve_turn_entrypoint_url(
            eval_entrypoint_url="https://seed.example",
            in_turn_entrypoint="https://message.example/start",
            anchor_entrypoint="https://anchor.example/start",
            persisted_entrypoint_url="https://persisted.example/start",
            current_entrypoint_url="https://current.example/start",
        )
        == "https://seed.example"
    )


@pytest.mark.parametrize(
    ("in_turn", "anchor", "persisted", "current", "expected"),
    [
        (
            "https://message.example/start",
            "https://anchor.example/s",
            "https://p.example/s",
            None,
            "https://message.example/start",
        ),
        (None, "https://anchor.example/s", "https://p.example/s", None, "https://anchor.example/s"),
        (None, None, "https://p.example/s", None, "https://p.example/s"),
        (
            None,
            "https://anchor.example/s",
            "https://p.example/s",
            "https://current.example/s",
            "https://current.example/s",
        ),
        (None, None, None, None, None),
    ],
)
def test_without_an_eval_seed_the_existing_ladder_is_unchanged(
    in_turn: str | None,
    anchor: str | None,
    persisted: str | None,
    current: str | None,
    expected: str | None,
) -> None:
    assert (
        resolve_turn_entrypoint_url(
            eval_entrypoint_url=None,
            in_turn_entrypoint=in_turn,
            anchor_entrypoint=anchor,
            persisted_entrypoint_url=persisted,
            current_entrypoint_url=current,
        )
        == expected
    )


async def _capture_seeded_turn(
    monkeypatch: pytest.MonkeyPatch, *, seed: str, global_llm_context: str | None
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["ctx"] = kwargs["ctx"]
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)

    await run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="build it, starting at https://message.example/start",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=global_llm_context,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        eval_entrypoint_url=seed,
    )
    return captured


@pytest.mark.parametrize("seed", ["https://www.google.com", "https://example.com"])
@pytest.mark.asyncio
async def test_the_eval_seed_reaches_the_turn_context_before_the_agent_loop(
    monkeypatch: pytest.MonkeyPatch, seed: str
) -> None:
    captured = await _capture_seeded_turn(monkeypatch, seed=seed, global_llm_context=None)

    assert captured["ctx"].resolved_discovery_entrypoint_url == seed
    assert seed in captured["initial_input"]


@pytest.mark.asyncio
async def test_a_legacy_prose_context_still_receives_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_seeded_turn(
        monkeypatch, seed="https://seed.example", global_llm_context="the user wants a scraper"
    )

    assert '"entrypoint_url": "https://seed.example"' in captured["initial_input"]
    assert "the user wants a scraper" in captured["initial_input"]


@pytest.mark.asyncio
async def test_an_unparsable_structured_context_is_not_erased_by_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    unparsable = '{"user_goal": "buy a widget", "credential_approvals": [truncated'
    captured = await _capture_seeded_turn(monkeypatch, seed="https://seed.example", global_llm_context=unparsable)

    assert unparsable in captured["initial_input"]
    assert "https://seed.example" not in captured["initial_input"]
    # A seed the model never saw must not still win the ladder, or the turn resolves seeded
    # while reasoning unseeded and the benchmark row claims a seed that did nothing.
    assert captured["ctx"].resolved_discovery_entrypoint_url == "https://message.example/start"
