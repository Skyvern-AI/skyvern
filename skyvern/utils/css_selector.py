"""Compute stable CSS selectors from element data for the script reviewer.

When the AI agent successfully interacts with an element during fallback,
we capture the element's attributes. This module computes a robust CSS
selector from those attributes so the script reviewer can write code that
targets the same element without relying on ephemeral unique_ids.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from skyvern.webeye.actions.actions import Action


def compute_stable_selector(element_data: dict | None) -> str | None:
    """Derive the BEST stable CSS selector from a scraped element dictionary.

    Returns the highest-ranked selector that isn't obviously dynamic.
    For the full ranked list (shown to the reviewer), use compute_selector_options.
    """
    options = compute_selector_options(element_data)
    if not options:
        return None
    # Return the top-ranked option
    return options[0][0]


def compute_selector_options(element_data: dict | None) -> list[tuple[str, str]]:
    """Compute ALL viable CSS selectors ranked by stability.

    Returns a list of (selector, description) tuples, most stable first.
    The reviewer sees all options and picks the best one.

    Ranking (most stable first):
      1. [data-testid="..."]        — stable test attribute
      2. tag[aria-label="..."]      — accessibility attribute
      3. tag[name="..."]            — form element name
      4. tag[placeholder="..."]     — visible hint text
      5. tag:has-text("...")        — visible text content
      6. tag[role="..."]           — ARIA role
      7. #id (stable)               — unique, not auto-generated
      8. input[type="..."]          — type attribute (weak)
      9. #id (possibly dynamic)     — ID that looks auto-generated
    """
    if not element_data:
        return []

    tag = (element_data.get("tagName") or "").lower()
    attrs = element_data.get("attributes") or {}
    text = (element_data.get("text") or "").strip()
    options: list[tuple[str, str]] = []

    # data-testid (most stable — added by developers for testing)
    testid = attrs.get("data-testid", "").strip()
    if testid:
        options.append((f'[data-testid="{_css_escape_attr(testid)}"]', "stable test attribute"))

    # aria-label + tag
    aria = attrs.get("aria-label", "").strip()
    if aria and tag:
        options.append((f'{tag}[aria-label="{_css_escape_attr(aria)}"]', "accessibility label"))

    # name + tag (for form elements)
    name = attrs.get("name", "").strip()
    if name and tag in ("input", "select", "textarea", "button"):
        options.append((f'{tag}[name="{_css_escape_attr(name)}"]', "form element name"))

    # placeholder + tag
    placeholder = attrs.get("placeholder", "").strip()
    if placeholder and tag in ("input", "textarea"):
        options.append((f'{tag}[placeholder="{_css_escape_attr(placeholder)}"]', "placeholder text"))

    # Visible text content + tag (for buttons, links)
    if text and tag in ("button", "a") and len(text) <= 50:
        clean_text = text.replace("\n", " ").replace("\r", "").strip()
        safe_text = _css_escape_attr(clean_text)
        options.append((f'{tag}:has-text("{safe_text}")', "visible text"))

    # role + tag
    role = attrs.get("role", "").strip()
    if role and tag:
        options.append((f'{tag}[role="{_css_escape_attr(role)}"]', "ARIA role"))

    # ID attribute — ranked AFTER semantic selectors
    elem_id = attrs.get("id", "").strip()
    if elem_id:
        if not _looks_dynamic(elem_id):
            options.append((f"#{_css_escape(elem_id)}", "HTML id"))
        else:
            # Still include dynamic IDs but clearly marked — let the reviewer decide
            options.append((f"#{_css_escape(elem_id)}", "HTML id (likely auto-generated — may change between runs)"))

    # Input type attribute (weak selector — many inputs share the same type)
    input_type = attrs.get("type", "").strip()
    if input_type and tag == "input" and input_type not in ("text", "hidden"):
        options.append((f'input[type="{_css_escape_attr(input_type)}"]', "input type"))

    return options


def _looks_dynamic(value: str) -> bool:
    """Heuristic: IDs that are likely auto-generated and will change across runs."""
    # Long hex strings (ember123, react-456, el_abc123def)
    if re.search(r"[0-9a-f]{8,}", value, re.IGNORECASE):
        return True
    # Word-digit patterns (uid-1234, el_5678)
    if re.search(r"^\w+[-_]\d{4,}$", value):
        return True
    return False


def _css_escape(s: str) -> str:
    """Escape a string for use as a CSS ID selector."""
    return re.sub(r'([!"#$%&\'()*+,./:;<=>?@[\\\]^`{|}~])', r"\\\1", s)


def _css_escape_attr(s: str) -> str:
    """Escape a string for use inside a CSS attribute value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Action summary builder (shared between workflow service & script service)
# ---------------------------------------------------------------------------

# Attributes safe to pass to the script reviewer (excludes noisy/dynamic attrs)
REVIEWER_SAFE_ATTRS = frozenset(
    {
        "name",
        "id",
        "placeholder",
        "aria-label",
        "type",
        "role",
        "data-testid",
        "data-test-id",
        "data-cy",
        "data-qa",
        "href",
        "for",
        "alt",
        "title",
        "action",
        "method",
        "autocomplete",
        "inputmode",
        "pattern",
        "maxlength",
        "aria-describedby",
        "aria-labelledby",
        "aria-haspopup",
        "value",  # useful for pre-selected state
    }
)


def build_action_summary(action: Action) -> dict:
    """Build a rich action summary dict for the script reviewer.

    Includes a computed CSS selector suggestion so the reviewer can write
    reliable selectors without guessing from sparse attributes.

    Kept in this module (rather than workflow/service.py) so both the workflow
    service and script_service can use it without circular imports.
    """
    elem = action.skyvern_element_data or {}
    attrs = elem.get("attributes") or {}

    useful_attrs = {k: v for k, v in attrs.items() if k in REVIEWER_SAFE_ATTRS and v}

    return {
        "action_type": action.action_type,
        "intention": action.intention,
        "reasoning": action.reasoning,
        "status": action.status,
        # Strip query params from URL — they can contain OAuth tokens, email
        # addresses, session IDs. The reviewer only needs host+path for redirect detection.
        "page_url": (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if (raw_url := elem.get("page_url")) and (parsed := urlparse(raw_url)).netloc
            else None
        ),
        "field": (action.input_or_select_context.field if action.input_or_select_context else None),
        # Legacy: 6 core attributes (kept for backward compat with older templates)
        "element_attributes": (
            {k: v for k, v in attrs.items() if k in ("name", "id", "placeholder", "aria-label", "type", "role") and v}
            if attrs
            else None
        ),
        # Element context for better selector generation
        "element_tag": elem.get("tagName"),
        "element_text": (elem.get("text") or "")[:100] or None,
        "all_attributes": useful_attrs or None,
        "css_suggestion": compute_stable_selector(elem),
        "selector_options": compute_selector_options(elem) or None,
    }


def build_action_summaries_with_timing(actions: list[Action]) -> list[dict]:
    """Build action summaries with time deltas between consecutive actions.

    Wraps build_action_summary and adds seconds_since_previous (the delta
    in seconds from the previous action) for all actions with timestamps.
    The Jinja template filters to only render deltas > 3 seconds.
    """
    summaries = []
    prev_ts = None
    for a in actions[:20]:
        summary = build_action_summary(a)
        if a.created_at and prev_ts:
            delta = (a.created_at - prev_ts).total_seconds()
            summary["seconds_since_previous"] = round(max(delta, 0), 1)
        if a.created_at:
            prev_ts = a.created_at
        summaries.append(summary)
    return summaries
