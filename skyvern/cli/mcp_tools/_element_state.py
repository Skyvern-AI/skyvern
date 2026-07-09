from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from ._common import ErrorCode, make_error

DEFAULT_ACTION_TIMEOUT_MS = 30000
DEFAULT_DIRECT_ACTION_TIMEOUT_MS = 5000
DIRECT_ACTION_TIMEOUT_ENV = "SKYVERN_MCP_DIRECT_TIMEOUT_MS"
MIN_ACTION_TIMEOUT_MS = 1000
MAX_ACTION_TIMEOUT_MS = 60000
ELEMENT_STATE_PROBE_TIMEOUT_MS = 1000

ACTION_TIMEOUT_DESCRIPTION = (
    "Max time to wait for the element in ms. "
    "Defaults to 5000 for deterministic selector-only/direct calls and 30000 for AI/fallback paths."
)

ElementState = Literal["not_found", "hidden", "disabled", "occluded", "unknown"]

_STATE_ERRORS: dict[ElementState, tuple[str, str, str]] = {
    "not_found": (
        ErrorCode.SELECTOR_NOT_FOUND,
        "Selector did not match any element before the direct action timeout",
        "Verify the selector matches an element on the page, or wait for the element to render before retrying.",
    ),
    "hidden": (
        ErrorCode.ACTION_FAILED,
        "Selector matched an element that is not visible",
        "The element exists but is not visible; it may be display:none or inside a collapsed container.",
    ),
    "disabled": (
        ErrorCode.ACTION_FAILED,
        "Selector matched a disabled element",
        "The element exists but is disabled; wait for it to become enabled or choose an enabled control.",
    ),
    "occluded": (
        ErrorCode.ACTION_FAILED,
        "Selector matched an element blocked by another element",
        "Another element is intercepting the action; close overlays, scroll, or target the visible control.",
    ),
    "unknown": (
        ErrorCode.ACTION_FAILED,
        "Direct action failed before the element became actionable",
        "Re-check the selector and page state, or use an intent-based action if the target is dynamic.",
    ),
}


def is_direct_action(selector: str | None, ai_mode: str | None, *, deterministic: bool = False) -> bool:
    return selector is not None and (ai_mode is None or deterministic)


def _direct_action_timeout_default_ms() -> int:
    raw_value = os.environ.get(DIRECT_ACTION_TIMEOUT_ENV)
    if raw_value is None:
        return DEFAULT_DIRECT_ACTION_TIMEOUT_MS
    try:
        timeout = int(raw_value)
    except ValueError:
        return DEFAULT_DIRECT_ACTION_TIMEOUT_MS
    return max(MIN_ACTION_TIMEOUT_MS, min(MAX_ACTION_TIMEOUT_MS, timeout))


def resolve_action_timeout_ms(timeout: int | None, *, direct_action: bool) -> int:
    if timeout is not None:
        return timeout
    return _direct_action_timeout_default_ms() if direct_action else DEFAULT_ACTION_TIMEOUT_MS


def is_pointer_interception_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "intercepts pointer events" in message or "intercepted by another element" in message


async def classify_element_state(page: Any, selector: str, *, pointer_intercepted: bool = False) -> ElementState:
    # Frame-aware actions resolve against SkyvernPage._locator_scope (working iframe if set);
    # the probe must query the same root or an iframe failure misclassifies as not_found.
    probe_root = getattr(page, "_locator_scope", None)
    if probe_root is None:
        probe_root = page.page

    async def _probe() -> ElementState:
        locator = probe_root.locator(selector)
        if await locator.count() == 0:
            return "not_found"
        first = locator.first
        if not await first.is_visible():
            return "hidden"
        if not await first.is_enabled():
            return "disabled"
        if pointer_intercepted:
            return "occluded"
        return "unknown"

    try:
        return await asyncio.wait_for(_probe(), timeout=ELEMENT_STATE_PROBE_TIMEOUT_MS / 1000)
    except Exception:
        return "unknown"


def element_state_error(state: ElementState, exc: Exception, *, selector: str, timeout_ms: int) -> dict[str, Any]:
    code, message, hint = _STATE_ERRORS[state]
    return make_error(
        code,
        message,
        hint,
        details={
            "element_state": state,
            "selector": selector,
            "actionability_timeout_ms": timeout_ms,
            "exception_type": type(exc).__name__,
        },
    )


async def make_direct_action_error(page: Any, selector: str, exc: Exception, *, timeout_ms: int) -> dict[str, Any]:
    state = await classify_element_state(page, selector, pointer_intercepted=is_pointer_interception_error(exc))
    return element_state_error(state, exc, selector=selector, timeout_ms=timeout_ms)
