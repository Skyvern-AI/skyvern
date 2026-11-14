from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skyvern.forge.sdk.copilot.blocker_signal import maybe_clear_blocker_signal_on_tool_success


def record_tool_step_result_for_ctx(
    ctx: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    result: Mapping[str, Any],
) -> None:
    # Strict ``is True`` check: a malformed result dict missing ``ok`` entirely
    # must not be treated as success and accidentally clear a blocker signal.
    if result.get("ok") is True:
        maybe_clear_blocker_signal_on_tool_success(ctx, tool_name)
