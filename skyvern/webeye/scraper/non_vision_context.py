from __future__ import annotations

import json
import re
from typing import Any

import structlog
from playwright.async_api import Frame, Page

from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.forge.sdk.core import skyvern_context

LOG = structlog.get_logger()

MAX_NON_VISION_CONTEXT_CHARS = 20_000
MAX_VISIBLE_TEXT_CHARS = 6_000
MAX_ACCESSIBILITY_NODES = 160
_SENSITIVE_VALUE_RE = re.compile(
    r"(password|passcode|one[-_\s]?time[-_\s]?code|otp|token|secret|api[-_\s]?key|access[-_\s]?key|"
    r"private[-_\s]?key|cvv|cvc|security[-_\s]?code|ssn|social[-_\s]?security|card[-_\s]?number|"
    r"credit[-_\s]?card)",
    re.IGNORECASE,
)

_ACCESSIBILITY_CONTEXT_SCRIPT = """
({ skyvernIdAttr, maxNodes, maxVisibleTextChars }) => {
  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const truncate = (value, limit) => {
    const text = normalize(value);
    return text.length > limit ? text.slice(0, limit) + "..." : text;
  };
  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (!style || style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0
      && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
  };
  const textByIds = (ids) => normalize(ids.split(/\\s+/).map((id) => {
    const ref = document.getElementById(id);
    return ref ? (ref.innerText || ref.textContent || "") : "";
  }).join(" "));
  const labelText = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria;
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = textByIds(labelledBy);
      if (text) return text;
    }
    const id = el.getAttribute("id");
    if (id) {
      const escapedId = window.CSS && CSS.escape ? CSS.escape(id) : id.replace(/["\\\\]/g, "\\\\$&");
      const label = document.querySelector(`label[for="${escapedId}"]`);
      if (label) return label.innerText || label.textContent || "";
    }
    const parentLabel = el.closest("label");
    if (parentLabel) return parentLabel.innerText || parentLabel.textContent || "";
    return el.getAttribute("title") || el.getAttribute("alt") || el.getAttribute("placeholder") || "";
  };
  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "a" && el.getAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "button") return "button";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "img";
    return "";
  };
  const rgb = (value) => {
    const match = (value || "").match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (!match) return null;
    return { r: Number(match[1]), g: Number(match[2]), b: Number(match[3]) };
  };
  const isErrorColor = (value) => {
    const color = rgb(value);
    return color && color.r > 140 && color.r > color.g * 1.4 && color.r > color.b * 1.4;
  };
  const selector = [
    "a[href]", "button", "input", "select", "textarea", "summary", "option",
    "[role]", "[aria-label]", "[aria-labelledby]", "[aria-describedby]",
    "[aria-invalid]", "[aria-expanded]", "[aria-selected]", "[aria-checked]",
    "[tabindex]:not([tabindex='-1'])", "[data-testid]"
  ].join(",");
  const nodes = [];
  for (const el of Array.from(document.querySelectorAll(selector))) {
    if (nodes.length >= maxNodes) break;
    if (!isVisible(el)) continue;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role") || implicitRole(el);
    const text = truncate(el.innerText || el.textContent || "", 220);
    const name = truncate(labelText(el), 220);
    const node = {
      skyvern_id: el.getAttribute(skyvernIdAttr) || null,
      tag,
      role: role || null,
      name: name || null,
      text: text && text !== name ? text : null,
      type: el.getAttribute("type") || null,
      value: tag === "input" || tag === "textarea" || tag === "select" ? truncate(el.value, 120) : null,
      autocomplete: el.getAttribute("autocomplete") || null,
      html_id: el.getAttribute("id") || null,
      html_name: el.getAttribute("name") || null,
      placeholder: el.getAttribute("placeholder") || null,
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true" || null,
      required: el.required === true || el.getAttribute("aria-required") === "true" || null,
      checked: el.checked === true || el.getAttribute("aria-checked") || null,
      selected: el.selected === true || el.getAttribute("aria-selected") || null,
      expanded: el.getAttribute("aria-expanded") || null,
      invalid: el.getAttribute("aria-invalid") || (el.validity && !el.validity.valid) || null,
      validation_message: el.validationMessage || null,
      error_color: isErrorColor(style.color) ? style.color : null,
      rect: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    };
    for (const key of Object.keys(node)) {
      if (node[key] === null) delete node[key];
    }
    if (node.name || node.text || node.role || node.skyvern_id || node.placeholder || node.value) {
      nodes.push(node);
    }
  }
  return {
    title: document.title || "",
    url: window.location.href,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    visible_text: truncate(document.body ? document.body.innerText : "", maxVisibleTextChars),
    accessibility_tree: nodes
  };
}
"""


