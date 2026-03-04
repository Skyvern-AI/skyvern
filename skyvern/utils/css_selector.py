"""Compute stable CSS selectors from element data for the script reviewer.

When the AI agent successfully interacts with an element during fallback,
we capture the element's attributes. This module computes a robust CSS
selector from those attributes so the script reviewer can write code that
targets the same element without relying on ephemeral unique_ids.
"""

from __future__ import annotations

import re


def compute_stable_selector(element_data: dict | None) -> str | None:
    """Derive a stable CSS selector from a scraped element dictionary.

    Priority order (highest confidence first):
      1. #id                        — unique by definition
      2. [data-testid="..."]        — stable test attribute
      3. tag[aria-label="..."]      — accessibility attribute
      4. tag[name="..."]            — form element name
      5. tag[placeholder="..."]     — visible hint text
      6. tag:has-text("...")        — visible text content
      7. tag[role="..."]           — ARIA role

    Returns None if no reliable selector can be built.
    """
    if not element_data:
        return None

    tag = (element_data.get("tagName") or "").lower()
    attrs = element_data.get("attributes") or {}
    text = (element_data.get("text") or "").strip()

    # 1. ID attribute (strongest — unique per page by spec)
    elem_id = attrs.get("id", "").strip()
    if elem_id and not _looks_dynamic(elem_id):
        return f"#{_css_escape(elem_id)}"

    # 2. data-testid (stable testing attribute)
    testid = attrs.get("data-testid", "").strip()
    if testid:
        return f'[data-testid="{_css_escape_attr(testid)}"]'

    # 3. aria-label + tag
    aria = attrs.get("aria-label", "").strip()
    if aria and tag:
        return f'{tag}[aria-label="{_css_escape_attr(aria)}"]'

    # 4. name + tag (for form elements)
    name = attrs.get("name", "").strip()
    if name and tag in ("input", "select", "textarea", "button"):
        return f'{tag}[name="{_css_escape_attr(name)}"]'

    # 5. placeholder + tag
    placeholder = attrs.get("placeholder", "").strip()
    if placeholder and tag in ("input", "textarea"):
        return f'{tag}[placeholder="{_css_escape_attr(placeholder)}"]'

    # 6. Visible text content + tag (for buttons, links)
    if text and tag in ("button", "a") and len(text) <= 50:
        # Use :has-text() which is a case-insensitive substring match
        clean_text = text.replace("\n", " ").replace("\r", "").strip()
        safe_text = _css_escape_attr(clean_text)
        return f'{tag}:has-text("{safe_text}")'

    # 7. role + tag
    role = attrs.get("role", "").strip()
    if role and tag:
        return f'{tag}[role="{_css_escape_attr(role)}"]'

    # 8. type + tag (for inputs — weak but better than nothing)
    input_type = attrs.get("type", "").strip()
    if input_type and tag == "input" and input_type not in ("text", "hidden"):
        return f'input[type="{_css_escape_attr(input_type)}"]'

    return None


def _looks_dynamic(value: str) -> bool:
    """Heuristic: IDs with long hex/numeric suffixes are likely auto-generated."""
    # Matches patterns like "ember123", "react-456", "el_abc123def", "uid-xxxx"
    if re.search(r"[0-9a-f]{8,}", value, re.IGNORECASE):
        return True
    if re.search(r"^\w+[-_]\d{4,}$", value):
        return True
    return False


def _css_escape(s: str) -> str:
    """Escape a string for use as a CSS ID selector."""
    return re.sub(r'([!"#$%&\'()*+,./:;<=>?@[\\\]^`{|}~])', r"\\\1", s)


def _css_escape_attr(s: str) -> str:
    """Escape a string for use inside a CSS attribute value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
