from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, Literal

from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.forge_log import current_codeblock_log_redactor

from ._common import ErrorCode, make_error

if TYPE_CHECKING:
    from skyvern.core.script_generations.skyvern_page import SkyvernPage

DEFAULT_ACTION_TIMEOUT_MS = 30000
DEFAULT_DIRECT_ACTION_TIMEOUT_MS = 5000
DIRECT_ACTION_TIMEOUT_ENV = "SKYVERN_MCP_DIRECT_TIMEOUT_MS"
MIN_ACTION_TIMEOUT_MS = 1000
MAX_ACTION_TIMEOUT_MS = 60000
# The probe reports why an action failed, and every timeout here degrades to the useless "unknown"
# state. It only runs after an action already failed, so a wider budget costs nothing on the happy
# path and buys an accurate reason on the path that needs one.
ELEMENT_STATE_PROBE_TIMEOUT_MS = 3000
ELEMENT_STATE_ERROR_DETAIL_MAX_CHARS = 500

ACTION_TIMEOUT_DESCRIPTION = (
    "Max time to wait for the element in ms. "
    "Defaults to 5000 for deterministic selector-only/direct calls and 30000 for AI/fallback paths."
)

ElementState = Literal["not_found", "hidden", "disabled", "occluded", "unknown"]

_STATE_ERRORS: dict[ElementState, tuple[str, str, str]] = {
    "not_found": (
        ErrorCode.SELECTOR_NOT_FOUND,
        "Selector did not match any element before the direct action timeout",
        "The page may have re-rendered since it was inspected, which invalidates a selector captured "
        "earlier. Inspect the page again and act on a freshly reported selector rather than retrying "
        "this one.",
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
        "The element may not have settled, or the page may have re-rendered since it was inspected. "
        "Inspect the page again and act on a freshly reported selector.",
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


def _exception_text(exc: BaseException) -> str:
    """A user-defined __str__ can raise or return a non-str; reading it must not raise here."""
    try:
        return str(exc)
    except BaseException:
        return ""


def is_pointer_interception_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "intercepts pointer events" in message or "intercepted by another element" in message


async def _click_point_is_covered(locator: Any) -> bool:
    """True when a different element sits at the target's click point, i.e. the click cannot land."""
    try:
        return bool(
            await locator.evaluate(
                "el => { const r = el.getBoundingClientRect();"
                " if (!r.width || !r.height) return false;"
                " const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
                " return !!top && top !== el && !el.contains(top); }"
            )
        )
    except Exception:
        return False


async def classify_element_state(
    page: SkyvernPage, selector: str, *, pointer_intercepted: bool = False
) -> ElementState:
    async def _probe() -> ElementState:
        # Frame-aware actions resolve against the working iframe when one is set; the probe must
        # query the same scope or an iframe failure misclassifies as not_found.
        locator = page.locator_scope.locator(selector)
        if await locator.count() == 0:
            return "not_found"
        first = locator.first
        if not await first.is_visible():
            return "hidden"
        if not await first.is_enabled():
            return "disabled"
        if pointer_intercepted:
            return "occluded"
        # Playwright reports an occluded target as a plain action timeout, so the caller cannot
        # flag interception. Ask the page what actually sits at the click point instead.
        if await _click_point_is_covered(first):
            return "occluded"
        return "unknown"

    try:
        return await asyncio.wait_for(_probe(), timeout=ELEMENT_STATE_PROBE_TIMEOUT_MS / 1000)
    except Exception:
        # This runs while an action failure is already being reported, so a stale-scope refusal
        # degrades the reason to "unknown" instead of replacing a handled failure with a crash.
        return "unknown"


def _redacted_exception_detail(exc: Exception) -> str:
    # Both redactors match on the text, so bounding first would leave a cut secret unmatched.
    text = _exception_text(exc).strip()
    redactor = current_codeblock_log_redactor()
    if redactor is not None:
        # forge_log.py guards the same call: a raising redactor must not turn a handled
        # actionability failure into an unhandled one, and it must not fall through unredacted.
        try:
            redacted = redactor(text)
        except BaseException:
            redacted = ""
        text = redacted if isinstance(redacted, str) else ""
    return redact_raw_secrets_for_prompt(text)[:ELEMENT_STATE_ERROR_DETAIL_MAX_CHARS]


def element_state_error(state: ElementState, exc: Exception, *, selector: str, timeout_ms: int) -> dict[str, Any]:
    code, message, hint = _STATE_ERRORS[state]
    details: dict[str, Any] = {
        "element_state": state,
        "selector": selector,
        "actionability_timeout_ms": timeout_ms,
        "exception_type": type(exc).__name__,
    }
    detail = _redacted_exception_detail(exc)
    if detail:
        details["exception_detail"] = detail
    return make_error(code, message, hint, details=details, exc=exc)


async def make_direct_action_error(page: Any, selector: str, exc: Exception, *, timeout_ms: int) -> dict[str, Any]:
    state = await classify_element_state(page, selector, pointer_intercepted=is_pointer_interception_error(exc))
    return element_state_error(state, exc, selector=selector, timeout_ms=timeout_ms)
