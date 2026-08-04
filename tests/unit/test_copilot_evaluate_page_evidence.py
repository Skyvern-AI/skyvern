from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence
from skyvern.forge.sdk.copilot.tools import (
    _evaluate_post_hook,
    _inspect_page_for_composition_impl,
    _mark_pending_browser_interaction_observation,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.blockers import _tool_loop_error
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode
from skyvern.forge.sdk.schemas.credentials import CredentialType


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
@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "error": "evaluate failed"},
        {"ok": True},
        {"ok": True, "data": {}},
        {"ok": True, "data": []},
    ],
)
async def test_evaluate_post_hook_resets_steer_on_unusable_result(result: dict[str, object]) -> None:
    ctx = _ctx()
    ctx.last_evaluate_actionable_signature = "stale-signature"
    ctx.last_evaluate_actionable_url = "https://example.test/old"
    ctx.latest_evaluate_result_composition_steer = LoadedResultCompositionEvidence(
        result_container_count=1,
        table_result_container_count=1,
    )

    updated = await _evaluate_post_hook(result, raw={}, ctx=ctx)

    assert updated is result
    assert ctx.last_evaluate_actionable_signature is None
    assert ctx.last_evaluate_actionable_url is None
    assert ctx.latest_evaluate_result_composition_steer is None


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
    assert evidence["challenge_state"]["requires_human_verification"] is True
    assert evidence["challenge_state"]["gates_submit_controls"] is True
    assert evidence["challenge_state"]["gated_submit_controls"][0]["disabled"] is True
    assert "turnstile" in evidence["anti_bot_indicators"]
    assert ctx.composition_page_evidence is evidence


@pytest.mark.asyncio
async def test_evaluate_nested_challenge_payload_does_not_block_before_attempt() -> None:
    ctx = _ctx()

    result = {
        "ok": True,
        "data": {
            "url": "https://example.test/certificant-search",
            "title": "Certificant Search",
            "buttons": [{"text": "Search", "disabled": True, "selector": "#search-button"}],
            "fields": [
                {
                    "label": "Verification code",
                    "name": "captcha_response",
                    "placeholder": "Enter verification code",
                }
            ],
        },
    }

    await _evaluate_post_hook(result, raw={}, ctx=ctx)

    evidence = ctx.composition_page_evidence
    assert evidence is not None
    assert evidence["source_tool"] == "evaluate"
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["requires_human_verification"] is True
    assert evidence["challenge_state"]["gates_submit_controls"] is True
    assert evidence["challenge_state"]["gated_submit_controls"][0]["disabled"] is True

    msg = _tool_loop_error(ctx, "update_and_run_blocks", {"block_labels": ["search_lookup"]})

    assert msg is None
    assert ctx.turn_halt is None


@pytest.mark.asyncio
async def test_evaluate_text_only_challenge_payload_stays_diagnostic() -> None:
    ctx = _ctx()

    result = {
        "ok": True,
        "data": {
            "url": "https://example.test/certificant-search",
            "title": "Certificant Search",
            "text": "Verify you are human before searching.",
            "turnstile": True,
        },
    }

    await _evaluate_post_hook(result, raw={}, ctx=ctx)

    evidence = ctx.composition_page_evidence
    assert evidence is not None
    assert evidence["source_tool"] == "evaluate"
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["requires_human_verification"] is False
    assert evidence["challenge_state"]["gates_submit_controls"] is False
    assert evidence["challenge_state"]["gated_submit_controls"] == []


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

    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.composition_capture._discovery_navigate", unexpected_navigate)

    result = await _inspect_page_for_composition_impl(ctx, "https://example.test/")

    assert result["ok"] is False
    assert result["data"] == {
        "current_url": "https://example.test/search/results?s=1",
        "observation_step": 4,
    }
    assert 'target_url="current_page"' in result["error"]


