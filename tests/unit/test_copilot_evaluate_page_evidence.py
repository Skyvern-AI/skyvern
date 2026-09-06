from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    OriginRunRedactionRegistry,
    browser_page_custody_lock,
    register_sensitive_origin_run_lease,
    release_sensitive_origin_run_lease,
    sensitive_origin_page_has_active_run,
)
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values
from skyvern.forge.sdk.copilot.tools import (
    _evaluate_post_hook,
    _inspect_page_for_composition_impl,
    _mark_pending_browser_interaction_observation,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.schemas.credentials import CredentialType, TotpType
from tests.unit.copilot_test_helpers import (
    SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS,
    remove_sensitive_disclosure_prerequisite,
    taint_by_terminal_run,
)


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        request_policy=RequestPolicy(),
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
    assert evidence["challenge_state"]["requires_human_verification"] is True
    assert evidence["challenge_state"]["gates_submit_controls"] is True
    assert evidence["challenge_state"]["gated_submit_controls"][0]["disabled"] is True
    assert "turnstile" in evidence["anti_bot_indicators"]
    assert ctx.composition_page_evidence is evidence


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


def _current_page_after_credential_run(monkeypatch: pytest.MonkeyPatch) -> CopilotContext:
    """The composition read on the page a credential run left, with the run terminal and bound."""
    ctx = _ctx()
    ctx.browser_session_id = "pbs-run"
    clear_session_scrub_values("pbs-run")
    ctx.last_run_blocks_workflow_run_id = "wr-sensitive"
    ctx.last_run_blocks_browser_session_id = "pbs-run"
    taint_by_terminal_run(ctx, workflow_run_id="wr-sensitive", session_id="pbs-run")
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr-sensitive",
        {"password": "origin-secret-2026"},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    capture = AsyncMock(
        return_value=(
            {
                "inspected_url": "https://private.example.test/otp",
                "current_url": "https://private.example.test/otp",
                "source_tool": "inspect_page_for_composition",
                "page_title": "Sign in origin-secret-2026",
                "forms": [
                    {
                        "fields": [{"name": "token", "label": "Token", "type": "text", "selector": "#token"}],
                        "submit_controls": [{"text": "Login", "type": "submit", "selector": "button.btn--login"}],
                    }
                ],
                "navigation_targets": [],
                "result_containers": [],
                "challenge_controls": [],
            },
            None,
        )
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.composition_capture._authority_tool_error", lambda *_a: None)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._fallback_page_info",
        AsyncMock(return_value=("https://private.example.test/otp", "Sign in")),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence", capture)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._bind_login_credential_for_observed_url", AsyncMock()
    )
    return ctx


@pytest.mark.asyncio
async def test_current_page_inspection_discloses_scrubbed_facts_after_a_terminal_credential_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _current_page_after_credential_run(monkeypatch)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    assert result["reached_via"] == "post_run"
    dumped = json.dumps(result)
    assert '"name": "token"' in dumped
    assert "origin-secret-2026" not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
async def test_current_page_inspection_withholds_when_a_disclosure_prerequisite_is_absent(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    ctx = _current_page_after_credential_run(monkeypatch)
    remove_sensitive_disclosure_prerequisite(ctx, arm)

    result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is False
    assert "origin-secret-2026" not in json.dumps(result)


@pytest.mark.asyncio
async def test_sensitive_named_url_inspection_clears_only_its_successfully_navigated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.browser_session_id = "pbs-debug"
    ctx.last_run_blocks_workflow_run_id = "wr-sensitive"
    ctx.last_run_blocks_browser_session_id = "pbs-debug"
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr-sensitive",
        {"password": "origin-secret"},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    ctx.sensitive_origin_browser_session_ids = {"pbs-debug", "pbs-other"}

    navigate = AsyncMock(return_value={"ok": True, "data": {"url": "https://public.example.test/search"}})
    capture = AsyncMock(
        return_value=(
            {
                "inspected_url": "https://public.example.test/search",
                "current_url": "https://public.example.test/search",
                "source_tool": "inspect_page_for_composition",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [],
                "challenge_controls": [],
            },
            None,
        )
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._authority_tool_error",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._discovery_navigate",
        navigate,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence",
        capture,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._bind_login_credential_for_observed_url",
        AsyncMock(),
    )

    result = await _inspect_page_for_composition_impl(ctx, "https://public.example.test/search")

    assert result["ok"] is True
    assert ctx.sensitive_origin_browser_session_ids == {"pbs-other"}
    navigate.assert_awaited_once()
    capture.assert_awaited_once()


@pytest.mark.asyncio
async def test_sensitive_registration_waits_for_named_navigation_capture_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.browser_session_id = "pbs-debug"
    ctx.sensitive_origin_browser_session_ids = {"pbs-debug"}
    registration_task: asyncio.Task[None] | None = None

    async def register_sensitive_run() -> None:
        async with browser_page_custody_lock(ctx):
            ctx.sensitive_origin_browser_session_ids.add("pbs-debug")
            ctx.active_sensitive_origin_browser_session_ids.add("pbs-debug")

    async def navigate(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal registration_task
        registration_task = asyncio.create_task(register_sensitive_run())
        await asyncio.sleep(0)
        assert not registration_task.done()
        return {"ok": True, "data": {"url": "https://public.example.test/search"}}

    async def capture(*_args: object, **_kwargs: object) -> tuple[dict[str, object], None]:
        assert registration_task is not None
        assert not registration_task.done()
        return (
            {
                "inspected_url": "https://public.example.test/search",
                "current_url": "https://public.example.test/search",
                "source_tool": "inspect_page_for_composition",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [],
                "challenge_controls": [],
            },
            None,
        )

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._authority_tool_error", lambda *_args: None
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.composition_capture._discovery_navigate", navigate)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence", capture)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._bind_login_credential_for_observed_url",
        AsyncMock(),
    )

    result = await _inspect_page_for_composition_impl(ctx, "https://public.example.test/search")
    assert registration_task is not None
    await registration_task

    assert result["ok"] is True
    assert ctx.sensitive_origin_browser_session_ids == {"pbs-debug"}
    assert ctx.active_sensitive_origin_browser_session_ids == {"pbs-debug"}


def test_terminal_run_releases_only_its_exact_sensitive_run_lease() -> None:
    ctx = _ctx()
    ctx.browser_session_id = "pbs-shared"
    register_sensitive_origin_run_lease(ctx, workflow_run_id="wr-paused-a", session_id="pbs-shared")
    register_sensitive_origin_run_lease(ctx, workflow_run_id="wr-terminal-b", session_id="pbs-shared")

    release_sensitive_origin_run_lease(ctx, workflow_run_id="wr-terminal-b")

    assert ctx.active_sensitive_origin_run_sessions == {"wr-paused-a": "pbs-shared"}
    assert sensitive_origin_page_has_active_run(ctx) is True

    release_sensitive_origin_run_lease(ctx, workflow_run_id="wr-paused-a")

    assert ctx.active_sensitive_origin_run_sessions == {}
    assert sensitive_origin_page_has_active_run(ctx) is False


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

    async def fallback_page_info(_ctx: CopilotContext, _session_id_override: str | None = None) -> tuple[str, str]:
        return "https://example.test/search?case=secret", "Search"

    async def capture_evidence(
        _ctx: CopilotContext,
        *,
        inspected_url: str,
        current_url: str,
        **_kwargs: object,
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

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._fallback_page_info",
        fallback_page_info,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence",
        capture_evidence,
    )
    result = await _inspect_page_for_composition_impl(ctx, "current_page")
    prompt = agent_module._code_authoring_repair_context_prompt(ctx)

    assert result["ok"] is True
    assert ctx.pending_code_authoring_runtime_repair_context is None
    assert ctx.last_code_authoring_repair_context is not None
    assert ctx.last_code_authoring_repair_context.current_origin == "https://example.test"
    assert ctx.last_code_authoring_repair_context.page_result_summaries == ["No matching records"]
    assert "runtime_failure_class:" not in prompt
    assert 'runtime_failure_reason: Timeout waiting for locator("#results")' in prompt
    assert "page_results: No matching records" in prompt
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
async def test_evaluate_click_expression_is_not_refused_by_the_prehook() -> None:
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    expression = "document.querySelector('#submit').click()"

    result = await _evaluate_pre_hook({"expression": expression}, ctx)

    assert result is None
    assert ctx.pending_scout_read_expression == expression


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
    assert gathered["data"]["requested_output_designation_capability"] == {
        "tool": "inspect_page_for_composition",
        "argument": "requested_output_reads",
        "page_reference": "current_page",
        "requested_output_paths": ["output.visitors"],
        "citation_fields": ["output_path", "value_text", "label"],
        "effect": "browser verifies the cited rendered value and returns selector candidates",
    }
    assert gathered["data"]["requested_outputs_still_unread"] == ["output.sessions", "output.visitors"]


@pytest.mark.asyncio
async def test_a_non_scalar_read_returns_visible_designation_candidates_as_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import mcp_hooks
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _evaluate_pre_hook

    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors")
        ]
    )
    monkeypatch.setattr(mcp_hooks, "unbound_candidate_relations", lambda _evidence: [("Visitors", "8.89K")])

    await _evaluate_pre_hook({"expression": "document.body.innerText", "output_path": "output.visitors"}, ctx)
    gathered = await _evaluate_post_hook(
        {
            "ok": True,
            "data": {
                "result": {"label": "Visitors", "value": "8.89K"},
                "url": "https://dash.example.test/web",
            },
        },
        raw={"name": "evaluate"},
        ctx=ctx,
    )

    assert gathered["data"]["requested_output_designation_candidates"] == [{"label": "Visitors", "value_text": "8.89K"}]


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

    async def fallback_page_info(_ctx: CopilotContext, _session_id_override: str | None = None) -> tuple[str, str]:
        return login_url, "Sign in"

    async def capture_evidence(
        _ctx: CopilotContext,
        *,
        inspected_url: str,
        current_url: str,
        **_kwargs: object,
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
            totp_type=TotpType.NONE,
        )
    ]

    with patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=org_credentials)):
        result = await _inspect_page_for_composition_impl(ctx, "current_page")

    assert result["resolved_login_credential_id"] == "cred_analytics"
    assert result["resolved_login_credential_name"] == "analytics"
    assert ctx.request_policy.live_page_admitted_urls == {"cred_analytics": login_url}
    assert "tested_url" not in json.dumps(result)
