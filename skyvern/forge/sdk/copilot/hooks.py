"""RunHooks for copilot tool activity recording."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from agents.agent import Agent
from agents.lifecycle import RunHooksBase
from agents.run_context import RunContextWrapper
from agents.tool import Tool

from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result
from skyvern.forge.sdk.copilot.streaming_adapter import parse_tool_output

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()

# Tools whose block-level outputs are worth summarizing onto the activity
# entry as a short preview. This is a hooks-side concern (what to record),
# not a registry of the tools themselves.
_BLOCK_OUTPUT_TOOLS: frozenset[str] = frozenset(
    {"run_blocks_and_collect_debug", "get_run_results", "update_and_run_blocks"}
)


class CopilotRunHooks(RunHooksBase):
    """Record tool activity for StructuredContext.merge_turn_summary()."""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx

    async def on_tool_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
        result: Any,
    ) -> None:
        # Activity recording is observability -- a malformed tool result or an
        # unserializable block output must not propagate into the agent loop
        # and kill the whole run.
        try:
            tool_name = tool.name
            parsed = parse_tool_output(result)
            summary = summarize_tool_result(tool_name, parsed)

            activity_entry: dict[str, Any] = {"tool": tool_name, "summary": summary}

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
                exc_info=True,
            )