@pytest.mark.asyncio
async def test_current_page_inspection_finalizes_runtime_repair_context_for_next_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    run_execution_module._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_failed",
                "overall_status": "failed",
                "blocks": [
                    {
                        "label": "search_registry",
                        "status": "failed",
                        "failure_reason": 'Timeout waiting for locator("#results")',
                    }
                ],
            },
        },
    )

    async def fallback_page_info(_ctx: CopilotContext) -> tuple[str, str]:
        return "https://example.test/search?case=secret", "Search"

    async def capture_evidence(
        _ctx: CopilotContext,
        *,
        inspected_url: str,
        current_url: str,
    ) -> tuple[dict[str, object], None]:
        return (
            {
                "inspected_url": inspected_url,
                "current_url": current_url,
                "page_title": "Search",
                "source_tool": "inspect_page_for_composition",
                "forms": [{"fields": [{"label": "Search", "selector": "#search"}], "submit_controls": []}],
                "result_containers": [{"selector": "#results", "text_excerpt": "No matching records"}],
                "navigation_targets": [],
                "challenge_controls": [],
            },
            None,
        )

    async def no_completion_verification(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._fallback_page_info",
        fallback_page_info,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence",
        capture_evidence,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._maybe_run_completion_verification_from_page_observation",
        no_completion_verification,
    )

    result = await _inspect_page_for_composition_impl(ctx, "current_page")
    prompt = agent_module._code_authoring_repair_context_prompt(ctx)

    assert result["ok"] is True
    assert ctx.pending_code_authoring_runtime_repair_context is None
    assert ctx.last_code_authoring_repair_context is not None
    assert ctx.last_code_authoring_repair_context.current_origin == "https://example.test"
    assert ctx.last_code_authoring_repair_context.page_result_summaries == ["#results No matching records"]
    assert "runtime_failure_class: timeout_waiting_for_selector" in prompt
    assert "page_results: #results No matching records" in prompt
    assert "case=secret" not in ctx.last_code_authoring_repair_context.model_dump_json()


@pytest.mark.asyncio
async def test_live_seam_evaluate_records_scouted_read_from_prehook_stash() -> None:
    # Enters through the same two hooks the MCP adapter drives, with a response shaped like the
    # real one: the expression exists only in the invocation, never in the response.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    pre = await _evaluate_pre_hook({"expression": "document.querySelector('#count').textContent"}, ctx)
    assert pre is None

    result = {"ok": True, "data": {"result": "778 logs found", "url": "https://dash.example.test/logs"}}
    await _evaluate_post_hook(result, raw={"name": "evaluate"}, ctx=ctx)

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    assert len(reads) == 1
    assert reads[0]["read_expression"] == "document.querySelector('#count').textContent"
    assert reads[0]["read_result_shape"] == "str"
    assert ctx.pending_scout_read_expression is None


