from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import (
    _evaluate_post_hook,
    _inspect_page_for_composition_impl,
    _mark_pending_browser_interaction_observation,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        request_policy=RequestPolicy(),
        build_phase=BuildPhase.COMPOSING,
        turn_intent=TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        ),
    )


@pytest.mark.asyncio
async def test_evaluate_nested_rows_records_interaction_observation_step() -> None:
    ctx = _ctx()
    _mark_pending_browser_interaction_observation(
        ctx,
        tool_name="click",
        url="https://example.test/search/results?s=1",
    )

    result = {
        "ok": True,
        "data": {
            "url": "https://example.test/search/results?s=1",
            "title": "Search Results",
            "result": {
                "rows": [
                    {
                        "cells": [
                            {"text": "Example Person"},
                            {"text": "Credential A"},
                            {"text": "Number 123"},
                            {"text": "Expiration 2030-01-01"},
                        ]
                    }
                ]
            },
        },
    }

    updated = await _evaluate_post_hook(result, raw={}, ctx=ctx)

    assert updated["observation_step"] == 0
    assert updated["data"]["observation_step"] == 0
    assert len(ctx.flow_evidence) == 1
    assert ctx.flow_evidence[0]["reached_via"] == "interaction"
    assert ctx.flow_evidence[0]["had_bounded_schema"] is True
    assert ctx.flow_evidence[0]["step"] == 0
    evidence = ctx.flow_evidence[0]["evidence"]
    assert evidence["source_tool"] == "evaluate"
    assert evidence["current_url"] == "https://example.test/search/results?s=1"
    assert evidence["result_containers"][0]["row_count"] == 1
    assert "Credential A" in evidence["result_containers"][0]["sample_rows"][0]
    assert ctx.composition_page_evidence is evidence


@pytest.mark.asyncio
async def test_evaluate_turnstile_key_records_challenge_observation_step() -> None:
    ctx = _ctx()
    _mark_pending_browser_interaction_observation(
        ctx,
        tool_name="type_text",
        url="https://example.test/certificant-search",
    )

    result = {
        "ok": True,
        "data": {
            "url": "https://example.test/certificant-search",
            "title": "Certificant Search",
            "text": "Verify you are human before searching.",
            "turnstile": True,
            "btnDisabled": True,
        },
    }

    updated = await _evaluate_post_hook(result, raw={}, ctx=ctx)

    assert updated["observation_step"] == 0
    assert len(ctx.flow_evidence) == 1
    evidence = ctx.flow_evidence[0]["evidence"]
    assert evidence["source_tool"] == "evaluate"
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["kind"] == "captcha"
    assert evidence["challenge_state"]["gates_submit_controls"] is True
    assert evidence["challenge_state"]["gated_submit_controls"][0]["disabled"] is True
    assert "turnstile" in evidence["anti_bot_indicators"]
    assert ctx.composition_page_evidence is evidence


@pytest.mark.asyncio
async def test_target_url_inspection_does_not_navigate_away_from_interaction_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.flow_evidence.append(
        {
            "evidence": {
                "source_tool": "evaluate",
                "current_url": "https://example.test/search/results?s=1",
                "inspected_url": "https://example.test/search/results?s=1",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [{"tag": "table", "selector": "#results"}],
                "challenge_controls": [],
            },
            "reached_via": "interaction",
            "had_bounded_schema": True,
            "step": 4,
        }
    )

    async def unexpected_navigate(*_: object, **__: object) -> dict[str, object]:
        raise AssertionError("target_url inspection should not navigate away from reached evidence")

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools._discovery_navigate", unexpected_navigate)

    result = await _inspect_page_for_composition_impl(ctx, "https://example.test/")

    assert result["ok"] is False
    assert result["data"] == {
        "current_url": "https://example.test/search/results?s=1",
        "observation_step": 4,
    }
    assert 'target_url="current_page"' in result["error"]
    assert "observation_step 4" in result["error"]
