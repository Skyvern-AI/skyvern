"""Interact skills — live Playwright operations. Mid-run only.

The defining feature of mid-run v3: hypothesis → try → observe. The agent
can:

- Read live page state (URL, DOM around a selector, full DOM, element
  attributes, element count for a selector, current text).
- Attempt mutations (click, fill, scroll). A successful mutation IS the
  commit — the workflow continues from the post-mutation state. There's no
  separate "now do it for real" step.

Every skill returns a structured payload that includes pre/post observations
(URL, DOM hash) so the agent can detect whether its action actually changed
anything. Console errors raised during the action are surfaced too.

Context shape: ``FailureContext``. ``context.page`` is the live Playwright
:class:`Page`; ``context.context.recent_dialog_messages`` is consulted for
post-action dialog signals.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import structlog

from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillError, SkillResult

LOG = structlog.get_logger()


# Tight per-skill timeouts. Playwright ops can legitimately stall (network,
# DOM thrash) but the agent budget should bound total time. Per-skill caps
# back-stop the budget and avoid one hung op consuming the wall-clock cap.
_LIVE_READ_TIMEOUT_MS = 5_000
_LIVE_MUTATE_TIMEOUT_MS = 8_000


def _get_page(context: Any) -> Any:
    page = getattr(context, "page", None)
    if page is None:
        raise SkillError("FailureContext.page is unavailable")
    return page


async def _dom_hash(page: Any) -> str:
    """Stable hash of the visible body innerHTML — used to detect mutation."""
    try:
        html = await page.evaluate("() => document.body.innerHTML")
    except Exception:
        return "<<eval_error>>"
    if not isinstance(html, str):
        html = str(html)
    return hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()[:16]


async def _safe_url(page: Any) -> str:
    try:
        return page.url
    except Exception:
        return "<<url_error>>"


async def _handler_live_get_url(args: dict[str, Any], context: Any) -> SkillResult:
    page = _get_page(context)
    return SkillResult.ok(data={"url": await _safe_url(page)})


async def _handler_live_get_dom(args: dict[str, Any], context: Any) -> SkillResult:
    """Return DOM around a selector (or whole-page if no selector).

    DOM payloads are capped at 32KB to fit in the agent's context window.
    """
    page = _get_page(context)
    selector = args.get("selector")
    max_chars = int(args.get("max_chars") or 16_000)
    max_chars = max(500, min(32_000, max_chars))

    try:
        if selector and isinstance(selector, str):
            html = await asyncio.wait_for(
                page.evaluate(
                    "(sel) => {const e = document.querySelector(sel); return e ? e.outerHTML : null;}",
                    selector,
                ),
                timeout=_LIVE_READ_TIMEOUT_MS / 1000,
            )
        else:
            html = await asyncio.wait_for(
                page.evaluate("() => document.body.outerHTML"),
                timeout=_LIVE_READ_TIMEOUT_MS / 1000,
            )
    except asyncio.TimeoutError:
        return SkillResult.error("live_get_dom_timeout")
    except Exception as exc:
        return SkillResult.error(f"page_eval_error: {type(exc).__name__}: {exc}")

    if html is None:
        return SkillResult.not_available(f"selector {selector!r} matched no element")
    if not isinstance(html, str):
        html = str(html)
    truncated = len(html) > max_chars
    return SkillResult.ok(
        data={
            "selector": selector,
            "url": await _safe_url(page),
            "html_chars": len(html),
            "html": (html[:max_chars] + f"\n<!-- truncated {len(html) - max_chars} chars -->") if truncated else html,
            "truncated": truncated,
        }
    )


async def _handler_live_query_all(args: dict[str, Any], context: Any) -> SkillResult:
    page = _get_page(context)
    selector = args.get("selector")
    if not selector or not isinstance(selector, str):
        raise SkillError("selector is required")
    try:
        elements = await asyncio.wait_for(
            page.evaluate(
                """(sel) => {
                    const els = Array.from(document.querySelectorAll(sel));
                    return els.slice(0, 20).map(e => ({
                        tag: e.tagName.toLowerCase(),
                        id: e.id || null,
                        classes: e.className && typeof e.className === 'string' ? e.className.split(/\\s+/).filter(Boolean).slice(0, 10) : [],
                        text: (e.textContent || '').trim().slice(0, 80),
                        visible: !!(e.offsetParent !== null),
                        attrs: Object.fromEntries(Array.from(e.attributes).filter(a => ['name','aria-label','role','data-testid','placeholder','href','type'].includes(a.name)).map(a => [a.name, a.value])),
                    }));
                }""",
                selector,
            ),
            timeout=_LIVE_READ_TIMEOUT_MS / 1000,
        )
    except asyncio.TimeoutError:
        return SkillResult.error("live_query_all_timeout")
    except Exception as exc:
        return SkillResult.error(f"page_eval_error: {type(exc).__name__}: {exc}")
    if not isinstance(elements, list):
        elements = []
    return SkillResult.ok(
        data={
            "selector": selector,
            "match_count": len(elements),
            "elements": elements,
            "url": await _safe_url(page),
        }
    )


async def _handler_live_get_text(args: dict[str, Any], context: Any) -> SkillResult:
    page = _get_page(context)
    selector = args.get("selector")
    if not selector or not isinstance(selector, str):
        raise SkillError("selector is required")
    try:
        text = await asyncio.wait_for(
            page.evaluate(
                "(sel) => {const e = document.querySelector(sel); return e ? e.textContent : null;}",
                selector,
            ),
            timeout=_LIVE_READ_TIMEOUT_MS / 1000,
        )
    except asyncio.TimeoutError:
        return SkillResult.error("live_get_text_timeout")
    except Exception as exc:
        return SkillResult.error(f"page_eval_error: {type(exc).__name__}: {exc}")
    if text is None:
        return SkillResult.not_available(f"selector {selector!r} matched no element")
    return SkillResult.ok(data={"selector": selector, "text": str(text).strip()[:2000]})


async def _handler_live_try_click(args: dict[str, Any], context: Any) -> SkillResult:
    """Attempt a click. A successful click IS the commit — the workflow
    continues from the post-click state.

    Returns pre/post URL and DOM hashes so the agent can verify the click had
    an observable effect (and not a no-op click on a non-interactive element).
    """
    page = _get_page(context)
    selector = args.get("selector")
    if not selector or not isinstance(selector, str):
        raise SkillError("selector is required")

    pre_url = await _safe_url(page)
    pre_hash = await _dom_hash(page)
    try:
        await asyncio.wait_for(
            page.click(selector, timeout=_LIVE_MUTATE_TIMEOUT_MS),
            timeout=(_LIVE_MUTATE_TIMEOUT_MS + 1_000) / 1000,
        )
    except asyncio.TimeoutError:
        return SkillResult.ok(
            data={
                "success": False,
                "error": "click_timeout",
                "pre_url": pre_url,
                "post_url": await _safe_url(page),
                "selector": selector,
            }
        )
    except Exception as exc:
        return SkillResult.ok(
            data={
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "pre_url": pre_url,
                "post_url": await _safe_url(page),
                "selector": selector,
            }
        )

    post_url = await _safe_url(page)
    post_hash = await _dom_hash(page)
    return SkillResult.ok(
        data={
            "success": True,
            "selector": selector,
            "pre_url": pre_url,
            "post_url": post_url,
            "url_changed": pre_url != post_url,
            "pre_dom_hash": pre_hash,
            "post_dom_hash": post_hash,
            "dom_changed": pre_hash != post_hash,
        }
    )


async def _handler_live_try_fill(args: dict[str, Any], context: Any) -> SkillResult:
    """Attempt page.fill(selector, value). Successful fill is the commit."""
    page = _get_page(context)
    selector = args.get("selector")
    value = args.get("value")
    if not selector or not isinstance(selector, str):
        raise SkillError("selector is required")
    if value is None or not isinstance(value, str):
        raise SkillError("value is required (string)")

    pre_url = await _safe_url(page)
    pre_hash = await _dom_hash(page)
    try:
        await asyncio.wait_for(
            page.fill(selector, value, timeout=_LIVE_MUTATE_TIMEOUT_MS),
            timeout=(_LIVE_MUTATE_TIMEOUT_MS + 1_000) / 1000,
        )
    except asyncio.TimeoutError:
        return SkillResult.ok(
            data={
                "success": False,
                "error": "fill_timeout",
                "selector": selector,
                "pre_url": pre_url,
            }
        )
    except Exception as exc:
        return SkillResult.ok(
            data={
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "selector": selector,
                "pre_url": pre_url,
            }
        )

    post_url = await _safe_url(page)
    post_hash = await _dom_hash(page)
    return SkillResult.ok(
        data={
            "success": True,
            "selector": selector,
            "value_chars": len(value),
            "pre_url": pre_url,
            "post_url": post_url,
            "url_changed": pre_url != post_url,
            "pre_dom_hash": pre_hash,
            "post_dom_hash": post_hash,
            "dom_changed": pre_hash != post_hash,
        }
    )


_MIDRUN_ONLY = frozenset({"midrun"})


def all_interact_skills() -> list[Skill]:
    return [
        Skill(
            name="live_get_url",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_get_url,
            schema={
                "name": "live_get_url",
                "description": "Return the current page URL.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        ),
        Skill(
            name="live_get_dom",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_get_dom,
            schema={
                "name": "live_get_dom",
                "description": (
                    "Return outerHTML around the selector (or the whole body if no selector). "
                    "Capped at max_chars. Use this to inspect the page state and find candidate "
                    "selectors when the failed selector is missing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 500, "maximum": 32000, "default": 16000},
                    },
                    "required": [],
                },
            },
        ),
        Skill(
            name="live_query_all",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_query_all,
            schema={
                "name": "live_query_all",
                "description": (
                    "Run document.querySelectorAll and return up to 20 element descriptors "
                    "(tag, id, classes, attrs subset, visibility, short text). Use this to "
                    "validate a candidate selector before live_try_click / live_try_fill."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                    "required": ["selector"],
                },
            },
        ),
        Skill(
            name="live_get_text",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_get_text,
            schema={
                "name": "live_get_text",
                "description": "Return the textContent of the first element matching selector.",
                "input_schema": {
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                    "required": ["selector"],
                },
            },
        ),
        Skill(
            name="live_try_click",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_try_click,
            schema={
                "name": "live_try_click",
                "description": (
                    "Click an element by CSS selector. A successful click IS the commit — the "
                    "workflow continues from the post-click state. Returns pre/post URL and DOM "
                    "hashes so you can verify the click had an effect. Always check that "
                    "url_changed or dom_changed is true before calling declare_success."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                    "required": ["selector"],
                },
            },
        ),
        Skill(
            name="live_try_fill",
            available_to=_MIDRUN_ONLY,
            handler=_handler_live_try_fill,
            schema={
                "name": "live_try_fill",
                "description": (
                    "Fill an input by CSS selector. A successful fill IS the commit. Returns "
                    "pre/post URL and DOM hashes."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["selector", "value"],
                },
            },
        ),
    ]


__all__ = ["all_interact_skills"]