@pytest.mark.asyncio
async def test_scouted_read_binds_to_a_canonical_slot_when_that_is_the_requested_output() -> None:
    # A rekeyed requested output carries a digest instead of a word. Binding the read anonymously
    # keys the producer differently from the criterion, so completion verification reports no
    # evidence for an outcome the scout already demonstrated.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    slot_path = "output.request_slot_5a2fc98725209bfe8366101490eab27e9c75426782ec20214_00"
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c0", outcome="the azure error count", output_path=slot_path)]
    )

    await _evaluate_pre_hook({"expression": "document.querySelector('#count').textContent"}, ctx)
    await _evaluate_post_hook(
        {"ok": True, "data": {"result": "778 logs found", "url": "https://dash.example.test/logs"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    assert [read["read_output_path"] for read in reads] == [slot_path]


@pytest.mark.asyncio
async def test_each_read_binds_to_the_requested_output_it_names() -> None:
    # Counting requested outputs can only attribute a read when the turn requests exactly one, so a
    # request for several fields bound none of them and returned them under an anonymous path.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the visitor count", output_path="output.visitors"),
            CompletionCriterion(id="c1", outcome="the error count", output_path="output.errors"),
        ]
    )

    for expression, output_path in (
        ("document.querySelector('#visitors').textContent", "output.visitors"),
        ("document.querySelector('#errors').textContent", "output.errors"),
    ):
        await _evaluate_pre_hook({"expression": expression, "output_path": output_path}, ctx)
        await _evaluate_post_hook(
            {"ok": True, "data": {"result": "8.45K", "url": "https://dash.example.test/web"}},
            raw={"name": "evaluate"},
            ctx=ctx,
        )

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    assert [read["read_output_path"] for read in reads] == ["output.visitors", "output.errors"]
    assert ctx.pending_scout_read_output_path is None


@pytest.mark.asyncio
async def test_an_empty_read_is_not_recorded_as_a_proven_read() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    for empty in ("", [], {}, None):
        ctx = _ctx()
        ctx.request_policy = RequestPolicy(
            completion_criteria=[CompletionCriterion(id="c0", outcome="the count", output_path="output.count")]
        )
        await _evaluate_pre_hook({"expression": "document.querySelector('#c').textContent"}, ctx)
        await _evaluate_post_hook(
            {"ok": True, "data": {"result": empty, "url": "https://dash.example.test/"}},
            raw={"name": "evaluate"},
            ctx=ctx,
        )
        assert [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"] == [], empty


@pytest.mark.asyncio
async def test_a_markup_dump_is_not_recorded_as_the_read_for_a_scalar_output() -> None:
    # The live defect: the scout inspects a card by returning its outerHTML, and that probe becomes
    # the read synthesis replays as the extraction for output.visitors.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors")
        ]
    )
    markup = '<div class="card"><span>Visitors</span><span>8.3K</span></div>' * 40

    await _evaluate_pre_hook({"expression": "document.querySelector('.card').outerHTML.slice(0, 12000)"}, ctx)
    await _evaluate_post_hook(
        {"ok": True, "data": {"result": markup, "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    bound = [r for r in reads if r.get("read_output_path") == "output.visitors"]
    assert bound == [], "a markup dump must not become the proven read for a scalar requested output"


@pytest.mark.asyncio
async def test_a_scalar_read_keeps_the_value_it_saw_and_a_dump_does_not() -> None:
    # Only the type name was kept, so nothing downstream could locate the element still carrying the
    # value the scout had already read.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    async def _read(ctx: CopilotContext, expression: str, result: object) -> None:
        await _evaluate_pre_hook({"expression": expression, "output_path": "output.visitors"}, ctx)
        await _evaluate_post_hook(
            {"ok": True, "data": {"result": result, "url": "https://dash.example.test/web"}},
            raw={"name": "evaluate"},
            ctx=ctx,
        )

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors")
        ]
    )

    await _read(ctx, "document.querySelector('.tile .value').innerText", "8.7K")
    await _read(ctx, "document.querySelector('.card')", {"tag": "div", "children": 4})
    await _read(ctx, "document.body.innerText", "x" * 400)

    reads = [item for item in ctx.scout_trajectory if item.get("tool_name") == "read_value"]
    assert [item.get("read_result_value") for item in reads] == ["8.7K", None, None]


@pytest.mark.asyncio
async def test_sole_requested_output_still_claims_a_read_that_named_another_purpose() -> None:
    # Two runs registered visitors_last_week=8700 through exactly this path: the reader named its own
    # purpose and elimination still attributed the read to the one output the turn was asked for.
    # Diverting that read to the scouted-read slot left every later turn registering nothing.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors")
        ]
    )

    await _evaluate_pre_hook(
        {"expression": "document.querySelector('.card .value').innerText", "output_path": "visitor_card_value"}, ctx
    )
    await _evaluate_post_hook(
        {"ok": True, "data": {"result": "8.7K", "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    reads = [item for item in ctx.scout_trajectory if item.get("tool_name") == "read_value"]
    assert [item.get("read_output_path") for item in reads] == ["output.visitors"]


@pytest.mark.asyncio
async def test_evaluate_names_the_requested_output_no_read_has_claimed() -> None:
    # A live turn probed the tile's structure three times, each read naming its own purpose, and
    # reached authoring with nothing bound to the output it was asked for.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    # Two requested outputs, so elimination cannot attribute a read on its own and a probe that
    # names its own purpose leaves both slots unclaimed.
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors"),
            CompletionCriterion(id="c1", outcome="the number of sessions", output_path="output.sessions"),
        ]
    )

    await _evaluate_pre_hook(
        {"expression": "document.querySelector('.card')", "output_path": "visitor_card_structure"}, ctx
    )
    probe = await _evaluate_post_hook(
        {"ok": True, "data": {"result": {"tag": "div"}, "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )
    assert probe["data"]["requested_outputs_still_unread"] == ["output.sessions", "output.visitors"]

    await _evaluate_pre_hook(
        {"expression": "document.querySelector('.card .value').innerText", "output_path": "output.visitors"}, ctx
    )
    answered = await _evaluate_post_hook(
        {"ok": True, "data": {"result": "8.3K", "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )
    assert answered["data"]["requested_outputs_still_unread"] == ["output.sessions"]


@pytest.mark.asyncio
async def test_a_declared_read_holding_no_single_value_leaves_its_output_unread() -> None:
    # Claiming on the declaration alone deleted the outstanding-output signal, so the turn was told
    # its read recorded nothing and that nothing was outstanding, in the same reply (SKY-13226).
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors"),
            CompletionCriterion(id="c1", outcome="the number of sessions", output_path="output.sessions"),
        ]
    )

    await _evaluate_pre_hook(
        {"expression": "document.querySelectorAll('.card')", "output_path": "output.visitors"}, ctx
    )
    gathered = await _evaluate_post_hook(
        {"ok": True, "data": {"result": [{"t": "8.3K"}, {"t": "12"}], "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    assert gathered["data"]["claimed_output_without_a_single_value"] == "output.visitors"
    assert gathered["data"]["requested_outputs_still_unread"] == ["output.sessions", "output.visitors"]


@pytest.mark.asyncio
async def test_a_read_naming_an_output_the_request_never_asked_for_is_not_bound_to_it() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the visitor count", output_path="output.visitors"),
            CompletionCriterion(id="c1", outcome="the error count", output_path="output.errors"),
        ]
    )

    await _evaluate_pre_hook(
        {"expression": "document.querySelector('#other').textContent", "output_path": "output.invented"}, ctx
    )
    await _evaluate_post_hook(
        {"ok": True, "data": {"result": "12", "url": "https://dash.example.test/web"}},
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    assert [read["read_output_path"] for read in reads] == ["output.scouted_read"]


@pytest.mark.asyncio
async def test_a_later_diagnostic_read_does_not_evict_the_requested_output_read() -> None:
    # Reads sharing an output path collapse to the last one, so a page dump taken after the value
    # would silently replace the read the criterion is graded against.
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    slot_path = "output.request_slot_5a2fc98725209bfe8366101490eab27e9c75426782ec20214_00"
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c0", outcome="the azure error count", output_path=slot_path)]
    )

    for expression, result in (
        ("document.querySelector('#count').textContent", "778 logs found"),
        ("document.body.innerText", "a whole page of unrelated text"),
    ):
        await _evaluate_pre_hook({"expression": expression}, ctx)
        await _evaluate_post_hook(
            {"ok": True, "data": {"result": result, "url": "https://dash.example.test/logs"}},
            raw={"name": "evaluate"},
            ctx=ctx,
        )

    reads = [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"]
    assert [read["read_output_path"] for read in reads] == [slot_path, slot_path]
    # Both are retained with their own expressions; synthesis, not capture, chooses between them.
    assert [read["read_expression"] for read in reads] == [
        "document.querySelector('#count').textContent",
        "document.body.innerText",
    ]


@pytest.mark.asyncio
async def test_failed_evaluate_does_not_leak_expression_into_next_read() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    await _evaluate_pre_hook({"expression": "document.title"}, ctx)
    await _evaluate_post_hook({"ok": False, "error": "boom"}, raw={}, ctx=ctx)

    # Next evaluate carries no expression (adapter reject path) — the stale stash must not attach.
    pre = await _evaluate_pre_hook({}, ctx)
    assert pre is None
    await _evaluate_post_hook({"ok": True, "data": {"result": "still here"}}, raw={}, ctx=ctx)

    assert [i for i in ctx.scout_trajectory if i.get("tool_name") == "read_value"] == []


@pytest.mark.asyncio
async def test_inspecting_a_login_page_binds_the_credential_that_page_vouches_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_url = "https://analytics.example.test/login?next=%2Fweb"
    ctx = _ctx()

    async def fallback_page_info(_ctx: CopilotContext) -> tuple[str, str]:
        return login_url, "Sign in"

    async def capture_evidence(
        _ctx: CopilotContext,
        *,
        inspected_url: str,
        current_url: str,
    ) -> tuple[dict[str, object], None]:
        return (
            {
                "inspected_url": inspected_url,
                "current_url": current_url,
                "page_title": "Sign in",
                "source_tool": "inspect_page_for_composition",
                "forms": [],
                "result_containers": [],
                "navigation_targets": [],
                "challenge_controls": [],
            },
            None,
        )

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._fallback_page_info",
        fallback_page_info,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence",
        capture_evidence,
    )
    org_credentials = [
        SimpleNamespace(
            credential_id="cred_analytics",
            name="analytics",
            tested_url="https://analytics.example.test/login",
            credential_type=CredentialType.PASSWORD,
        )
    ]

    with patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=org_credentials)):
        result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["resolved_login_credential_id"] == "cred_analytics"
    assert result["resolved_login_credential_name"] == "analytics"
    assert ctx.request_policy.live_page_admitted_urls == {"cred_analytics": login_url}
    assert "tested_url" not in json.dumps(result)
