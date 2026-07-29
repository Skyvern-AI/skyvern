from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.copilot import llm_config
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.runtime import AgentContext, ScoutedInteraction
from skyvern.forge.sdk.copilot.terminal_predicates import outcome_fully_verified
from skyvern.forge.sdk.copilot.turn_origin import HealAdoptionFailed, TurnOrigin

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.turn_intent import TurnIntent

LOG = structlog.get_logger()

_SELF_HEAL_MCP_TOOL_ALLOWLIST = frozenset(
    {
        "navigate_browser",
        "get_browser_screenshot",
        "evaluate",
        "click",
        "type_text",
        "scroll",
        "console_messages",
        "select_option",
        "press_key",
    }
)


class _NoopEventSourceStream:
    async def send(self, data: Any) -> bool:
        del data
        return True

    async def is_disconnected(self) -> bool:
        return False

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class SelfHealRecoveryResult:
    success: bool
    action_count: int
    wall_clock_ms: int
    verified: bool = False
    performed_mutation: bool = False
    scout_trajectory: list[ScoutedInteraction] = field(default_factory=list)
    failure_note: str | None = None


def _runtime_self_heal_intent() -> TurnIntent:
    from skyvern.forge.sdk.copilot.turn_intent import (
        TurnIntent,
        TurnIntentAuthority,
        TurnIntentExpectedOutput,
        TurnIntentMode,
        TurnIntentReasonCode,
    )

    authority = TurnIntentAuthority(
        may_update_workflow=False,
        may_run_blocks=False,
        may_answer_without_mutation=True,
        requires_user_input=False,
        may_read_run_context=False,
    )
    return TurnIntent(
        mode=TurnIntentMode.BUILD,
        user_goal="runtime self-heal recovery",
        authority=authority,
        expected_output=TurnIntentExpectedOutput.EXPLANATION,
        reason_codes=[TurnIntentReasonCode.REQUEST_POLICY_DERIVED],
        confidence=1.0,
    )


def _self_heal_recovery_prompt(goal: str) -> str:
    return (
        "You are performing runtime self-heal recovery for a failed code block.\n"
        "Recover this goal on the current live page and stop:\n"
        f"{goal}\n\n"
        "Constraints:\n"
        "- Browser/scout tools only.\n"
        "- Never ask the user for input.\n"
        "- Never propose or mutate workflow YAML.\n"
        '- Final answer must be strict JSON: {"type":"REPLY","user_response":"<short completion claim>"}.\n'
    )


def _self_heal_tool_surface() -> tuple[dict[str, str], dict[str, Any]]:
    from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map

    alias_map = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    return (
        {name: target for name, target in alias_map.items() if name in _SELF_HEAL_MCP_TOOL_ALLOWLIST},
        {name: overlay for name, overlay in overlays.items() if name in _SELF_HEAL_MCP_TOOL_ALLOWLIST},
    )


def _classify_terminal_reply(final_text: str) -> str:
    from skyvern.forge.sdk.copilot.output_utils import parse_final_response

    response_type = ""
    stripped = final_text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped, strict=False)
        except Exception:
            parsed = parse_final_response(final_text)
            if parsed.get("user_response") == final_text:
                return "unparseable_terminal"
        if isinstance(parsed, dict):
            response_type = str(parsed.get("type") or "").strip().upper()

    if response_type == "ASK_QUESTION":
        return "asked_user_question"
    if response_type == "REPLACE_WORKFLOW":
        return "proposed_workflow_mutation"
    if response_type == "REPLY":
        return "reply"
    return "unparseable_terminal"


def _count_successful_self_heal_browser_calls(ctx: AgentContext) -> int:
    count = 0
    for activity in getattr(ctx, "tool_activity", []):
        if not isinstance(activity, dict):
            continue
        tool_name = str(activity.get("tool") or "").strip()
        if tool_name not in _SELF_HEAL_MCP_TOOL_ALLOWLIST:
            continue
        summary = str(activity.get("summary") or "").strip()
        if summary.startswith("Failed:"):
            continue
        count += 1
    return count


def _effective_action_count(ctx: AgentContext) -> int:
    return max(len(ctx.scout_trajectory), _count_successful_self_heal_browser_calls(ctx))


_SELF_HEAL_MUTATING_TOOLS = frozenset({"click", "type_text", "select_option", "press_key", "evaluate"})


def _performed_mutation_during_self_heal(ctx: AgentContext) -> bool:
    for activity in getattr(ctx, "tool_activity", []):
        if not isinstance(activity, dict):
            continue
        tool_name = str(activity.get("tool") or "").strip()
        # evaluate runs arbitrary JS (form.submit(), dispatched clicks), so it can commit a side
        # effect without a click/type call; treat it as mutating so the fail-closed floor
        # suppression is not bypassed.
        if tool_name not in _SELF_HEAL_MUTATING_TOOLS:
            continue
        summary = str(activity.get("summary") or "").strip()
        if summary.startswith("Failed:"):
            continue
        return True
    return False


