"""Generic main-world JS evaluation hook.

When a context has a prefix configured via ``configure_main_world_prefix``,
``evaluate_in_main_world`` runs the script via a single CDP ``Runtime.evaluate``
call with the prefix prepended, so middleware that inspects script content sees
the prefix intact. With no prefix configured the call falls through to
``page.evaluate`` with no extra CDP overhead. The prefix is opaque text.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import re
import sys
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import BrowserContext, Page

_CONTEXT_PREFIXES: weakref.WeakKeyDictionary[BrowserContext, str] = weakref.WeakKeyDictionary()

_MAIN_WORLD_DEFAULT_METHODS: dict[str, tuple[str, ...]] = {
    "Page": ("evaluate", "evaluate_handle"),
    "Frame": ("evaluate", "evaluate_handle"),
    "Worker": ("evaluate", "evaluate_handle"),
    "Locator": ("evaluate", "evaluate_handle", "evaluate_all"),
}
_MAIN_WORLD_DEFAULT_WRAPPERS: set[Callable[..., Awaitable[Any]]] = set()


def _with_main_world_default(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(method)
    async def evaluate(self: Any, *args: Any, isolated_context: bool = False, **kwargs: Any) -> Any:
        return await method(self, *args, isolated_context=isolated_context, **kwargs)

    _MAIN_WORLD_DEFAULT_WRAPPERS.add(evaluate)
    return evaluate


def default_evaluate_to_main_world(driver: Any) -> None:
    """Make the driver's ``evaluate`` family default to the page's main world. Idempotent."""
    # Some drivers default these to an isolated world that cannot see page globals or init-script state
    # unless the caller passes ``isolated_context=False``; drivers without that parameter use the main world.
    api = sys.modules.get(type(driver).__module__)
    for class_name, method_names in _MAIN_WORLD_DEFAULT_METHODS.items():
        cls = getattr(api, class_name, None)
        if not isinstance(cls, type):
            continue
        for method_name in method_names:
            method = cls.__dict__.get(method_name)
            if not callable(method) or method in _MAIN_WORLD_DEFAULT_WRAPPERS:
                continue
            if "isolated_context" not in inspect.signature(method).parameters:
                continue
            setattr(cls, method_name, _with_main_world_default(method))


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
# After leading whitespace + line/block comments are stripped, does the body
# begin with a token that re-declaring on a repeated injection would actually
# fail? Only the lexical bindings (``let``/``const``/``class``) need
# ``replMode`` — re-declaring a top-level ``var`` or ``function`` is legal
# in JS, so forcing replMode on them just risks the Chromium
# ``returnByValue`` -> ``{}`` envelope rewrite for value-returning bodies
# like ``function rows() {...}; rows()``.
_TOP_LEVEL_DECL_TOKEN_RE = re.compile(r"(?:let|const|class)\b")


def _is_function_form(expression: str) -> bool:
    return bool(_ARROW_FN_RE.match(expression) or _FUNCTION_DECL_RE.match(expression))


def _starts_with_top_level_declaration(body: str) -> bool:
    """True if ``body`` begins with a top-level declaration token after any
    leading whitespace, line (``//``) and block (``/* */``) comments."""
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if body.startswith("//", i):
            nl = body.find("\n", i + 2)
            if nl == -1:
                return False
            i = nl + 1
            continue
        if body.startswith("/*", i):
            end = body.find("*/", i + 2)
            if end == -1:
                return False
            i = end + 2
            continue
        break
    return bool(_TOP_LEVEL_DECL_TOKEN_RE.match(body, i))


def _extract_runtime_result(result: dict[str, Any]) -> Any:
    # CDP returns ``value`` for JSON-serialisable types; unserialisable primitives
    # (NaN/Infinity/-0/BigInt) fall through to None, matching what page.evaluate
    # gives callers that don't opt into custom serialisation.
    result_obj = result.get("result") or {}
    if "value" in result_obj:
        return result_obj["value"]
    return None


async def evaluate_in_main_world(
    page: Page,
    expression: str,
    arg: Any = None,
    *,
    force_cdp: bool = False,
) -> Any:
    """Evaluate ``expression`` in the page main world when a prefix is configured.

    No prefix → identical to ``page.evaluate(expression, arg)``. Prefix → single
    ``Runtime.evaluate`` call so middleware sees the prefix intact. Function-form
    expressions are auto-wrapped as IIFEs; ``arg`` is inlined as a JSON literal.
    Non-function expressions drop ``arg`` (mirroring page.evaluate). page.evaluate
    is avoided here because Playwright's function-string normalisation requires
    an unprefixed function head, which a leading prefix line breaks.
    """
    prefix = _resolve_prefix(page)
    if prefix is None and not force_cdp:
        if arg is None:
            return await page.evaluate(expression)
        return await page.evaluate(expression, arg)

    is_function_form = _is_function_form(expression)
    if is_function_form:
        if arg is None:
            wrapped = f"({expression})()"
        else:
            wrapped = f"({expression})({json.dumps(arg)})"
    else:
        wrapped = expression
    body = f"{prefix or ''}\n{wrapped}"
    # ``replMode`` only for statement-form bodies that begin with a lexical
    # top-level declaration (``let``/``const``/``class``). Bare expressions
    # and value-returning forms must skip it — Chromium rewrites their
    # ``returnByValue`` envelope to ``{}`` under replMode. The check runs
    # against ``body`` so a prefix that itself carries declarations is
    # covered too.
    needs_repl_mode = not is_function_form and _starts_with_top_level_declaration(body)
    cdp_session = await page.context.new_cdp_session(page)
    try:
        params: dict[str, Any] = {
            "expression": body,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if needs_repl_mode:
            params["replMode"] = True
        result = await cdp_session.send("Runtime.evaluate", params)
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
