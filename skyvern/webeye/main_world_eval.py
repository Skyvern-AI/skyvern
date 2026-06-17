"""Generic main-world JS evaluation hook.

When a context has a prefix configured via ``configure_main_world_prefix``,
``evaluate_in_main_world`` runs the script via a single CDP ``Runtime.evaluate``
call with the prefix prepended, so middleware that inspects script content sees
the prefix intact. With no prefix configured the call falls through to
``page.evaluate`` with no extra CDP overhead. The prefix is opaque text.
"""

from __future__ import annotations

import contextlib
import json
import re
import weakref
from typing import Any

from playwright.async_api import BrowserContext, Page

_CONTEXT_PREFIXES: weakref.WeakKeyDictionary[BrowserContext, str] = weakref.WeakKeyDictionary()


def configure_main_world_prefix(context: BrowserContext, prefix: str) -> None:
    """Attach an opaque text prefix to ``context``; prepended to every JS body."""
    _CONTEXT_PREFIXES[context] = prefix


def clear_main_world_prefix(context: BrowserContext) -> None:
    _CONTEXT_PREFIXES.pop(context, None)


def get_main_world_prefix(context: BrowserContext) -> str | None:
    return _CONTEXT_PREFIXES.get(context)


def _resolve_prefix(page: Page) -> str | None:
    return _CONTEXT_PREFIXES.get(page.context)


# Conservative: only arrow / function declarations that Playwright would auto-wrap
# as an IIFE. Bare expressions / object literals must NOT be wrapped.
# Limitation: arrow param list is ``[^()]*``, so nested-paren params (e.g.
# ``(a = (1+2)) => a``) don't match — current callers don't use that shape.
_ARROW_FN_RE = re.compile(r"^\s*(async\s+)?(\([^()]*\)|\w+)\s*=>")
_FUNCTION_DECL_RE = re.compile(r"^\s*(async\s+)?function\b")


def _is_function_form(expression: str) -> bool:
    return bool(_ARROW_FN_RE.match(expression) or _FUNCTION_DECL_RE.match(expression))


def _extract_runtime_result(result: dict[str, Any]) -> Any:
    # CDP returns ``value`` for JSON-serialisable types; unserialisable primitives
    # (NaN/Infinity/-0/BigInt) fall through to None, matching what page.evaluate
    # gives callers that don't opt into custom serialisation.
    result_obj = result.get("result") or {}
    if "value" in result_obj:
        return result_obj["value"]
    return None


async def evaluate_in_main_world(page: Page, expression: str, arg: Any = None) -> Any:
    """Evaluate ``expression`` in the page main world when a prefix is configured.

    No prefix → identical to ``page.evaluate(expression, arg)``. Prefix → single
    ``Runtime.evaluate`` call so middleware sees the prefix intact. Function-form
    expressions are auto-wrapped as IIFEs; ``arg`` is inlined as a JSON literal.
    Non-function expressions drop ``arg`` (mirroring page.evaluate). page.evaluate
    is avoided here because Playwright's function-string normalisation requires
    an unprefixed function head, which a leading prefix line breaks.
    """
    prefix = _resolve_prefix(page)
    if prefix is None:
        if arg is None:
            return await page.evaluate(expression)
        return await page.evaluate(expression, arg)

    if _is_function_form(expression):
        if arg is None:
            wrapped = f"({expression})()"
        else:
            wrapped = f"({expression})({json.dumps(arg)})"
    else:
        wrapped = expression
    body = f"{prefix}\n{wrapped}"
    cdp_session = await page.context.new_cdp_session(page)
    try:
        result = await cdp_session.send(
            "Runtime.evaluate",
            {
                "expression": body,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
    finally:
        with contextlib.suppress(Exception):
            await cdp_session.detach()

    exception_details = result.get("exceptionDetails")
    if exception_details:
        exception = exception_details.get("exception") or {}
        description = (
            exception.get("description")
            or exception.get("value")
            or exception_details.get("text", "Runtime.evaluate exception")
        )
        raise RuntimeError(f"main-world evaluate raised: {description}")

    return _extract_runtime_result(result)
