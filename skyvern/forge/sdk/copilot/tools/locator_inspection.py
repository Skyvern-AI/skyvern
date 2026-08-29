"""Locator inspection that can tell a container from the node carrying the value.

A match count says a selector resolved. It cannot separate ``button.pill:has-text("Star")``, which
uniquely resolves and wraps the wrong text, from ``button.pill:has-text("Star") span.n``, which
uniquely resolves and carries it. Both score identically on every signal a repair turn otherwise
has, which is why a repair can find a live element and still extract nothing usable.

This returns bounded DOM facts for each match, including descendants, so the distinction is
visible. It reports; it does not rank, score, generate a selector, or name a candidate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

TOOL_NAME = "inspect_locator_matches"
MAX_SELECTORS = 8
MAX_MATCHES = 5
MAX_DESCENDANTS = 12
TEXT_CHARS = 200
DESCENDANT_TEXT_CHARS = 120
OUTER_HTML_CHARS = 1200

TOOL_DESCRIPTION = (
    "Inspect Playwright locators being considered for a repair. Pass the failed authored locator "
    "and concrete alternatives before editing. Returns match counts and bounded DOM facts for each "
    "match, including descendants, so container elements can be distinguished from controls or "
    "value-bearing nodes. It reports facts only and does not rank or choose a locator."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "enum": ["debug", "last_run"],
            "description": "Which browser to inspect in.",
        },
        "selectors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Playwright selectors to inspect, in the order you want them reported.",
        },
    },
    "required": ["target", "selectors"],
    "additionalProperties": False,
}

# One expression per match: the element's own identity plus its descendants, so a wrapper and the
# node holding the number are distinguishable without the caller guessing at structure.
_MATCH_FACTS = """
(el) => {
  const clip = (s, n) => (typeof s === 'string' ? s.trim().slice(0, n) : null);
  const identity = (node, textChars) => ({
    tag: node.tagName ? node.tagName.toLowerCase() : null,
    id: node.id || null,
    classes: node.classList ? Array.from(node.classList) : [],
    role: node.getAttribute ? node.getAttribute('role') : null,
    aria_label: node.getAttribute ? node.getAttribute('aria-label') : null,
    text_content: clip(node.textContent, textChars),
  });
  const self = identity(el, %(text_chars)d);
  self.title = el.getAttribute ? el.getAttribute('title') : null;
  self.outer_html = clip(el.outerHTML, %(html_chars)d);
  const kids = Array.from(el.querySelectorAll('*')).slice(0, %(max_desc)d);
  self.descendants = kids.map((node, index) => {
    const d = identity(node, %(desc_chars)d);
    d.index = index;
    return d;
  });
  return self;
}
"""


SELECTOR_BUDGET_SECONDS = 8.0


async def inspect_locator_matches(page: Page, selectors: list[str]) -> dict[str, Any]:
    """Bounded facts for each selector's matches on this page. Read-only: it never navigates."""
    expression = _MATCH_FACTS % {
        "text_chars": TEXT_CHARS,
        "html_chars": OUTER_HTML_CHARS,
        "max_desc": MAX_DESCENDANTS,
        "desc_chars": DESCENDANT_TEXT_CHARS,
    }
    results: list[dict[str, Any]] = []
    for selector in selectors[:MAX_SELECTORS]:
        entry: dict[str, Any] = {"selector": selector}
        locator = page.locator(selector)
        try:
            count = await asyncio.wait_for(locator.count(), timeout=SELECTOR_BUDGET_SECONDS)
        except (PlaywrightError, TimeoutError) as exc:
            # An unusable or unresponsive selector is this selector's answer, not the end of the
            # call: without its own bound, one wedged selector would hold the whole tool call.
            entry["error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
            continue
        entry["match_count"] = count
        entry["matches_truncated"] = count > MAX_MATCHES
        matches: list[dict[str, Any]] = []
        for index in range(min(count, MAX_MATCHES)):
            try:
                facts = await asyncio.wait_for(locator.nth(index).evaluate(expression), timeout=SELECTOR_BUDGET_SECONDS)
            except (PlaywrightError, TimeoutError) as exc:
                matches.append({"index": index, "error": f"{type(exc).__name__}: {exc}"})
                continue
            facts["index"] = index
            matches.append(facts)
        entry["matches"] = matches
        results.append(entry)
    return {"selectors": results, "selectors_truncated": len(selectors) > MAX_SELECTORS}