def _truncate(text: str | None, max_chars: int) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars] + "..."


def _redact_sensitive_values(page_context: dict[str, Any]) -> bool:
    nodes = page_context.get("accessibility_tree")
    if not isinstance(nodes, list):
        return False

    redacted_sensitive_value = False
    for node in nodes:
        if not isinstance(node, dict) or "value" not in node:
            continue
        sensitive_fields = [
            node.get("type"),
            node.get("name"),
            node.get("placeholder"),
            node.get("autocomplete"),
            node.get("html_id"),
            node.get("html_name"),
        ]
        haystack = " ".join(str(value) for value in sensitive_fields if value)
        if _SENSITIVE_VALUE_RE.search(haystack):
            node.pop("value", None)
            node["value_redacted"] = True
            redacted_sensitive_value = True

    return redacted_sensitive_value


def _json_dumps(page_context: dict[str, Any]) -> str:
    return json.dumps(page_context, separators=(",", ":"), default=str)


def _bounded_json_context(page_context: dict[str, Any], max_chars: int) -> str:
    rendered = _json_dumps(page_context)
    if len(rendered) <= max_chars:
        return rendered

    bounded = dict(page_context)
    bounded["truncated"] = True
    if isinstance(bounded.get("visible_text"), str):
        bounded["visible_text"] = _truncate(bounded["visible_text"], min(MAX_VISIBLE_TEXT_CHARS, max_chars // 4))

    nodes = bounded.get("accessibility_tree")
    if isinstance(nodes, list):
        bounded["accessibility_tree"] = list(nodes)
        while bounded["accessibility_tree"] and len(_json_dumps(bounded)) > max_chars:
            keep_count = max(0, len(bounded["accessibility_tree"]) // 2)
            bounded["accessibility_tree"] = bounded["accessibility_tree"][:keep_count]

    rendered = _json_dumps(bounded)
    if len(rendered) <= max_chars:
        return rendered

    fallback: dict[str, Any] = {"truncated": True}
    for key in ("url", "title"):
        value = bounded.get(key)
        if value:
            fallback[key] = _truncate(str(value), max_chars // 4)
    rendered = _json_dumps(fallback)
    return rendered if len(rendered) <= max_chars else _json_dumps({"truncated": True})


async def _safe_page_context(page: Frame | Page | None) -> tuple[dict[str, Any], bool]:
    if page is None:
        return {}, False
    try:
        result = await page.evaluate(
            _ACCESSIBILITY_CONTEXT_SCRIPT,
            {
                "skyvernIdAttr": SKYVERN_ID_ATTR,
                "maxNodes": MAX_ACCESSIBILITY_NODES,
                "maxVisibleTextChars": MAX_VISIBLE_TEXT_CHARS,
            },
        )
        if not isinstance(result, dict):
            return {}, False
        redacted_sensitive_values = _redact_sensitive_values(result)
        if redacted_sensitive_values:
            result.pop("visible_text", None)
        return result, redacted_sensitive_values
    except Exception:
        LOG.warning("Failed to build non-vision page context", exc_info=True)
        return {}, False


async def build_non_vision_page_context(
    *,
    scraped_page: Any | None = None,
    page: Frame | Page | None = None,
    max_chars: int = MAX_NON_VISION_CONTEXT_CHARS,
) -> str | None:
    page_context, redacted_sensitive_values = await _safe_page_context(page)
    if scraped_page is not None:
        page_context.setdefault("url", getattr(scraped_page, "url", None))
        extracted_text = _truncate(getattr(scraped_page, "extracted_text", None), MAX_VISIBLE_TEXT_CHARS)
        if extracted_text and not page_context.get("visible_text") and not redacted_sensitive_values:
            page_context["visible_text"] = extracted_text

    if not page_context:
        return None

    return _bounded_json_context(page_context, max_chars)


async def build_non_vision_page_context_if_needed(
    *,
    scraped_page: Any | None = None,
    page: Frame | Page | None = None,
    max_chars: int = MAX_NON_VISION_CONTEXT_CHARS,
) -> str | None:
    context = skyvern_context.current()
    if not context or not context.llm_accessibility_context_enabled():
        return None
    return await build_non_vision_page_context(scraped_page=scraped_page, page=page, max_chars=max_chars)