async def _seed_completion_criteria(
    ctx: AgentContext,
    *,
    composed_goal: str,
    organization_id: str,
    llm_handler: Any,
    copilot_config: Any,
    workflow_run_id: str,
    workflow_run_block_id: str,
) -> bool:
    try:
        from skyvern.forge.sdk.copilot.request_policy import build_request_policy

        # A runtime heal is verified from the post-recovery page, so criteria must be phrased as
        # the observable end state (what is or is not present), not the actions taken to reach it.
        # An action-phrased criterion ("the Accept button is clicked") is unverifiable from a page
        # snapshot and forces every heal to regress to the floor.
        verification_request = (
            f"{composed_goal}\n\n"
            "Define completion criteria strictly as the observable end state of the page once this "
            "is achieved (what becomes visible, or is no longer visible), never as the actions taken."
        )
        policy = await build_request_policy(
            user_message=verification_request,
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            organization_id=organization_id,
            handler=llm_handler,
            config=copilot_config,
        )
        ctx.request_policy = policy
        # verification_seeded must mean "there is something to grade", not merely "the call
        # returned". Current main's criteria generator is conservative, and a machine-written heal
        # goal can yield zero gradeable criteria, which makes verified=True unreachable and would
        # silently regress every such heal to the floor. Surface it as a distinct signal instead.
        if not policy.graded_completion_criteria():
            LOG.warning(
                "Runtime self-heal produced no gradeable completion criteria; verification cannot pass",
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            return False
        return True
    except Exception:
        LOG.warning(
            "Runtime self-heal completion-verification seeding failed",
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            exc_info=True,
        )
        return False


async def _run_post_loop_verification_from_browser_state(ctx: AgentContext, *, browser_state: Any) -> None:
    try:
        page = await browser_state.get_or_create_page()
        from skyvern.forge.sdk.copilot.composition_browser_expressions import COMPOSITION_STRIPPED_HTML_EXPRESSION
        from skyvern.forge.sdk.copilot.tools.completion import _maybe_run_completion_verification_from_page_observation
        from skyvern.forge.sdk.copilot.tools.page_observation import _record_composition_page_observation

        html = await page.evaluate(COMPOSITION_STRIPPED_HTML_EXPRESSION)
        url = page.url
        title = await page.title()
        observed_data = {
            "html": html,
            "url": url,
            "title": title,
        }
        # Deterministic post-loop evidence capture keeps runtime self-heal verification
        # independent from MCP evaluate-hook timing and MCP server lifecycle.
        _record_composition_page_observation(
            ctx,
            source_tool="self_heal_verify",
            url=url,
            title=title,
            observed_data=observed_data,
            append_to_flow=True,
            reached_via="auto",
        )
        await _maybe_run_completion_verification_from_page_observation(
            ctx,
            url=url,
            title=title,
            observed_data=observed_data,
        )
    except Exception:
        LOG.warning("self-heal post-loop verification capture failed", exc_info=True)


def _terminal_failure_note(final_text: str) -> str | None:
    terminal_kind = _classify_terminal_reply(final_text)
    if terminal_kind == "reply":
        return None
    return terminal_kind


async def run_self_heal_recovery(
    *,
    block: Any,
    workflow_run_context: Any,
    workflow_run_id: str,
    workflow_run_block_id: str,
    organization_id: str,
    browser_state: Any,
    failing_line: int | None,
    api_key: str,
    max_actions: int,
    wall_clock_budget_seconds: int,
) -> SelfHealRecoveryResult:
    if max_actions <= 0:
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=0,
            wall_clock_ms=0,
            scout_trajectory=[],
            failure_note="max_actions_exhausted",
        )

    composed_goal = block._compose_heal_goal(workflow_run_context=workflow_run_context, failing_line=failing_line)
    stream = _NoopEventSourceStream()
    ctx = AgentContext(
        organization_id=organization_id,
        workflow_id=workflow_run_context.workflow_id,
        workflow_permanent_id=workflow_run_context.workflow_permanent_id,
        workflow_yaml="",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
        turn_origin=TurnOrigin.runtime_self_heal,
        injected_browser_state=browser_state,
        heal_workflow_run_id=workflow_run_id,
    )
    ctx.turn_intent = _runtime_self_heal_intent()

    copilot_config = app.AGENT_FUNCTION.get_copilot_config() or CopilotConfig()
    copilot_config.max_turns = min(copilot_config.max_turns, max_actions + 1)
    llm_handler = await llm_config.resolve_main_copilot_handler(
        workflow_run_context.workflow_permanent_id, organization_id
    )
    from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

    model_name, run_config, llm_key, _supports_vision = resolve_model_config(
        llm_handler,
        copilot_config=copilot_config,
    )
    alias_map, overlays = _self_heal_tool_surface()
    if not alias_map:
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=0,
            wall_clock_ms=0,
            scout_trajectory=[],
            failure_note="tool_surface_unavailable",
        )
    verification_seeded = False

    started = time.monotonic()
    try:
        async with asyncio.timeout(wall_clock_budget_seconds):
            from agents import GuardrailFunctionOutput, OutputGuardrail

            from skyvern.forge.sdk.copilot.agent import (
                _build_self_heal_output_guardrails,
                _run_agent_loop_with_surface,
            )

            output_guardrails = _build_self_heal_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

            from agents.exceptions import ModelBehaviorError

            try:
                result = await _run_agent_loop_with_surface(
                    ctx=ctx,
                    stream=stream,
                    chat_id=f"selfheal:{workflow_run_id}:{uuid.uuid4().hex}",
                    user_message=_self_heal_recovery_prompt(composed_goal),
                    system_prompt="You are a browser-only runtime self-heal recovery agent.",
                    model_name=model_name,
                    run_config=run_config,
                    llm_key=llm_key,
                    copilot_config=copilot_config,
                    native_tools=[],
                    alias_map=alias_map,
                    overlays=overlays,
                    output_guardrails=output_guardrails,
                    allow_untested_retry=False,
                )
            except ModelBehaviorError:
                # The model can emit an out-of-surface tool call (e.g. update_workflow) after
                # already fixing the page; the SDK raises it as fatal. The browser work may
                # satisfy the goal, so grade the live page and let the judge be authoritative.
                LOG.warning(
                    "Runtime self-heal agent made an out-of-surface tool call; grading page anyway",
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                )
                result = None
            # Seed completion criteria AFTER the loop: build_request_policy classifies the heal
            # goal as an authoring request, so pre-loop seeding drives the browser-only agent to
            # call workflow-authoring tools (update_workflow) absent from its surface. Post-loop,
            # only the judge reads the criteria.
            verification_seeded = await _seed_completion_criteria(
                ctx,
                composed_goal=composed_goal,
                organization_id=organization_id,
                llm_handler=llm_handler,
                copilot_config=copilot_config,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            ctx.post_run_page_observation_after_failed_test = True
            await _run_post_loop_verification_from_browser_state(ctx, browser_state=browser_state)
    except HealAdoptionFailed:
        raise
    except asyncio.TimeoutError:
        wall_clock_ms = int((time.monotonic() - started) * 1000)
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=_effective_action_count(ctx),
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note="wall_clock_budget_exhausted",
        )
    except Exception as exc:
        wall_clock_ms = int((time.monotonic() - started) * 1000)
        LOG.warning(
            "Runtime self-heal recovery turn failed",
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=_effective_action_count(ctx),
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note=type(exc).__name__,
        )

    wall_clock_ms = int((time.monotonic() - started) * 1000)
    from skyvern.forge.sdk.copilot.output_utils import extract_final_text

    final_text = extract_final_text(result) if result is not None else ""
    action_count = _effective_action_count(ctx)
    verified = verification_seeded and outcome_fully_verified(ctx)
    if action_count > max_actions:
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=action_count,
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note="max_actions_exhausted",
        )
    if result is not None:
        terminal_failure_note = _terminal_failure_note(final_text)
        if terminal_failure_note is not None:
            return SelfHealRecoveryResult(
                success=False,
                verified=False,
                action_count=action_count,
                wall_clock_ms=wall_clock_ms,
                performed_mutation=_performed_mutation_during_self_heal(ctx),
                scout_trajectory=list(ctx.scout_trajectory),
                failure_note=terminal_failure_note,
            )
    elif not verified:
        # The loop ended on an out-of-surface tool call, so there is no clean terminal reply.
        # Success then rests entirely on the judge; an unverified page is a real failure.
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=action_count,
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note="out_of_surface_tool_call",
        )
    if action_count < 1:
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=action_count,
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note="no_action_progress",
        )
    if verified:
        unverified_note = None
    elif not verification_seeded:
        unverified_note = "no_gradeable_criteria"
    else:
        unverified_note = "goal_unverified"
    return SelfHealRecoveryResult(
        success=True,
        verified=verified,
        action_count=action_count,
        wall_clock_ms=wall_clock_ms,
        performed_mutation=_performed_mutation_during_self_heal(ctx),
        scout_trajectory=list(ctx.scout_trajectory),
        failure_note=unverified_note,
    )
