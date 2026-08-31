from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot import llm_config
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.runtime import AgentContext, ScoutedInteraction, _redacted_heal_adoption_failure_reason
from skyvern.forge.sdk.copilot.terminal_predicates import outcome_fully_verified
from skyvern.forge.sdk.copilot.turn_origin import HealAdoptionFailed, TurnOrigin
from skyvern.utils.strings import escape_code_fences

if TYPE_CHECKING:
    from skyvern.forge.sdk.experimentation.llm_prompt_config import LLMAPIHandler

LOG = structlog.get_logger()

_SELF_HEAL_OBSERVABLE_GOAL_PROMPT = "workflow-copilot-self-heal-observable-goal"
_SELF_HEAL_GOAL_ENTAILMENT_PROMPT = "workflow-copilot-self-heal-goal-entailment"
_SELF_HEAL_OBSERVABLE_GOAL_TIMEOUT_SECONDS = 10


def _redact_codeblock_value(value: Any, parameters: dict[str, Any]) -> Any:
    return app.AGENT_FUNCTION.redact_codeblock_parameter_values(value, parameters) if parameters else value


def _parameter_log_redaction(parameters: dict[str, Any]) -> Any:
    if not parameters:
        return nullcontext()
    from skyvern.forge.sdk.forge_log import codeblock_parameter_log_redaction, current_codeblock_log_redactor

    if current_codeblock_log_redactor() is not None:
        return nullcontext()
    return codeblock_parameter_log_redaction(lambda value: _redact_codeblock_value(value, parameters))


class _ObservableGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["1"]
    observable_end_state: str
    source_citations: list[str]


class _ObservableGoalEntailment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["1"]
    entails: bool


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
        "wait_for_either_state",
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


