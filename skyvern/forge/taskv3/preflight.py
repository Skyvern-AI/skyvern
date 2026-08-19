"""Observe-only wiring of the browser-action policy onto the Task V3 raw-tool path.

The step engine routes every browser action through ``preflight_action``
(:mod:`skyvern.forge.sdk.browser_action_preflight`, SKY-12874) before it executes; the v3 tools
drive Playwright directly and would otherwise bypass that seam entirely. This mirrors the step
engine's observe hook so v3 actions are visible to the policy — and so the seam already exists on
this path when enforcement lands at the sinks (SKY-12881+); a v3 action must not become a silent
hole in a control the step engine passes through.

It is observe-only and gated by ``BROWSER_ACTION_POLICY_MODE`` (disabled by default), so it is a
no-op until the policy is enabled and never changes execution: the gate is checked before any
action is built, ``preflight_action`` swallows its own failures, and the returned decision is
discarded. v3 does not scrape, so element actions carry no observation provenance (they surface as
unstamped telemetry); a ``navigate`` URL is a model-declared PAGE target the origin check can act on.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge.sdk.browser_action_preflight import policy_observation_enabled, preflight_action
from skyvern.webeye.actions.actions import (
    Action,
    ClickAction,
    GotoUrlAction,
    InputTextAction,
    KeypressAction,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
)

LOG = structlog.get_logger()

# The tools that carry a browser action the policy can reason about — exactly the names
# `_build_action` maps. This is the single source of truth for which tool handlers get wrapped;
# `build_browser_tools` imports it rather than keeping a second list that can drift.
PREFLIGHT_TOOL_NAMES = frozenset(
    {"click", "type", "select_combobox", "select_option", "press_key", "file_upload", "navigate"}
)


def _build_action(tool_name: str, args: dict[str, Any]) -> Action | None:
    selector = str(args.get("selector") or "")
    if tool_name == "click":
        return ClickAction(element_id=selector)
    if tool_name == "type":
        return InputTextAction(element_id=selector, text=str(args.get("text", "")))
    if tool_name == "select_combobox":
        return InputTextAction(element_id=selector, text=str(args.get("value", "")))
    if tool_name == "select_option":
        return SelectOptionAction(
            element_id=selector, option=SelectOption(value=args.get("value"), label=args.get("label"))
        )
    if tool_name == "press_key":
        key = args.get("key")
        return KeypressAction(keys=[str(key)] if key else [])
    if tool_name == "file_upload":
        return UploadFileAction(element_id=selector, file_url=str(args.get("file") or ""))
    if tool_name == "navigate":
        url = args.get("url")
        return GotoUrlAction(url=str(url)) if url else None
    return None


def preflight_tool_action(tool_name: str, args: dict[str, Any], page: Any) -> None:
    """Run the observe-only browser-action policy for one v3 tool call before it executes.

    No-op unless ``BROWSER_ACTION_POLICY_MODE`` enables observation; never raises."""
    if not policy_observation_enabled():
        return
    try:
        action = _build_action(tool_name, args)
        if action is not None:
            preflight_action(action, page, site=f"taskv3-{tool_name}")
    except Exception:
        LOG.debug("taskv3 preflight skipped", tool=tool_name, exc_info=True)
