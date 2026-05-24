"""Shared LLM tool-use loop for the v3 agentic reviewer.

Used by both ``midrun.v3_review_in_flight`` and
``postrun.v3_review_post_run``. The loop:

1. Calls ``llm_caller.call(prompt, tools=...)`` with ``use_message_history=True``.
2. Parses tool_use blocks from the response.
3. Dispatches each tool call through the :class:`SkillRegistry`.
4. Feeds tool_result blocks back to the caller via ``add_tool_result``.
5. Loops until: a terminal skill fired, budget exhausted, or wall-clock hit.

The wall-clock cap is enforced by wrapping the loop in ``asyncio.wait_for``
at the caller — this module's job is to honor a passed-in :class:`Budget`
plus the per-skill timeouts that :class:`Skill` already enforces.

Design notes:
- ``raw_response=True`` returns the LiteLLM/Vertex response object directly,
  giving us access to tool_use blocks and per-call cost/token telemetry.
- Per-cycle latency, cost, and tokens are accumulated in the returned
  timeline so consumers can persist artifacts post-loop.
- The loop is provider-agnostic: it works with Anthropic-style tool_use and
  with Gemini's function-calling shape, because LiteLLM normalizes both into
  the same response.choices[0].message.tool_calls / tool_use structure.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any, Awaitable, Callable

import litellm
import structlog

from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.forge.sdk.copilot.loop_detection import (
    detect_failed_tool_step_loop,
    detect_tool_loop,
    record_tool_step_result,
)
from skyvern.services.script_reviewer_v3.budget import Budget, InvocationHandle
from skyvern.services.script_reviewer_v3.decision import Decision
from skyvern.services.script_reviewer_v3.skills import SkillResult
from skyvern.services.script_reviewer_v3.skills.base import SkillRegistry

LOG = structlog.get_logger()


@dataclasses.dataclass
class AgentLoopResult:
    """Outcome of one full agent-loop run.

    ``terminal_decision`` is None only when the loop never produced a
    terminal — the caller maps that to ``budget_exhausted`` or ``loop_error``
    as appropriate.
    """

    terminal_decision: Decision | None
    timeline: list[dict[str, Any]]
    budget: Budget


# Type aliases for the bound-context callbacks the loop accepts. A handler
# returns the Decision directly when its tool name appears in `terminal_names`;
# otherwise the executor returns a SkillResult and the loop continues.
ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[SkillResult]]

# Terminal builders return (decision_or_none, should_stop). `decision_or_none`
# is the Decision the caller wants to surface (or None to fall back to the
# default schema-driven builder). `should_stop` controls whether the loop
# exits after this tool call — post-run uses this to keep the loop running
# through per-episode terminals (declare_review_complete / give_up_episode /
# demote_class_a) until the LLM finally emits a global terminal.
TerminalBuilder = Callable[
    [str, dict[str, Any], SkillResult],
    tuple[Decision | None, bool],
]


def _extract_tool_calls(response: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Extract ``(tool_call_id, tool_name, args)`` from a raw LLM response.

    LiteLLM normalizes tool calls under ``choices[0].message.tool_calls``
    regardless of provider. Each entry has ``.id``, ``.function.name``, and
    ``.function.arguments`` (JSON string).

    Returns an empty list if there are no tool calls (the response is just
    text, which the agent loop treats as the LLM stalling — bumps cycle but
    keeps going one more turn).
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    try:
        choices = getattr(response, "choices", None) or response.get("choices", [])  # type: ignore[union-attr]
        if not choices:
            return out
        message = getattr(choices[0], "message", None) or choices[0].get("message")  # type: ignore[union-attr]
        if message is None:
            return out
        tool_calls = getattr(message, "tool_calls", None) or message.get("tool_calls")  # type: ignore[union-attr]
        if not tool_calls:
            return out
        for tc in tool_calls:
            tc_id = getattr(tc, "id", None) or tc.get("id")  # type: ignore[union-attr]
            fn = getattr(tc, "function", None) or tc.get("function")  # type: ignore[union-attr]
            if not fn:
                continue
            name = getattr(fn, "name", None) or fn.get("name")  # type: ignore[union-attr]
            args_raw = getattr(fn, "arguments", None) or fn.get("arguments")  # type: ignore[union-attr]
            if not name:
                continue
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            out.append((tc_id or f"call_{len(out)}", name, args))
    except Exception:  # pragma: no cover — defensive parsing
        LOG.warning("Failed to parse tool calls from v3 LLM response", exc_info=True)
    return out


def _extract_usage(response: Any) -> tuple[int, int, float]:
    """Pull (input_tokens, output_tokens, cost_usd) from a raw response.

    LiteLLM attaches ``response.usage`` and ``response._hidden_params['response_cost']``
    for most providers. Missing fields fall back to 0.

    the v3 router workaround sometimes
    returns responses without ``_hidden_params['response_cost']`` populated
    (raw_response=True path bypasses litellm.completion_cost). When that
    happens we fall back to ``litellm.completion_cost(completion_response=response)``
    just like the main Skyvern handler does (see
    ``skyvern/forge/sdk/api/llm/api_handler_factory.py:1110``). Final fallback
    is 0 — never raises.
    """
    try:
        usage = getattr(response, "usage", None) or response.get("usage", {})  # type: ignore[union-attr]
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or usage.get("prompt_tokens", 0) or 0)  # type: ignore[union-attr]
        output_tokens = int(
            getattr(usage, "completion_tokens", 0) or usage.get("completion_tokens", 0) or 0  # type: ignore[union-attr]
        )
    except Exception:
        input_tokens, output_tokens = 0, 0
    cost_usd = 0.0
    try:
        hidden = getattr(response, "_hidden_params", None) or {}
        cost_usd = float(hidden.get("response_cost", 0.0) or 0.0)
    except Exception:
        cost_usd = 0.0
    # Fallback: when response_cost is 0/missing, ask litellm to compute it
    # from the model+usage. This is the same call the main Skyvern LLM
    # handler uses, so cost numbers stay comparable across v2 and v3.
    if cost_usd == 0.0:
        try:
            computed = litellm.completion_cost(completion_response=response)
            cost_usd = float(computed or 0.0)
        except Exception:
            cost_usd = 0.0
    return input_tokens, output_tokens, cost_usd


def _extract_text(response: Any) -> str:
    try:
        choices = getattr(response, "choices", None) or response.get("choices", [])  # type: ignore[union-attr]
        if not choices:
            return ""
        message = getattr(choices[0], "message", None) or choices[0].get("message")  # type: ignore[union-attr]
        if message is None:
            return ""
        content = getattr(message, "content", None) or message.get("content")  # type: ignore[union-attr]
        return content or ""
    except Exception:
        return ""


async def run_agent_loop(
    *,
    llm_caller: LLMCaller,
    system_prompt: str,
    user_prompt: str,
    registry: SkillRegistry,
    agent_kind: str,
    context: Any,
    budget: Budget,
    organization_id: str | None = None,
    prompt_name: str | None = None,
    invocation_handle: InvocationHandle | None = None,
    terminal_builder: TerminalBuilder | None = None,
) -> AgentLoopResult:
    """Drive an LLM tool-use loop until a terminal decision or budget exhaustion.

    Parameters
    ----------
    llm_caller
        A pre-built :class:`LLMCaller`. The caller is responsible for picking
        the LLM key (currently :data:`V3_REVIEWER_MODEL`).
    system_prompt
        Static role + instructions injected as the system message.
    user_prompt
        Initial user message with FailureContext / PostRunContext details.
    registry
        Skill registry. The loop reads ``tool_schemas(agent_kind)`` for the
        LLM call and ``terminal_names(agent_kind)`` to know when to stop.
    agent_kind
        ``"midrun"`` or ``"postrun"``. Filters the skill set.
    context
        Opaque object passed to every skill handler. Typically the
        :class:`FailureContext` or :class:`PostRunContext`.
    budget
        Per-review triple budget. Each cycle ticks ``cycles_used``; each LLM
        response charges tokens + cost.
    organization_id, prompt_name
        Forwarded to :meth:`LLMCaller.call` for telemetry attribution.
    invocation_handle
        Optional run-budget handle (mid-run only). Cost is reconciled on
        return regardless of outcome.
    terminal_builder
        Optional override that maps ``(tool_name, args, skill_result)`` to a
        :class:`Decision`. Defaults to a generic builder that reads ``reason``
        and ``investigation_summary`` from ``args``. Pass a custom builder for
        per-agent decision construction (e.g., mapping ``persist_block_edit``'s
        success to ``Decision.declare_success``).
    """
    timeline: list[dict[str, Any]] = []
    terminal_names = registry.terminal_names(agent_kind)
    tools = registry.tool_schemas(agent_kind)

    # Anti-oscillation trackers, ported from Copilot's loop_detection module.
    # ``consecutive_tool_tracker`` catches A-A-A streaks (same skill 3 times
    # in a row). ``failed_step_tracker`` catches repeated failures of the
    # same (skill, args) pair — even when other skills run in between.
    # Terminal skills are exempted from loop detection because they're
    # called once and end the loop anyway.
    consecutive_tool_tracker: list[str] = []
    failed_step_tracker: dict[str, int] = {}

    # We maintain ``messages`` ourselves rather than relying on
    # LLMCaller.add_tool_result + use_message_history. Reason:
    # llm_messages_builder_with_history only re-uses the sent message_history
    # — it never appends the assistant's response or the tool_result blocks
    # added via add_tool_result. Multi-turn tool use needs us to thread
    # assistant turns + tool turns explicitly.
    #
    # System prompt is the FIRST message in the list (in-band system role).
    # LLMCaller.call accepts a ``system_prompt`` kwarg but in the path v3
    # uses (raw_response + use_message_history), that kwarg is silently
    # dropped — ``llm_messages_builder_with_history`` never prepends a
    # system message. So we own the system message ourselves at messages[0].
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    timeline.append(
        {
            "event": "loop_start",
            "agent_kind": agent_kind,
            "available_tools": [t.get("name") or t.get("function", {}).get("name") for t in tools],
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
        }
    )

    terminal_decision: Decision | None = None

    try:
        while True:
            exhausted, reason = budget.exhausted()
            if exhausted:
                terminal_decision = Decision.budget_exhausted(reason or "budget_cap")
                timeline.append({"event": "budget_exhausted", "reason": reason})
                break

            budget.charge_cycle()
            cycle_start = time.monotonic()

            # Inject the conversation so far into LLMCaller's message_history,
            # then pass prompt=None so it doesn't append a duplicate user turn.
            # Internal LLMCaller behavior; first-class multi-turn API is a
            # follow-up — if LLMCaller.call() ever validates prompt, this
            # silently breaks and tests catch it.
            llm_caller.message_history = list(messages)
            try:
                response = await llm_caller.call(
                    prompt=None,
                    prompt_name=prompt_name,
                    organization_id=organization_id,
                    tools=tools,
                    use_message_history=True,
                    raw_response=True,
                    # system_prompt kwarg intentionally NOT passed — the
                    # message-history path drops it. We inject the system
                    # message at messages[0] above instead.
                )
            except Exception as exc:
                LOG.warning(
                    "v3 agent loop LLM call failed",
                    agent_kind=agent_kind,
                    cycles_used=budget.cycles_used,
                    exc_info=True,
                )
                terminal_decision = Decision.loop_error(f"llm_call_failed: {type(exc).__name__}: {exc}")
                timeline.append(
                    {
                        "event": "llm_call_failed",
                        "cycle": budget.cycles_used,
                        "error": str(exc),
                    }
                )
                break

            llm_latency_ms = (time.monotonic() - cycle_start) * 1000.0
            in_toks, out_toks, cost_usd = _extract_usage(response)
            budget.charge_tokens(in_toks + out_toks)
            budget.charge_cost(cost_usd)
            text = _extract_text(response)
            tool_calls = _extract_tool_calls(response)
            timeline.append(
                {
                    "event": "llm_response",
                    "cycle": budget.cycles_used,
                    "latency_ms": round(llm_latency_ms, 2),
                    "input_tokens": in_toks,
                    "output_tokens": out_toks,
                    "cost_usd": round(cost_usd, 6),
                    "tool_call_count": len(tool_calls),
                    "text_preview": (text[:200] if text else None),
                }
            )

            # Append the assistant turn (including tool_calls) to history.
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": text or None,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for tc_id, name, args in tool_calls
                ]
            messages.append(assistant_msg)

            # No tool calls → LLM stalled (probably just narrating). Push back
            # with a directive user message and try once more; if it stalls
            # again, the cycles cap will end the loop.
            if not tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You must call a tool or a terminal skill (give_up / declare_success / "
                            "declare_review_complete / abandon_post_run / declare_post_run_complete). "
                            "Pick one and emit it now."
                        ),
                    }
                )
                continue

            # Process all tool calls in this turn. If any is terminal, we stop
            # after handling it. Non-terminal tool results are appended as
            # tool messages so the next LLM cycle sees them.
            should_stop = False
            for tc_id, tool_name, args in tool_calls:
                skill = registry.get(tool_name)
                is_terminal_call = tool_name in terminal_names

                # Anti-oscillation gate. Terminal skills are exempt — they
                # end the loop anyway. Investigate / validate skills can
                # repeat legitimately if the agent's reading different
                # things, so loop_tracker is also exempt; only mutating
                # skills (persist) and live_try_* would oscillate, but
                # detect_tool_loop catches the trivial A-A-A regardless.
                loop_warning: str | None = None
                if not is_terminal_call:
                    loop_warning = detect_tool_loop(consecutive_tool_tracker, tool_name)
                    if loop_warning is None:
                        loop_warning = detect_failed_tool_step_loop(failed_step_tracker, tool_name, args)
                if loop_warning is not None:
                    timeline.append(
                        {
                            "event": "loop_detected",
                            "cycle": budget.cycles_used,
                            "tool_call_id": tc_id,
                            "tool_name": tool_name,
                            "warning": loop_warning,
                        }
                    )
                    # Feed the warning back to the LLM as the tool's "result"
                    # so the next cycle sees the loop signal in-line where it
                    # expects the tool's output. Skill is NOT executed.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": tool_name,
                            "content": loop_warning,
                        }
                    )
                    continue

                if skill is None:
                    result = SkillResult.error(f"unknown_tool: {tool_name}")
                    timeline.append(
                        {
                            "event": "tool_unknown",
                            "cycle": budget.cycles_used,
                            "tool_call_id": tc_id,
                            "tool_name": tool_name,
                        }
                    )
                else:
                    skill_start = time.monotonic()
                    result = await skill.execute(args, context)
                    skill_latency_ms = (time.monotonic() - skill_start) * 1000.0
                    timeline.append(
                        {
                            "event": "tool_call",
                            "cycle": budget.cycles_used,
                            "tool_call_id": tc_id,
                            "tool_name": tool_name,
                            "args_preview": {k: _truncate_for_log(v) for k, v in args.items()},
                            "status": result.status,
                            "latency_ms": round(skill_latency_ms, 2),
                        }
                    )

                # Update the failed-step tracker. record_tool_step_result
                # expects a Mapping with "ok" and optional "error"/"data";
                # we adapt SkillResult here. "not_available" is neutral —
                # neither success nor failure — so we skip the update.
                if not is_terminal_call and result.status in {"ok", "error"}:
                    record_tool_step_result(
                        failed_step_tracker,
                        tool_name,
                        args,
                        {
                            "ok": result.status == "ok",
                            "error": result.error_message,
                            "data": result.data if isinstance(result.data, dict) else None,
                        },
                    )

                # Feed result back to the LLM as a tool message so the next
                # cycle's LLM call sees it.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tool_name,
                        "content": result.to_tool_content(),
                    }
                )

                if tool_name in terminal_names and result.status == "ok":
                    built, stop = _build_terminal_decision(
                        tool_name=tool_name,
                        args=args,
                        skill_result=result,
                        builder=terminal_builder,
                    )
                    if built is not None:
                        terminal_decision = built
                    timeline.append(
                        {
                            "event": "terminal_decision",
                            "cycle": budget.cycles_used,
                            "tool_name": tool_name,
                            "decision_type": built.type if built else None,
                            "decision_reason": built.reason if built else None,
                            "should_stop": stop,
                        }
                    )
                    if stop:
                        should_stop = True
                        break

            if should_stop:
                break

    finally:
        if invocation_handle is not None:
            try:
                await invocation_handle.finalize_cost(budget.cost_usd_used)
            except Exception:  # pragma: no cover
                LOG.debug("invocation_handle.finalize_cost failed", exc_info=True)

    # If the loop exited without a terminal we tag it as loop_error so the
    # caller can update the episode with a stable error string.
    if terminal_decision is None:
        terminal_decision = Decision.loop_error("loop_ended_without_terminal")

    timeline.append(
        {
            "event": "loop_end",
            "decision_type": terminal_decision.type,
            "budget": budget.to_metrics(),
        }
    )
    return AgentLoopResult(terminal_decision=terminal_decision, timeline=timeline, budget=budget)


def _build_terminal_decision(
    *,
    tool_name: str,
    args: dict[str, Any],
    skill_result: SkillResult,
    builder: TerminalBuilder | None,
) -> tuple[Decision | None, bool]:
    """Map a terminal tool call to ``(Decision, should_stop)``.

    The custom builder (if provided) controls both fields. Without a builder,
    the loop falls back to the schema-driven default which always sets
    should_stop=True — that's the right default for mid-run (every terminal
    ends the loop) but post-run overrides this to keep the loop running on
    per-episode terminals.
    """
    if builder is not None:
        try:
            built, stop = builder(tool_name, args, skill_result)
            if built is not None or stop is False:
                # Builder produced a decision OR explicitly opted out of stopping.
                return built, stop
        except Exception:  # pragma: no cover
            LOG.warning("v3 terminal_builder raised, falling back to default", exc_info=True)

    data = skill_result.data if isinstance(skill_result.data, dict) else {}
    reason = str(args.get("reason") or data.get("reason") or tool_name)
    summary = args.get("investigation_summary") or data.get("investigation_summary")
    new_rev = data.get("new_script_revision_id")
    applied_fix = data.get("applied_fix_description")
    episode_id = args.get("episode_id") or data.get("episode_id")

    if tool_name == "declare_success":
        return Decision.declare_success(
            reason=reason,
            investigation_summary=summary,
            applied_fix_description=applied_fix if isinstance(applied_fix, dict) else None,
            new_script_revision_id=new_rev,
        ), True
    if tool_name == "give_up":
        return Decision.give_up(reason), True
    if tool_name == "declare_review_complete":
        if not episode_id:
            LOG.warning("declare_review_complete without episode_id; falling back to give_up")
            return Decision.give_up("declare_review_complete_without_episode_id"), True
        return (
            Decision.declare_review_complete(
                episode_id=str(episode_id),
                investigation_summary=summary,
                new_script_revision_id=new_rev,
            ),
            True,
        )
    if tool_name == "give_up_episode":
        if not episode_id:
            return Decision.give_up("give_up_episode_without_episode_id"), True
        return Decision.give_up_episode(str(episode_id), reason), True
    if tool_name == "demote_class_a":
        if not episode_id:
            return Decision.give_up("demote_class_a_without_episode_id"), True
        return Decision.demote_class_a(str(episode_id), reason), True
    if tool_name == "declare_post_run_complete":
        return Decision.declare_post_run_complete(reason, investigation_summary=summary), True
    if tool_name == "abandon_post_run":
        return Decision.abandon_post_run(reason), True
    return None, True


def _truncate_for_log(value: Any, max_chars: int = 200) -> Any:
    """Cap long string args before adding to timeline. Defends timeline JSON size."""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"... <truncated {len(value) - max_chars} chars>"
    return value


__all__ = ["AgentLoopResult", "run_agent_loop"]
