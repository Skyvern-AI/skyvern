"""RunHooks for copilot tool activity recording."""

from __future__ import annotations

import json
from typing import Any

from agents.agent import Agent
from agents.lifecycle import RunHooksBase
from agents.run_context import RunContextWrapper
from agents.tool import Tool

from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result
from skyvern.forge.sdk.copilot.streaming_adapter import _parse_tool_output


class CopilotRunHooks(RunHooksBase):
    """Record tool activity for StructuredContext.merge_turn_summary()."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def on_tool_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
        result: Any,
    ) -> None:
        tool_name = tool.name
        parsed = _parse_tool_output(result)
        summary = summarize_tool_result(tool_name, parsed)

        activity_entry: dict[str, Any] = {"tool": tool_name, "summary": summary}

        if tool_name in ("run_blocks_and_collect_debug", "get_run_results") and parsed.get("ok"):
            data = parsed.get("data") or {}
            blocks = data.get("blocks", []) if isinstance(data, dict) else []
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
