"""RunHooks for copilot tool activity recording."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog
from agents.agent import Agent, AgentBase
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import RunHooksBase
from agents.run_context import AgentHookContext, RunContextWrapper
from agents.tool import Tool

from skyvern.forge.sdk.copilot.browser_ablation import prompt_sha256
from skyvern.forge.sdk.copilot.credential_pause import arm_credential_pause_gate
from skyvern.forge.sdk.copilot.enforcement import (
    gate_decision_trace_fields,
    outcome_fully_verified,
)
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_gate_decision
from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result
from skyvern.forge.sdk.copilot.streaming_adapter import parse_tool_output
from skyvern.forge.sdk.copilot.turn_halt import (
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

LOG = structlog.get_logger()

# Tools whose block-level outputs are worth summarizing onto the activity
# entry as a short preview. This is a hooks-side concern (what to record),
# not a registry of the tools themselves.
_BLOCK_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {"run_blocks_and_collect_debug", "get_run_results", "update_and_run_blocks", "edit_block_and_run"}
)
# The tools that dispatch a build test. Counted per model response so the acquisition seam can tell
# a stale run of an earlier turn from a sibling call the same response is running right now.
_BUILD_TEST_DISPATCH_TOOLS: frozenset[str] = frozenset(
    {"run_blocks_and_collect_debug", "update_and_run_blocks", "edit_block_and_run"}
)
_VERIFIED_GOAL_CONTEXT_ATTRS: frozenset[str] = frozenset(
    {
        "last_test_ok",
        "last_full_workflow_test_ok",
        "last_update_block_count",
        "latest_diagnosis_repair_contract",
        "completion_verification_result",
        "user_message",
    }
)


def _copilot_log_fields(ctx: CopilotContext) -> dict[str, str | None]:
    return {
        "workflow_permanent_id": getattr(ctx, "workflow_permanent_id", None),
        "turn_id": getattr(ctx, "turn_id", None),
        "workflow_copilot_chat_id": getattr(ctx, "workflow_copilot_chat_id", None),
    }


def _tool_completion_satisfies_turn(ctx: CopilotContext, tool_name: str, parsed: Mapping[str, object]) -> bool:
    if tool_name not in {"run_blocks_and_collect_debug", "update_and_run_blocks", "edit_block_and_run"}:
        return False
    if not all(hasattr(ctx, attr) for attr in _VERIFIED_GOAL_CONTEXT_ATTRS):
        return False
    # An unfinished run (ok != True) can still satisfy the turn when the outcome
    # judge confirmed the goal from evidence: recognition must not key on run status.
    if parsed.get("ok") is not True and not outcome_fully_verified(ctx):
        return False
    gate_fields = gate_decision_trace_fields(ctx)
    record_gate_decision(ctx, gate_fields)
    return gate_fields["gate_satisfied"]


class CopilotRunHooks(RunHooksBase):
    """Record tool activity for StructuredContext.merge_turn_summary()."""

    def __init__(self, ctx: CopilotContext) -> None:
        self._ctx = ctx

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        try:
            self._ctx.model_calls_this_turn += 1
            if self._ctx.eval_mode == "browser_ablation" and isinstance(system_prompt, str):
                self._ctx.eval_prompt_sha256 = prompt_sha256(system_prompt)
        except Exception:
            LOG.warning(
                "CopilotRunHooks.on_llm_start counting failed",
                **_copilot_log_fields(self._ctx),
            )

    async def on_llm_end(self, context: RunContextWrapper, agent: Agent, response: ModelResponse) -> None:
        if self._ctx.check_model_work_deadline is not None:
            self._ctx.check_model_work_deadline()
        try:
            called = [getattr(item, "name", None) for item in response.output]
            # Counted before the credential gate: a gate that raises would otherwise leave the
            # previous response's count standing, and the acquisition seam reads it as this one's.
            self._ctx.build_test_tool_calls_in_model_response = sum(
                1 for name in called if name in _BUILD_TEST_DISPATCH_TOOLS
            )
            if "request_credential" in called:
                arm_credential_pause_gate(self._ctx)
        except Exception:
            LOG.warning(
                "CopilotRunHooks.on_llm_end response accounting failed",
                **_copilot_log_fields(self._ctx),
            )

    async def on_agent_start(self, context: AgentHookContext, agent: AgentBase) -> None:
        try:
            self._ctx.enforcement_pass_count += 1
        except Exception:
            LOG.warning(
                "CopilotRunHooks.on_agent_start counting failed",
                **_copilot_log_fields(self._ctx),
            )

    async def on_tool_start(
        self,
        context: RunContextWrapper,
        agent: AgentBase,
        tool: Tool,
    ) -> None:
        # Retry safety depends on this monotonic fact, so record it before the
        # tool executes and outside the best-effort activity-summary path.
        self._ctx.tool_calls_this_turn += 1

    async def on_tool_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
        result: Any,
    ) -> None:
        if self._ctx.check_model_work_deadline is not None:
            self._ctx.check_model_work_deadline()
        # Activity recording is observability -- a malformed tool result or an
        # unserializable block output must not propagate into the agent loop
        # and kill the whole run.
        try:
            tool_name = tool.name
            parsed = parse_tool_output(result)
            summary = summarize_tool_result(tool_name, parsed)

            activity_entry: dict[str, Any] = {"tool": tool_name, "summary": summary}
            LOG.info(
                "copilot tool completed",
                tool_name=tool_name,
                ok=parsed.get("ok"),
                summary=summary,
                total_tokens=getattr(self._ctx, "total_tokens_used", None),
                **_copilot_log_fields(self._ctx),
            )

            if tool_name == "list_credentials" and parsed.get("ok"):
                data = parsed.get("data") or {}
                listed = data.get("credentials", []) if isinstance(data, dict) else []
                if not isinstance(listed, list):
                    listed = []
                exact = data.get("credential") if isinstance(data, dict) else None
                if not listed and isinstance(exact, dict):
                    listed = [exact]
                resolved = [
                    {"credential_id": c.get("credential_id"), "name": c.get("name")}
                    for c in listed
                    if isinstance(c, dict) and isinstance(c.get("credential_id"), str)
                ]
                if resolved:
                    activity_entry["credentials"] = resolved

            if tool_name == "list_integrations" and parsed.get("ok"):
                data = parsed.get("data") or {}
                listed = data.get("integrations", []) if isinstance(data, dict) else []
                if not isinstance(listed, list):
                    listed = []
                integrations = [
                    {
                        "connection_id": integration.get("connection_id"),
                        "provider": integration.get("provider"),
                        "state": integration.get("state"),
                        "scopes_granted": integration.get("scopes_granted"),
                    }
                    for integration in listed
                    if isinstance(integration, dict)
                    and isinstance(integration.get("connection_id"), str)
                    and isinstance(integration.get("provider"), str)
                    and isinstance(integration.get("state"), str)
                    and isinstance(integration.get("scopes_granted"), list)
                ]
                activity_entry["integrations"] = integrations

            if tool_name in _BLOCK_OUTPUT_TOOLS and parsed.get("ok"):
                data = parsed.get("data") or {}
                blocks = data.get("blocks", []) if isinstance(data, dict) else []
                if not isinstance(blocks, list):
                    blocks = []
                output_parts = []
                for b in blocks:
                    if b.get("output") or b.get("extracted_data"):
                        out = b.get("output") or b.get("extracted_data")
                        out_str = json.dumps(out, default=str) if not isinstance(out, str) else out
                        if len(out_str) > 500:
                            out_str = out_str[:500] + "..."
                        output_parts.append(f"{b.get('label', '?')}: {out_str}")
                if output_parts:
                    activity_entry["output_preview"] = "; ".join(output_parts)

            self._ctx.tool_activity.append(activity_entry)
        except Exception:
            LOG.warning(
                "CopilotRunHooks.on_tool_end recording failed, skipping entry",
                tool=getattr(tool, "name", None),
                **_copilot_log_fields(self._ctx),
            )
            return

        stash_turn_halt_from_blocker_signal(
            self._ctx,
            getattr(self._ctx, "latest_tool_blocker_signal", None) or getattr(self._ctx, "blocker_signal", None),
            source="hook",
        )
        raise_if_turn_halt(self._ctx, verified=outcome_fully_verified(self._ctx))

        if _tool_completion_satisfies_turn(self._ctx, tool_name, parsed):
            # The run record satisfying the ask is the model's cue to wrap up, not the
            # harness's cue to end the loop and speak for it.
            LOG.info(
                "copilot tool satisfied goal; model composes the reply",
                tool_name=tool_name,
                workflow_run_id=(parsed.get("data") or {}).get("workflow_run_id")
                if isinstance(parsed.get("data"), dict)
                else None,
                **_copilot_log_fields(self._ctx),
            )
            self._ctx.goal_satisfied_tool_name = tool_name
            self._ctx.goal_satisfied_tool_output = dict(parsed)