def _self_heal_recovery_prompt(goal: str) -> str:
    return (
        "You are performing runtime self-heal recovery for a failed code block.\n"
        "Recover this goal on the current live page and stop:\n"
        f"{goal}\n\n"
        "Constraints:\n"
        "- Browser/scout tools only.\n"
        "- Never ask the user for input.\n"
        "- Never propose or mutate workflow YAML.\n"
        "- The completion claim must describe only the observable page state now visible after recovery, "
        "not the action you performed.\n"
        '- Final answer must be strict JSON: {"type":"REPLY","user_response":"<observable end state>"}.\n'
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


async def _derive_observable_goal(
    goal: str, handler: LLMAPIHandler | None, redaction_parameters: dict[str, Any] | None = None
) -> str:
    """Derive the judge's criterion independently, before the acting loop can influence it."""
    if handler is None:
        return ""
    redaction_parameters = redaction_parameters or {}
    goal = _redact_codeblock_value(goal, redaction_parameters)
    if not isinstance(goal, str):
        return ""
    try:
        prompt = prompt_engine.load_prompt(
            template=_SELF_HEAL_OBSERVABLE_GOAL_PROMPT,
            recovery_goal=escape_code_fences(goal[:8000]),
        )
        with _parameter_log_redaction(redaction_parameters):
            raw = await asyncio.wait_for(
                handler(prompt=prompt, prompt_name=_SELF_HEAL_OBSERVABLE_GOAL_PROMPT),
                timeout=_SELF_HEAL_OBSERVABLE_GOAL_TIMEOUT_SECONDS,
            )
        raw = _redact_codeblock_value(raw, redaction_parameters)
        if not isinstance(raw, (str, dict)):
            return ""
        if isinstance(raw, str):
            from skyvern.forge.sdk.copilot.output_utils import parse_final_response

            raw = parse_final_response(raw)
        parsed = _ObservableGoal.model_validate(raw)
        observable_end_state = parsed.observable_end_state.strip()
        citations = parsed.source_citations
        citations_are_grounded = bool(citations) and all(citation and citation in goal for citation in citations)
        if not observable_end_state or not citations_are_grounded:
            return ""
        entailment_prompt = prompt_engine.load_prompt(
            template=_SELF_HEAL_GOAL_ENTAILMENT_PROMPT,
            recovery_goal=escape_code_fences(goal[:8000]),
            observable_end_state=escape_code_fences(observable_end_state[:4000]),
        )
        with _parameter_log_redaction(redaction_parameters):
            entailment_raw = await asyncio.wait_for(
                handler(prompt=entailment_prompt, prompt_name=_SELF_HEAL_GOAL_ENTAILMENT_PROMPT),
                timeout=_SELF_HEAL_OBSERVABLE_GOAL_TIMEOUT_SECONDS,
            )
        entails = entailment_raw.get("entails") if isinstance(entailment_raw, dict) else None
        entailment_raw = _redact_codeblock_value(entailment_raw, redaction_parameters)
        if not isinstance(entailment_raw, (str, dict)):
            return ""
        if isinstance(entailment_raw, str):
            from skyvern.forge.sdk.copilot.output_utils import parse_final_response

            entailment_raw = parse_final_response(entailment_raw)
        elif type(entails) is bool and "entails" in entailment_raw:
            entailment_raw["entails"] = entails
        entailment = _ObservableGoalEntailment.model_validate(entailment_raw)
        if entailment.entails is not True:
            return ""
        return observable_end_state
    except (ValidationError, asyncio.TimeoutError):
        LOG.warning("Runtime self-heal observable-goal derivation returned invalid output")
        return ""
    except Exception:
        LOG.warning("Runtime self-heal observable-goal derivation failed")
        return ""


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
    observable_goal: str,
    organization_id: str,
    llm_handler: Any,
    copilot_config: Any,
    workflow_run_id: str,
    workflow_run_block_id: str,
) -> bool:
    try:
        from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy

        del organization_id, llm_handler, copilot_config
        observable_goal = observable_goal.strip()
        if not observable_goal:
            return False
        policy = RequestPolicy(
            completion_contract=observable_goal,
            completion_contract_status="present",
            completion_criteria=[
                CompletionCriterion(
                    id="runtime_self_heal_observable_goal",
                    outcome=observable_goal,
                    kind="outcome",
                    level="run",
                )
            ],
        )
        ctx.request_policy = policy
        return True
    except Exception:
        LOG.warning(
            "Runtime self-heal completion-verification seeding failed",
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
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
        safe_observed_data = _redact_codeblock_value(observed_data, ctx.codeblock_redaction_parameters)
        if not isinstance(safe_observed_data, dict):
            return
        safe_url = str(safe_observed_data.get("url") or "")
        safe_title = str(safe_observed_data.get("title") or "")
        # Deterministic post-loop evidence capture keeps runtime self-heal verification
        # independent from MCP evaluate-hook timing and MCP server lifecycle.
        _record_composition_page_observation(
            ctx,
            source_tool="self_heal_verify",
            url=safe_url,
            title=safe_title,
            observed_data=safe_observed_data,
            append_to_flow=True,
            reached_via="auto",
        )
        await _maybe_run_completion_verification_from_page_observation(
            ctx,
            url=safe_url,
            title=safe_title,
            observed_data=safe_observed_data,
        )
    except Exception:
        LOG.warning("self-heal post-loop verification capture failed")


def _terminal_failure_note(final_text: str) -> str | None:
    terminal_kind = _classify_terminal_reply(final_text)
    if terminal_kind == "reply":
        return None
    return terminal_kind


async def _run_self_heal_recovery(
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
    redaction_parameters: dict[str, Any] | None = None,
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
    ctx.codeblock_redaction_parameters = redaction_parameters or {}
    composed_goal = _redact_codeblock_value(composed_goal, ctx.codeblock_redaction_parameters)
    if not isinstance(composed_goal, str):
        composed_goal = ""

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
    run_config.tracing_disabled = True
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
            observable_goal = await _derive_observable_goal(
                composed_goal, llm_handler, ctx.codeblock_redaction_parameters
            )
            if not observable_goal:
                return SelfHealRecoveryResult(
                    success=False,
                    verified=False,
                    action_count=0,
                    wall_clock_ms=int((time.monotonic() - started) * 1000),
                    scout_trajectory=[],
                    failure_note="no_gradeable_criteria",
                )
            from agents import GuardrailFunctionOutput, OutputGuardrail

            from skyvern.forge.sdk.copilot.agent import (
                _build_self_heal_output_guardrails,
                _run_agent_loop_with_surface,
            )

            output_guardrails = _build_self_heal_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

            from agents.exceptions import ModelBehaviorError

            try:
                with _parameter_log_redaction(ctx.codeblock_redaction_parameters):
                    result = await _run_agent_loop_with_surface(
                        ctx=ctx,
                        stream=stream,
                        chat_id=f"selfheal:{workflow_run_id}:{uuid.uuid4().hex}",
                        initial_input=_self_heal_recovery_prompt(composed_goal),
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
            from skyvern.forge.sdk.copilot.output_utils import extract_final_text

            final_text = extract_final_text(result) if result is not None else ""
            # Seed the independently derived criterion only after the loop so it cannot alter
            # acting-tool behavior, while the actor can never redefine its own success condition.
            verification_seeded = await _seed_completion_criteria(
                ctx,
                observable_goal=observable_goal,
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
    except Exception:
        wall_clock_ms = int((time.monotonic() - started) * 1000)
        LOG.warning(
            "Runtime self-heal recovery turn failed",
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
        )
        return SelfHealRecoveryResult(
            success=False,
            verified=False,
            action_count=_effective_action_count(ctx),
            wall_clock_ms=wall_clock_ms,
            performed_mutation=_performed_mutation_during_self_heal(ctx),
            scout_trajectory=list(ctx.scout_trajectory),
            failure_note="recovery_failed",
        )

    wall_clock_ms = int((time.monotonic() - started) * 1000)
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


@wraps(_run_self_heal_recovery)
async def run_self_heal_recovery(**kwargs: Any) -> SelfHealRecoveryResult:
    parameters = kwargs.get("redaction_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    propagated_error: BaseException
    adoption_message: str | None = None
    try:
        with _parameter_log_redaction(parameters):
            return await _run_self_heal_recovery(**kwargs)
    except BaseException as exc:
        if type(exc) is HealAdoptionFailed:
            adoption_message = _redacted_heal_adoption_failure_reason(exc, parameters)
        else:
            propagated_error = (
                exc.with_traceback(None)
                if app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc)
                else RuntimeError("")
            )
        del kwargs, parameters, exc
    if adoption_message is not None:
        raise HealAdoptionFailed(adoption_message) from None
    raise propagated_error from None
