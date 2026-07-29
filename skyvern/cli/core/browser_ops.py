"""Shared browser operations for MCP tools and CLI commands.

Each function: validate inputs -> call SDK -> return typed result.
Session resolution and output formatting are caller responsibilities.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import structlog

from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.utils.page import JS_FUNCTION_DEFS, SkyvernFrame

from .guards import GuardError

LOG = structlog.get_logger(__name__)


@dataclass
class NavigateResult:
    url: str
    title: str


@dataclass
class ScreenshotResult:
    data: bytes
    full_page: bool = False


@dataclass
class ActResult:
    prompt: str
    completed: bool = True


@dataclass
class ExtractResult:
    extracted: Any = None


def parse_extract_schema(schema: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse and validate an extraction schema payload."""
    if schema is None:
        return None
    if isinstance(schema, dict):
        return schema

    try:
        return json.loads(schema)
    except (json.JSONDecodeError, TypeError) as e:
        raise GuardError(f"Invalid JSON schema: {e}", "Provide schema as a valid JSON string")


async def do_navigate(
    page: Any,
    url: str,
    timeout: int = 30000,
    wait_until: str | None = None,
) -> NavigateResult:
    await page.goto(url, timeout=timeout, wait_until=wait_until)
    return NavigateResult(url=page.url, title=await page.title())


async def do_screenshot(
    page: Any,
    full_page: bool = False,
    selector: str | None = None,
) -> ScreenshotResult:
    if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
        try:
            await SkyvernFrame.hide_cursor_overlay(page)
        except Exception:
            pass
    try:
        if selector:
            element = page.locator(selector)
            data = await element.screenshot()
        else:
            data = await page.screenshot(full_page=full_page)
    finally:
        if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
            try:
                await SkyvernFrame.show_cursor_overlay(page)
            except Exception:
                pass
    return ScreenshotResult(data=data, full_page=full_page)


async def do_act(
    page: Any,
    prompt: str,
    skip_refresh: bool = False,
    use_economy_tree: bool = False,
) -> ActResult:
    await page.act(prompt, skip_refresh=skip_refresh, use_economy_tree=use_economy_tree)
    return ActResult(prompt=prompt, completed=True)


async def do_extract(
    page: Any,
    prompt: str,
    schema: str | dict[str, Any] | None = None,
    skip_refresh: bool = False,
) -> ExtractResult:
    parsed_schema = parse_extract_schema(schema)
    extracted = await page.extract(prompt=prompt, schema=parsed_schema, skip_refresh=skip_refresh)
    return ExtractResult(extracted=extracted)


# -- Semantic locators --


@dataclass
class FindResult:
    selector: str
    count: int
    first_text: str | None
    first_visible: bool


locator_map: dict[str, str] = {
    "role": "get_by_role",
    "text": "get_by_text",
    "label": "get_by_label",
    "placeholder": "get_by_placeholder",
    "alt": "get_by_alt_text",
    "testid": "get_by_test_id",
}

LOCATOR_TYPES = frozenset(locator_map.keys())


async def do_find(page: Any, by: str, value: str) -> FindResult:
    """Locate elements using Playwright's semantic locator API."""
    if by not in locator_map:
        raise GuardError(
            f"Invalid locator type: {by!r}. Must be one of: {', '.join(sorted(LOCATOR_TYPES))}",
            f"Use one of: {', '.join(sorted(LOCATOR_TYPES))}",
        )
    locator = getattr(page, locator_map[by])(value)
    count = await locator.count()
    first_text = await locator.first.text_content() if count > 0 else None
    first_visible = await locator.first.is_visible() if count > 0 else False
    return FindResult(
        selector=f"{locator_map[by]}({value!r})",
        count=count,
        first_text=first_text,
        first_visible=first_visible,
    )


# -- Frame operations --


@dataclass
class FrameInfo:
    index: int
    name: str
    url: str
    is_main: bool


@dataclass
class FrameSwitchResult:
    name: str | None
    url: str | None
    selector: str | None = None
    requested_name: str | None = None
    index: int | None = None


async def do_frame_switch(
    page: Any,
    *,
    selector: str | None = None,
    name: str | None = None,
    index: int | None = None,
) -> FrameSwitchResult:
    result = await page.frame_switch(selector=selector, name=name, index=index)
    return FrameSwitchResult(
        name=result.get("name"),
        url=result.get("url"),
        selector=selector,
        requested_name=name,
        index=index,
    )


def do_frame_main(page: Any) -> None:
    page.frame_main()


async def do_frame_list(page: Any) -> list[FrameInfo]:
    frames = await page.frame_list()
    return [FrameInfo(index=f["index"], name=f["name"], url=f["url"], is_main=f["is_main"]) for f in frames]


# -- Auth state persistence --


@dataclass
class StateSaveResult:
    file_path: str
    cookie_count: int
    local_storage_count: int
    session_storage_count: int
    url: str


@dataclass
class StateLoadResult:
    cookie_count: int
    local_storage_count: int
    session_storage_count: int
    source_url: str
    skipped_cookies: int


def _cookie_domain_matches(cookie_domain: str, page_domain: str) -> bool:
    """Check if a cookie's domain matches the current page domain per RFC 6265.

    Handles leading dots (wildcard subdomains).
    Rejects suffix attacks: 'evil-example.com' must NOT match 'example.com'.
    """
    if not cookie_domain or not page_domain:
        return False
    cd = cookie_domain.lstrip(".")
    if not cd:
        return False
    return page_domain == cd or page_domain.endswith("." + cd)


async def do_state_save(page: Any, browser: Any, file_path: Path) -> StateSaveResult:
    """Save browser auth state to a JSON file.

    ``page`` is the raw Playwright Page (not SkyvernBrowserPage).
    ``browser`` is a SkyvernBrowser — cookies accessed via ``browser._browser_context``.
    """
    pw_context = browser._browser_context
    cookies = await pw_context.cookies()
    local_storage = await page.evaluate("() => Object.fromEntries(Object.entries(window.localStorage))")
    session_storage = await page.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")

    state = {
        "version": 1,
        "url": page.url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cookies": cookies,
        "local_storage": local_storage,
        "session_storage": session_storage,
    }

    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    return StateSaveResult(
        file_path=str(file_path),
        cookie_count=len(cookies),
        local_storage_count=len(local_storage),
        session_storage_count=len(session_storage),
        url=page.url,
    )


async def do_state_load(
    page: Any,
    browser: Any,
    file_path: Path,
    current_domain: str,
) -> StateLoadResult:
    """Load browser auth state from a JSON file.

    Validates JSON schema version. Filters cookies to only apply those matching
    ``current_domain`` to prevent cross-domain session injection.
    """
    raw = file_path.read_text()
    state = json.loads(raw)
    if state.get("version") != 1:
        raise ValueError(f"Unsupported state file version: {state.get('version')}")

    pw_context = browser._browser_context

    all_cookies = state.get("cookies", [])
    safe_cookies = [c for c in all_cookies if _cookie_domain_matches(c.get("domain", ""), current_domain)]
    skipped = len(all_cookies) - len(safe_cookies)

    if safe_cookies:
        await pw_context.add_cookies(safe_cookies)

    local_storage = state.get("local_storage", {})
    for k, v in local_storage.items():
        await page.evaluate(
            "(args) => window.localStorage.setItem(args[0], args[1])",
            [k, v],
        )

    session_storage = state.get("session_storage", {})
    for k, v in session_storage.items():
        await page.evaluate(
            "(args) => window.sessionStorage.setItem(args[0], args[1])",
            [k, v],
        )

    return StateLoadResult(
        cookie_count=len(safe_cookies),
        local_storage_count=len(local_storage),
        session_storage_count=len(session_storage),
        source_url=state.get("url", ""),
        skipped_cookies=skipped,
    )


# -- DOM inspection --


async def do_get_html(page: Any, selector: str, outer: bool = False) -> str:
    """Get innerHTML or outerHTML from an element. ``page`` is raw Playwright Page."""
    prop = "outerHTML" if outer else "innerHTML"
    return await page.locator(selector).evaluate(f"el => el.{prop}")


async def do_get_value(page: Any, selector: str) -> str | None:
    """Get the current value of a form input element."""
    return await page.locator(selector).input_value()


async def do_get_styles(page: Any, selector: str, properties: list[str] | None = None) -> dict[str, str]:
    """Get computed CSS styles from an element."""
    if properties is not None:
        if not properties:
            return {}
        return await page.locator(selector).evaluate(
            """(el, props) => {
                const styles = window.getComputedStyle(el);
                return Object.fromEntries(props.map(p => [p, styles.getPropertyValue(p)]));
            }""",
            properties,
        )
    return await page.locator(selector).evaluate(
        """el => {
            const styles = window.getComputedStyle(el);
            const result = {};
            for (let i = 0; i < Math.min(styles.length, 100); i++) {
                result[styles[i]] = styles.getPropertyValue(styles[i]);
            }
            return result;
        }"""
    )


# ---------------------------------------------------------------------------
# Network operations
# ---------------------------------------------------------------------------

# Fields stripped from list view to reduce payload. The detail tool returns
# the full entry dict (including these fields) via do_network_request_detail.
_LIST_STRIP_KEYS = frozenset({"response_headers"})


@dataclass
class NetworkRequestsResult:
    requests: list[dict[str, Any]]
    count: int
    error: dict[str, Any] | None = None


@dataclass
class NetworkRequestDetailResult:
    request: dict[str, Any] | None = None
    body: str | None = None
    found: bool = False


@dataclass
class NetworkRouteResult:
    url_pattern: str = ""
    action: str = ""
    active_routes: list[str] = field(default_factory=list)


@dataclass
class NetworkUnrouteResult:
    url_pattern: str = ""
    removed: bool = False
    active_routes: list[str] = field(default_factory=list)


def do_network_requests(
    state: Any,
    *,
    url_pattern: str | None = None,
    status_code: int | None = None,
    method: str | None = None,
    resource_type: str | None = None,
) -> NetworkRequestsResult:
    """Filter and return network request entries from state. Sync — no Playwright calls."""
    entries = list(state.network_requests)

    if url_pattern:
        try:
            compiled = re.compile(url_pattern)
            entries = [e for e in entries if compiled.search(e.get("url", ""))]
        except re.error:
            from .result import ErrorCode, make_error

            return NetworkRequestsResult(
                requests=[],
                count=0,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid regex pattern: {url_pattern}",
                    "Provide a valid Python regex pattern",
                ),
            )
    if status_code is not None:
        entries = [e for e in entries if e.get("status") == status_code]
    if method:
        method_upper = method.upper()
        entries = [e for e in entries if e.get("method") == method_upper]
    if resource_type:
        rt_lower = resource_type.lower()
        entries = [e for e in entries if e.get("resource_type", "").lower() == rt_lower]

    # Strip heavy fields for list view
    display = [{k: v for k, v in e.items() if k not in _LIST_STRIP_KEYS} for e in entries]
    return NetworkRequestsResult(requests=display, count=len(display))


def do_network_request_detail(state: Any, request_id: int) -> NetworkRequestDetailResult:
    """Look up a single request by ID and return full metadata + body."""
    for entry in state.network_requests:
        if entry.get("request_id") == request_id:
            body = state.get_response_body(request_id)
            return NetworkRequestDetailResult(request=dict(entry), body=body, found=True)
    return NetworkRequestDetailResult()


async def do_network_route(
    raw_page: Any,
    state: Any,
    *,
    url_pattern: str,
    action: Literal["abort", "mock"],
    mock_status: int = 200,
    mock_body: str | None = None,
    mock_content_type: str | None = None,
) -> NetworkRouteResult:
    """Register a route handler on the Playwright page and track in SessionState."""

    async def _handler(route: Any) -> None:
        try:
            if action == "abort":
                await route.abort()
            elif action == "mock":
                headers: dict[str, str] = {}
                if mock_content_type:
                    headers["content-type"] = mock_content_type
                elif mock_body is not None:
                    headers["content-type"] = "application/json"
                await route.fulfill(
                    status=mock_status,
                    headers=headers if headers else None,
                    body=mock_body or "",
                )
            else:
                await route.abort()
        except Exception:
            try:
                await route.abort()
            except Exception:
                pass

    page_id = id(raw_page)
    page_routes = state.active_routes.setdefault(page_id, set())

    # Re-register: unroute existing handler for this pattern first
    if url_pattern in page_routes:
        try:
            await raw_page.unroute(url_pattern)
            page_routes.discard(url_pattern)
        except Exception:
            pass

    await raw_page.route(url_pattern, _handler)
    page_routes.add(url_pattern)

    return NetworkRouteResult(
        url_pattern=url_pattern,
        action=action,
        active_routes=sorted(page_routes),
    )


async def do_network_unroute(raw_page: Any, state: Any, url_pattern: str) -> NetworkUnrouteResult:
    """Remove a route handler and update SessionState tracking."""
    page_id = id(raw_page)
    page_routes = state.active_routes.get(page_id, set())
    removed = url_pattern in page_routes
    if removed:
        await raw_page.unroute(url_pattern)
        page_routes.discard(url_pattern)

    return NetworkUnrouteResult(
        url_pattern=url_pattern,
        removed=removed,
        active_routes=sorted(page_routes),
    )


# ---------------------------------------------------------------------------
# Observe — scoped accessibility tree snapshot with stable refs
# ---------------------------------------------------------------------------

INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)

_ROLE_TO_TAG: dict[str, str] = {
    "textbox": "input",
    "searchbox": "input",
    "checkbox": "input",
    "radio": "input",
    "slider": "input",
    "spinbutton": "input",
    "switch": "input",
    "button": "button",
    "link": "a",
    "combobox": "select",
    "listbox": "select",
    "option": "option",
    "tab": "button",
    "menuitem": "li",
    "menuitemcheckbox": "li",
    "menuitemradio": "li",
    "treeitem": "li",
}

# Structural fields always kept in serialized output; display fields filtered if empty.
_ELEMENT_KEEP_ALWAYS = frozenset({"ref", "role"})

_DOMUTILS_INTERACTABILITY_READY_JS = r"""
() => typeof isInteractable === "function" && typeof getHoverStylesMap === "function" && typeof buildTreeFromBody === "function"
"""

# getHoverStylesMap() can await cross-origin stylesheet fetches with no abort signal; bound the
# scan so main-page observe falls back to a11y and selected-frame observe raises a typed error.
_DOM_SCAN_TIMEOUT_SECONDS = 30.0

_OBSERVE_INTERACTABLES_JS = r"""
async ({ scopeSelector, includeValues }) => {
  const root = scopeSelector ? document.querySelector(scopeSelector) : document.body;
  if (!root) return [];
  let hoverStylesMap;

  const esc = (value) => (window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&"));
  const classes = (element) => (element.className || "").toString();
  const lowerClasses = (element) => classes(element).toLowerCase();
  const isPassword = (element) => element.tagName.toLowerCase() === "input" && (element.getAttribute("type") || "").toLowerCase() === "password";
  const text = (element) => {
    const aria = element.getAttribute("aria-label");
    if (aria?.trim()) return aria.trim();
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      const labelledText = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText || document.getElementById(id)?.textContent || "")
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (labelledText) return labelledText;
    }
    if (element.id) {
      const labelText = (document.querySelector(`label[for="${esc(element.id)}"]`)?.innerText || "")
        .replace(/\s+/g, " ")
        .trim();
      if (labelText) return labelText;
    }
    if (element.labels && element.labels.length) {
      const labelsText = Array.from(element.labels)
        .map((node) => node.innerText || node.textContent || "")
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (labelsText) return labelsText;
    }
    // Deliberate: unlabeled non-password inputs may surface their value as the accessible
    // name (identification fallback), independent of includeValues; passwords never do.
    return (element.innerText || element.textContent || element.getAttribute("placeholder") || (isPassword(element) ? "" : element.value) || "")
      .replace(/\s+/g, " ")
      .trim();
  };
  const cssPath = (element) => {
    if (element.id) return `#${esc(element.id)}`;
    const parts = [];
    for (let current = element; current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement; current = current.parentElement) {
      let part = current.tagName.toLowerCase();
      if (current.id) {
        parts.unshift(`#${esc(current.id)}`);
        break;
      }
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter((child) => child.tagName === current.tagName)
        : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  };
  const explicitRole = (element) => (element.getAttribute("role") || "").toLowerCase();
  const nativeRole = (element) => {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    if (["button", "select", "option", "textarea"].includes(tag)) {
      return { button: "button", select: "combobox", option: "option", textarea: "textbox" }[tag];
    }
    if (tag === "a" && element.href) return "link";
    if (tag !== "input") return "";
    return { checkbox: "checkbox", radio: "radio", range: "slider", search: "searchbox" }[type] || "textbox";
  };
  const pointer = (element) => window.getComputedStyle(element)?.cursor === "pointer";
  const staticClick = (element) => element.hasAttribute("onclick") || typeof element.onclick === "function" || element.hasAttribute("jsaction");
  const frameworkClick = (element) => {
    if (element.getAttributeNames().some((attr) => ["ng-click", "data-ng-click", "x-ng-click", "(click)"].includes(attr.toLowerCase()))) return true;
    try {
      return Boolean(window.jQuery?._data?.(element, "events")?.click);
    } catch (_) {
      return false;
    }
  };
  const ancestorMatching = (element, predicate) => {
    for (let current = element.parentElement, depth = 0; current && depth < 5; current = current.parentElement, depth += 1) {
      if (predicate(current)) return current;
    }
    return null;
  };
  const optionContainer = (element) => ancestorMatching(element, (candidate) => {
    const role = explicitRole(candidate);
    const cls = lowerClasses(candidate);
    return role === "listbox" || role === "menu" || ["list", "menu", "option", "dropdown", "select"].some((token) => cls.includes(token));
  });
  const interactiveAncestor = (element) => ancestorMatching(element, (candidate) => {
    const role = explicitRole(candidate);
    const cls = lowerClasses(candidate);
    return pointer(candidate) || staticClick(candidate) || frameworkClick(candidate) || candidate.getAttribute("tabindex") === "0" || candidate.getAttribute("aria-expanded") === "true" || cls.includes("open") || role === "combobox" || role === "listbox";
  });
  const optionLike = (element) => Boolean(text(element) && optionContainer(element) && interactiveAncestor(element));
  const roleFor = (element) => explicitRole(element) || nativeRole(element) || (optionLike(element) ? "option" : "button");
  // The bare-option overlay must never resurface an element the production predicate rejected
  // for being invisible, hidden, or inert — a hidden input surfaced here would leak its value
  // through the name fallback.
  const overlayEligible = (element) =>
    isElementVisible(element) &&
    !isHidden(element) &&
    !isScriptOrStyle(element) &&
    !(getElementComputedStyle(element)?.pointerEvents === "none" && !element.disabled && !isHoverOnlyElement(element));
  // Overlay: bare options inside an interactive option container are driver-discoverable even
  // though the production predicate ignores them. "Bare" is strict: anything with a native or
  // explicit role (or a frame-family tag) gets production's FULL verdict, including its later
  // vetoes (disabled-select options, frame exclusions) — the overlay never overrides those.
  const overlayVetoedTags = new Set(["html", "iframe", "frame", "frameset"]);
  const bareElement = (element) => !overlayVetoedTags.has(element.tagName.toLowerCase()) && !nativeRole(element) && !explicitRole(element);
  const candidate = (element) => isInteractable(element, hoverStylesMap) || (bareElement(element) && overlayEligible(element) && optionLike(element));

  // A later production scrape must compute hover styles at ITS OWN time: cache ownership is
  // snapshotted in the same evaluation and restored in finally (identity-guarded). Ceiling:
  // two FIRST-TIME builds interleaving is last-writer-wins inside getHoverStylesMap, so the
  // loser costs one spare recompute — never a stale cache.
  const hadHoverCache = Boolean(window.globalHoverStylesMap);
  try {
    hoverStylesMap = await getHoverStylesMap();
    // querySelectorAll excludes the root itself — a scoped observe must still surface the scoped element.
    // The scan deliberately stays light-DOM-only; shadow roots are not traversed.
    return (scopeSelector ? [root, ...root.querySelectorAll("*")] : Array.from(root.querySelectorAll("*")))
      .filter(candidate)
      .map((element) => {
        const tag = element.tagName.toLowerCase();
        const item = { role: roleFor(element), name: text(element), tag, selector: cssPath(element) };
        if (includeValues === true && !isPassword(element) && ["input", "textarea", "select", "option"].includes(tag)) {
          item.value = element.value || element.getAttribute("value") || "";
        }
        if (tag === "select") item.options = Array.from(element.options).map((option) => text(option));
        return item;
      })
      .filter((item) => item.name || item.role);
  } finally {
    if (!hadHoverCache && window.globalHoverStylesMap === hoverStylesMap) {
      window.globalHoverStylesMap = undefined;
    }
  }
}
"""


_NATIVE_OPTION_TARGET_JS = r"""
(element) => {
  if (!element || element.tagName?.toLowerCase() !== "option") return null;
  const select = element.closest("select");
  if (!select) return null;

  const esc = (value) => (window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&"));
  const cssPath = (target) => {
    if (target.id) return `#${esc(target.id)}`;
    const parts = [];
    for (let current = target; current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement; current = current.parentElement) {
      let part = current.tagName.toLowerCase();
      if (current.id) {
        parts.unshift(`#${esc(current.id)}`);
        break;
      }
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter((child) => child.tagName === current.tagName)
        : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  };

  return {
    select_selector: cssPath(select),
    index: Array.prototype.indexOf.call(select.options, element),
    value: element.value ?? element.getAttribute("value") ?? "",
    label: (element.label || element.textContent || "").replace(/\s+/g, " ").trim(),
  };
}
"""


_NATIVE_OPTION_PROBE_TIMEOUT_MS = 1000


@dataclass
class NativeOptionSelection:
    select_selector: str
    value: str | None = None
    label: str | None = None
    index: int | None = None
    selected_by: Literal["index", "value", "label"] = "index"


async def select_native_option_if_targeted(
    page: Any,
    selector: str,
    *,
    timeout: int = 30000,
) -> NativeOptionSelection | None:
    """If ``selector`` targets a native <option>, select it via its parent <select>.

    Native options inside a collapsed select are not actionable click targets in
    Playwright. This keeps selector/ref driven click flows deterministic by
    translating that specific target shape into a select_option call.
    """
    raw_page = getattr(page, "page", page)
    locator_factory = getattr(raw_page, "locator", None)
    if locator_factory is None:
        return None

    locator = locator_factory(selector)
    first_locator = getattr(locator, "first", locator)
    # Bounded classification probe: only decides whether the target is a native <option>.
    # If the element is not readily present, defer to the caller's click (preserving
    # direct-mode fast-fail and resilient-mode waiting) instead of blocking the full
    # action timeout here.
    probe_timeout = min(timeout, _NATIVE_OPTION_PROBE_TIMEOUT_MS)
    try:
        option_info = await first_locator.evaluate(_NATIVE_OPTION_TARGET_JS, timeout=probe_timeout)
    except Exception:
        return None
    if not isinstance(option_info, dict):
        return None

    select_selector = option_info.get("select_selector")
    if not isinstance(select_selector, str) or not select_selector:
        return None

    select_locator = locator_factory(select_selector)
    value = option_info.get("value")
    label = option_info.get("label")
    index = option_info.get("index")
    last_error: Exception | None = None

    # The ref identified ONE specific <option>. Prefer its index so duplicate or empty option
    # values cannot resolve to the wrong option; fall back to value, then label.
    if isinstance(index, int) and index >= 0:
        try:
            await select_locator.select_option(index=index, timeout=timeout)
            return NativeOptionSelection(
                select_selector=select_selector,
                value=str(value) if value is not None else None,
                label=str(label) if label else None,
                index=index,
                selected_by="index",
            )
        except Exception as exc:
            last_error = exc

    if value is not None:
        try:
            await select_locator.select_option(value=str(value), timeout=timeout)
            return NativeOptionSelection(
                select_selector=select_selector,
                value=str(value),
                label=str(label) if label else None,
                index=index if isinstance(index, int) else None,
                selected_by="value",
            )
        except Exception as exc:
            last_error = exc

    if label:
        await select_locator.select_option(label=str(label), timeout=timeout)
        return NativeOptionSelection(
            select_selector=select_selector,
            value=str(value) if value is not None else None,
            label=str(label),
            index=index if isinstance(index, int) else None,
            selected_by="label",
        )

    if last_error is not None:
        raise last_error
    raise ValueError(f"Native option target {selector!r} has no selectable index, value, or label")


@dataclass
class ObservedElement:
    ref: str
    role: str
    name: str
    tag: str
    selector: str | None = None
    value: str | None = None
    options: list[str] | None = None
    match_index: int = 0
    needs_disambiguation: bool = False


class ObserveFrameError(RuntimeError):
    """Failure to evaluate an observe snapshot inside the selected frame."""

    def __init__(self, frame_name: str, frame_url: str, cause: Exception) -> None:
        self.frame_name = frame_name
        self.frame_url = frame_url
        frame_id = frame_name or frame_url or "<unnamed>"
        super().__init__(f"Failed to evaluate observe in frame {frame_id!r}: {cause}")


@dataclass
class ObserveResult:
    url: str
    title: str
    elements: list[ObservedElement]
    element_count: int
    total_on_page: int


def _flatten_a11y_tree(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Recursively flatten an accessibility tree into a flat element list."""
    if node is None:
        return []
    result: list[dict[str, Any]] = []
    if node.get("role") and node["role"] != "WebArea":
        # a11y snapshots carry current input values; drop them at capture so the guarded
        # DOM scan (includeValues gate in _OBSERVE_INTERACTABLES_JS) is the only value source.
        node.pop("value", None)
        result.append(node)
    for child in node.get("children", []):
        result.extend(_flatten_a11y_tree(child))
    return result


def _extract_options(node: dict[str, Any]) -> list[str] | None:
    """Extract option labels from combobox/listbox children."""
    children = node.get("children")
    if not children:
        return None
    opts = [c.get("name", "") for c in children if c.get("role") == "option"]
    return opts if opts else None


async def _get_dom_observe_elements(
    page: Any,
    selector: str | None = None,
    include_values: bool = False,
    *,
    frame_name: str | None = None,
    frame_url: str | None = None,
) -> list[dict[str, Any]]:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        if frame_name is not None:
            raise ObserveFrameError(frame_name, frame_url or "", RuntimeError("frame does not support evaluate"))
        return []
    try:
        async with asyncio.timeout(_DOM_SCAN_TIMEOUT_SECONDS):
            if await evaluate(_DOMUTILS_INTERACTABILITY_READY_JS) is not True:
                await evaluate(JS_FUNCTION_DEFS)
            result = await evaluate(
                _OBSERVE_INTERACTABLES_JS,
                {"scopeSelector": selector, "includeValues": include_values},
            )
    except Exception as exc:
        if frame_name is not None:
            raise ObserveFrameError(frame_name, frame_url or "", exc) from exc
        return []
    if not isinstance(result, list):
        return []
    return [element for element in result if isinstance(element, dict)]


def _merge_dom_observe_elements(
    a11y_elements: list[dict[str, Any]],
    dom_elements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(a11y_elements)
    selector_seen = {element.get("selector") for element in merged if element.get("selector")}

    # Only upgrade an a11y element with a DOM selector when the (role, name, tag) key is
    # unambiguous on both sides. A duplicate label would otherwise risk attaching the
    # selector to the wrong element — a confidently wrong deterministic action. Ambiguous
    # DOM elements still fall through to the append path with their own distinct selector.
    def _key(role: str, name: str, tag: str) -> tuple[str, str, str]:
        return role, name, tag

    a11y_key_counts: dict[tuple[str, str, str], int] = {}
    for element in merged:
        if not element.get("selector"):
            role = element.get("role", "")
            key = _key(role, element.get("name", ""), _ROLE_TO_TAG.get(role, ""))
            a11y_key_counts[key] = a11y_key_counts.get(key, 0) + 1
    dom_key_counts: dict[tuple[str, str, str], int] = {}
    for dom_element in dom_elements:
        key = _key(dom_element.get("role", ""), dom_element.get("name", ""), dom_element.get("tag", ""))
        dom_key_counts[key] = dom_key_counts.get(key, 0) + 1

    for dom_element in dom_elements:
        dom_selector = dom_element.get("selector")
        if dom_selector and dom_selector in selector_seen:
            continue
        dom_key = _key(dom_element.get("role", ""), dom_element.get("name", ""), dom_element.get("tag", ""))
        unambiguous = a11y_key_counts.get(dom_key, 0) == 1 and dom_key_counts.get(dom_key, 0) == 1
        matched_existing = False
        if unambiguous:
            for existing in merged:
                if (
                    not existing.get("selector")
                    and existing.get("role", "") == dom_element.get("role", "")
                    and existing.get("name", "") == dom_element.get("name", "")
                    and _ROLE_TO_TAG.get(existing.get("role", ""), "") == dom_element.get("tag", "")
                ):
                    existing["selector"] = dom_selector
                    if dom_element.get("value") is not None:
                        existing["value"] = dom_element["value"]
                    if dom_element.get("options") and not existing.get("children"):
                        existing["options"] = dom_element.get("options")
                    matched_existing = True
                    if dom_selector:
                        selector_seen.add(dom_selector)
                    break
        if not matched_existing:
            merged.append(dom_element)
            if dom_selector:
                selector_seen.add(dom_selector)

    return merged


async def do_observe(
    page: Any,
    selector: str | None = None,
    interactive_only: bool = True,
    max_elements: int = 50,
    include_values: bool = False,
) -> ObserveResult:
    """Capture interactive elements with stable refs for batch operations."""
    # Execute-step params arrive as untyped JSON; a string like "false" must not
    # enable value capture — the opt-in counts only as a literal boolean True.
    include_values = include_values is True
    working_frame = getattr(page, "_working_frame", None)
    if working_frame is not None:
        # A popup or tab change can make get_page() resolve a different page than the
        # one the selected frame belongs to — observing that frame would report the
        # wrong document as if it were the active page.
        owner = getattr(working_frame, "page", None)
        raw_page = getattr(page, "page", page)
        if owner is not None and owner is not raw_page:
            raise ObserveFrameError(
                working_frame.name,
                working_frame.url,
                RuntimeError("frame belongs to a different page or tab"),
            )
        try:
            detached = working_frame.is_detached()
        except AttributeError:
            detached = False
        if detached:
            raise ObserveFrameError(
                working_frame.name,
                working_frame.url,
                RuntimeError("frame is detached"),
            )
    observe_target = working_frame if working_frame is not None else page
    if working_frame is not None:
        # Legacy page accessibility iframe roots are unreliable; DOM-only avoids cross-document merges.
        snapshot = None
    else:
        accessibility = getattr(page, "accessibility", None)
        if selector and accessibility is not None:
            element_handle = await page.locator(selector).first.element_handle()
            snapshot = await accessibility.snapshot(root=element_handle)
        elif accessibility is not None:
            snapshot = await accessibility.snapshot()
        else:
            snapshot = None

    all_elements = _flatten_a11y_tree(snapshot)
    dom_elements = await _get_dom_observe_elements(
        observe_target,
        selector,
        include_values,
        frame_name=working_frame.name if working_frame is not None else None,
        frame_url=working_frame.url if working_frame is not None else None,
    )

    if interactive_only:
        all_elements = [e for e in all_elements if e.get("role") in INTERACTIVE_ROLES]

    all_elements = _merge_dom_observe_elements(all_elements, dom_elements)

    total = len(all_elements)

    # Compute group sizes against the FULL filtered list (pre-cap) so that kept elements
    # colliding with off-cap siblings still get disambiguated. Also assign a STABLE match
    # ordinal in original (pre-cap, pre-reorder) order so the `nth=N` fallback ref keeps
    # pointing at the right element even when the cap reorders elements below.
    full_group_size: dict[tuple[str, str], int] = {}
    stable_counts: dict[tuple[str, str], int] = {}
    for elem in all_elements:
        gkey = (elem.get("role", ""), elem.get("name", ""))
        full_group_size[gkey] = full_group_size.get(gkey, 0) + 1
        elem["_match_index"] = stable_counts.get(gkey, 0)
        stable_counts[gkey] = elem["_match_index"] + 1

    if total > max_elements:
        # Keep the custom options this scan surfaces from being crowded out of the cap:
        # option-role elements first, then other selector-bearing elements, then the rest.
        options = [e for e in all_elements if e.get("role") == "option"]
        other_selector = [e for e in all_elements if e.get("role") != "option" and e.get("selector")]
        rest = [e for e in all_elements if e.get("role") != "option" and not e.get("selector")]
        capped = (options + other_selector + rest)[:max_elements]
    else:
        capped = all_elements

    observed: list[ObservedElement] = []
    for i, elem in enumerate(capped):
        role = elem.get("role", "")
        name = elem.get("name", "")

        key = (role, name)
        match_index = elem.get("_match_index", 0)

        observed.append(
            ObservedElement(
                ref=f"e{i}",
                role=role,
                name=name,
                tag=elem.get("tag") or _ROLE_TO_TAG.get(role, ""),
                selector=elem.get("selector"),
                value=elem.get("value"),
                options=elem.get("options") or _extract_options(elem),
                match_index=match_index,
                needs_disambiguation=full_group_size[key] > 1,
            )
        )

    if working_frame is not None:
        try:
            title = await working_frame.title()
        except Exception as exc:
            raise ObserveFrameError(working_frame.name, working_frame.url, exc) from exc
    else:
        title = await page.title()

    return ObserveResult(
        url=working_frame.url if working_frame is not None else page.url,
        title=title,
        elements=observed,
        element_count=len(observed),
        total_on_page=total,
    )


def serialize_elements(elements: list[ObservedElement]) -> list[dict[str, Any]]:
    """Serialize observed elements to dicts, filtering empty display fields."""
    result: list[dict[str, Any]] = []
    for e in elements:
        fields: dict[str, Any] = {
            "ref": e.ref,
            "role": e.role,
            "name": e.name,
            "tag": e.tag,
            "selector": e.selector,
            "value": e.value,
            "options": e.options,
        }
        if e.needs_disambiguation:
            fields["match_index"] = e.match_index
        result.append({k: v for k, v in fields.items() if k in _ELEMENT_KEEP_ALWAYS or (v is not None and v != "")})
    return result


def ref_map_from_elements(elements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ref-map/registry entries persist across calls — they hold ref-resolution fields, never input values."""
    return {element["ref"]: {k: v for k, v in element.items() if k != "value"} for element in elements}


def ref_to_selector(elem: dict[str, Any]) -> str:
    """Convert an observed element's a11y data to a Playwright role selector."""
    if selector := elem.get("selector"):
        return selector
    role = elem.get("role", "")
    name = elem.get("name", "")
    if name:
        escaped = name.replace('"', '\\"')
        base = f'role={role}[name="{escaped}"]'
    else:
        base = f"role={role}"
    if "match_index" in elem:
        return f"{base} >> nth={elem['match_index']}"
    return base


# ---------------------------------------------------------------------------
# Execute — batch multi-step execution with ref threading
# ---------------------------------------------------------------------------

# Tools that are blocked after a failed navigate step (DESIGN-3)
_SENSITIVE_TOOLS = frozenset({"type", "evaluate"})

_ALLOWED_EXECUTE_TOOLS = frozenset(
    {
        "navigate",
        "click",
        "type",
        "press_key",
        "select_option",
        "hover",
        "scroll",
        "wait",
        "observe",
        "screenshot",
        "evaluate",
    }
)

MAX_EXECUTE_STEPS = 20


@dataclass
class ExecuteStep:
    tool: str
    params: dict[str, Any] = field(default_factory=dict)


class ToolStepError(RuntimeError):
    """Step failure that preserves the failing tool's structured error payload."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "Tool execution failed"))
        self.error = error


@dataclass
class StepResult:
    step: int
    tool: str
    ok: bool
    wall_ms: int = 0
    data: dict[str, Any] | None = None
    error: str | dict[str, Any] | None = None


@dataclass
class ExecuteResult:
    steps_completed: int
    steps_total: int
    results: list[StepResult]
    error_step: int | None


async def do_execute(
    dispatch_fn: Any,
    steps: list[ExecuteStep],
    stop_on_error: bool = True,
    on_ref_map_update: Callable[[dict[str, dict[str, Any]]], bool] | None = None,
) -> ExecuteResult:
    """Execute a sequence of deterministic browser operations in one batch.

    dispatch_fn: async callable(step, ref_map) -> dict with tool result
    """
    results: list[StepResult] = []
    ref_map: dict[str, dict[str, Any]] = {}
    nav_failed = False

    for i, step in enumerate(steps):
        if step.tool == "navigate":
            ref_map = {}

        # DESIGN-3: Block sensitive ops after failed navigate
        if nav_failed and not stop_on_error and step.tool in _SENSITIVE_TOOLS:
            results.append(
                StepResult(
                    step=i,
                    tool=step.tool,
                    ok=False,
                    error="blocked_by_failed_navigate: refusing to execute sensitive "
                    "operation after navigation failure",
                )
            )
            continue

        t0 = time.monotonic()
        try:
            result = await dispatch_fn(step, ref_map)
            # DESIGN-4: Each observe REPLACES the entire ref_map (not merges).
            # If session publication rejects the snapshot (a concurrent navigation
            # invalidated it), the batch must not act on it either.
            if step.tool == "observe" and result and "elements" in result:
                new_map = ref_map_from_elements(result["elements"])
                if on_ref_map_update is None or on_ref_map_update(new_map):
                    ref_map = new_map
                else:
                    ref_map = {}

            wall_ms = int((time.monotonic() - t0) * 1000)
            results.append(StepResult(step=i, tool=step.tool, ok=True, wall_ms=wall_ms, data=result))

        except Exception as e:
            wall_ms = int((time.monotonic() - t0) * 1000)
            error_payload: str | dict[str, Any] = e.error if isinstance(e, ToolStepError) else str(e)
            results.append(StepResult(step=i, tool=step.tool, ok=False, wall_ms=wall_ms, error=error_payload))
            if step.tool == "observe":
                # A failed observe means the document may have been replaced; refs from
                # an earlier observe in this batch must not survive it.
                ref_map = {}
            if step.tool == "navigate":
                nav_failed = True
            if stop_on_error:
                break

    return ExecuteResult(
        steps_completed=len(results),
        steps_total=len(steps),
        results=results,
        error_step=next((r.step for r in results if not r.ok), None),
    )


_DOM_FALLBACK_OPTION_WAIT_MS = 3000
_CUSTOM_SELECT_KEY_EVENT_RETRY_MS = 1000

_CUSTOM_SELECT_TARGET_JS = r"""
(element) => {
  const tag = element.tagName.toLowerCase();
  const type = tag === "input" ? (element.type || "").toLowerCase() : "";
  const isPassword = tag === "input" && type === "password";
  const haspopupValue = element.getAttribute("aria-haspopup");
  const optionish = Boolean(element.querySelector('[role="option"], [role="listbox"], [data-value]'));
  const related = Boolean(`${element.getAttribute("aria-controls") || ""} ${element.getAttribute("aria-owns") || ""}`.trim());
  const tabindex = element.hasAttribute("tabindex") && Number(element.getAttribute("tabindex")) >= 0;
  const editable = (["input", "textarea"].includes(tag) || element.isContentEditable) &&
    !isPassword && !element.readOnly && !element.disabled &&
    (element.getAttribute("aria-readonly") || "").toLowerCase() !== "true";
  const ownedIds = `${element.getAttribute("aria-controls") || ""} ${element.getAttribute("aria-owns") || ""}`.trim().split(/\s+/).filter(Boolean);
  return {tag, type, isPassword, role: (element.getAttribute("role") || "").toLowerCase(),
    haspopup: haspopupValue !== null && !["", "false"].includes(haspopupValue.toLowerCase()), editable,
    optionish, related, selectlike: tabindex && (optionish || related),
    ownedSelectors: ownedIds.map((id) => "#" + CSS.escape(id))};
}
"""

_CUSTOM_SELECT_IS_OBSERVED_JS = r"""
(element, observedSelectors) => observedSelectors.some((selector) => document.querySelector(selector) === element)
"""

_CUSTOM_SELECT_SCOPED_OPTIONS_JS = r"""
(element, target) => {
  const optionSelectors = target.optionSelectors || [];
  const options = optionSelectors.map((selector) => [selector, document.querySelector(selector)]).filter((entry) => entry[1]);
  const ownedIds = `${element.getAttribute("aria-controls") || ""} ${element.getAttribute("aria-owns") || ""}`.trim().split(/\s+/).filter(Boolean);
  const owned = ownedIds.map((id) => document.getElementById(id)).filter(Boolean);
  let scoped = [];
  if (ownedIds.length) {
    // Declared ownership is exclusive even before the owned root mounts: the poll loop
    // retries until it appears; falling back earlier could click an unrelated widget.
    scoped = options.filter((entry) => owned.some((root) => root.contains(entry[1])));
  } else if (target.bareInput) {
    const before = new Set(target.beforeOptionSelectors || []);
    const widgetSelector = 'input, textarea, select, button, [role="combobox"], [role="listbox"], [role="textbox"], [aria-haspopup], [tabindex]:not([role="option"]), [contenteditable]:not([contenteditable="false"])';
    let ownContainer = null;
    for (let parent = element.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth += 1) {
      const hasOtherWidget = Array.from(parent.querySelectorAll(widgetSelector)).some((node) => node !== element);
      if (hasOtherWidget) break;
      ownContainer = parent;
    }
    scoped = options.filter((entry) => !before.has(entry[0]) || Boolean(ownContainer && ownContainer.contains(entry[1])));
  } else {
    scoped = options.filter((entry) => element.contains(entry[1]));
  }
  if (!ownedIds.length && !target.bareInput && !scoped.length) {
    for (let parent = element.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth += 1) {
      scoped = options.filter((entry) => parent.contains(entry[1]));
      if (scoped.length) break; }
  }
  // The DOM scan can mark the control itself, its display text, and option containers as
  // role=option. Real options are leaf candidates; when container candidates exist, only
  // leaves inside a container count — that excludes display children of the control.
  const containers = scoped.filter((entry) => entry[1] !== element && scoped.some((other) => other[1] !== entry[1] && entry[1].contains(other[1])));
  let leaves = scoped.filter((entry) => entry[1] !== element && !scoped.some((other) => other[1] !== entry[1] && entry[1].contains(other[1])));
  if (containers.length) leaves = leaves.filter((entry) => containers.some((root) => root[1].contains(entry[1])));
  const describe = (entry) => ({selector: entry[0],
    label: (entry[1].innerText || entry[1].textContent || entry[1].getAttribute("aria-label") || "").replace(/\s+/g, " ").trim(),
    value: entry[1].value || entry[1].getAttribute("value") || entry[1].getAttribute("data-value") || ""});
  return leaves.map(describe);
}
"""

_CUSTOM_SELECT_COMMIT_JS = r"""
(element, target) => {
  const option = document.querySelector(target.matched);
  const optionNodes = target.options.map((selector) => document.querySelector(selector)).filter(Boolean);
  const clone = element.cloneNode(true);
  const cloneFor = (node) => {
    const path = [];
    for (let current = node; current && current !== element; current = current.parentElement) {
      const parent = current.parentElement;
      if (!parent) return null;
      path.unshift(Array.prototype.indexOf.call(parent.children, current));
    }
    let current = clone;
    for (const index of path) current = current && current.children[index];
    return current;
  };
  const optionClones = optionNodes.filter((node) => element.contains(node)).map(cloneFor).filter(Boolean);
  optionClones.forEach((node) => node.remove());
  const text = (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim();
  const ownedIds = `${element.getAttribute("aria-controls") || ""} ${element.getAttribute("aria-owns") || ""}`.trim().split(/\s+/).filter(Boolean);
  const roots = [element.parentElement, ...ownedIds.map((id) => document.getElementById(id))].filter(Boolean);
  const seen = new Set();
  const nodeKey = (node) => {
    if (node.id) return `#${node.id}`;
    if (node.tagName.toLowerCase() === "input" && node.type === "hidden" && node.name) {
      return `input:hidden:${node.name}`;
    }
    return null;
  };
  const channelCandidates = [];
  roots.forEach((root) => [root, ...root.querySelectorAll("*")].forEach((node) => {
    if (seen.has(node) || optionNodes.some((optionNode) => optionNode === node || optionNode.contains(node))) return;
    seen.add(node);
    const key = nodeKey(node);
    if (!key) return;
    if (node.tagName.toLowerCase() === "input" && node.type === "hidden") {
      channelCandidates.push({key: `${key}:value`, value: node.value || ""});
    }
    if (node.hasAttribute("data-value")) {
      channelCandidates.push({key: `${key}:data-value`, value: node.getAttribute("data-value") || ""});
    }
  }));
  const keyCounts = channelCandidates.reduce((counts, channel) => {
    counts.set(channel.key, (counts.get(channel.key) || 0) + 1);
    return counts;
  }, new Map());
  const containerChannels = channelCandidates.filter((channel) => keyCounts.get(channel.key) === 1);
  return {
    text, value: element.value || element.getAttribute("value") || "",
    dataValue: element.getAttribute("data-value") || "", containerChannels,
    channelValues: channelCandidates.map((channel) => channel.value),
    expanded: element.getAttribute("aria-expanded"), optionVisible: Boolean(option && option.getClientRects().length),
    optionPresent: Boolean(option),
    optionSelected: Boolean(option && (option.selected || option.getAttribute("aria-selected") === "true"))};
}
"""


class CustomSelectOpenError(RuntimeError):
    """Opening the widget (the initial click/fill) failed before any option was acted on."""


class CustomSelectRestoreError(RuntimeError):
    """The custom-select value could not be restored, so another action path is unsafe."""


class CustomSelectPasswordError(RuntimeError):
    """The target is a password input — a terminal, no-fallback rejection so the secret
    value never reaches the native fill or the AI-fallback LLM payload."""


class CustomSelectClassifyError(RuntimeError):
    """The target could not be classified (detached/navigated mid-probe). The caller must
    fail closed before any value-bearing AI fallback rather than treat it as native deferral."""


class CustomSelectMatchError(RuntimeError):
    def __init__(self, selector: str, requested_option: str, observed_options: list[str]) -> None:
        super().__init__(f"No unambiguous match for {requested_option!r}")
        self.selector = selector
        self.requested_option = requested_option
        self.observed_options = observed_options


def _normalized_option(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _custom_select_commit_channels(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        str(channel["key"]): _normalized_option(channel.get("value"))
        for channel in snapshot.get("containerChannels") or []
        if isinstance(channel, dict) and channel.get("key")
    }


async def _custom_select_dom_elements(page: Any, scan_selectors: list[str] | None) -> list[dict[str, Any]]:
    if not scan_selectors:
        return await _get_dom_observe_elements(page)
    elements: dict[str, dict[str, Any]] = {}
    for scan_selector in scan_selectors:
        for element in await _get_dom_observe_elements(page, scan_selector):
            selector = str(element.get("selector") or "")
            if selector:
                elements[selector] = element
    return list(elements.values())


async def _restore_custom_select_value(control: Any, original_value: str | None, timeout: int) -> None:
    if original_value is None:
        raise CustomSelectRestoreError("Could not restore original value before fallback")
    try:
        await control.fill(original_value, timeout=timeout)
        restored_value = await control.evaluate(
            "element => element.value ?? element.textContent ?? ''",
            timeout=timeout,
        )
    except Exception as e:
        raise CustomSelectRestoreError("Could not restore original value before fallback") from e
    if str(restored_value) != original_value:
        raise CustomSelectRestoreError("Could not verify restored original value before fallback")


async def _scoped_custom_options(
    page: Any,
    control: Any,
    dom_elements: list[dict[str, Any]] | None = None,
    *,
    before_option_selectors: set[str] | None = None,
    bare_input: bool = False,
    scan_selectors: list[str] | None = None,
) -> list[dict[str, Any]]:
    if dom_elements is None:
        dom_elements = await _custom_select_dom_elements(page, scan_selectors)
    options = [element for element in dom_elements if element.get("role") == "option" and element.get("selector")]
    selectors = [str(element["selector"]) for element in options]
    scoped = await control.evaluate(
        _CUSTOM_SELECT_SCOPED_OPTIONS_JS,
        {
            "optionSelectors": selectors,
            "beforeOptionSelectors": list(before_option_selectors or ()),
            "bareInput": bare_input,
        },
    )
    if not isinstance(scoped, list):
        return []
    by_selector = {str(element["selector"]): element for element in options}
    enriched = []
    for entry in scoped:
        element = by_selector.get(str(entry.get("selector") or "")) if isinstance(entry, dict) else None
        if element is not None:
            enriched.append({**element, "label": str(entry.get("label") or ""), "value": str(entry.get("value") or "")})
    return enriched


_LIVE_PASSWORD_JS = (
    "(s) => { const el = document.querySelector(s); "
    "return !!el && el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password'; }"
)


async def _assert_live_target_not_password(page: Any, selector: str) -> None:
    """Re-check the LIVE element immediately before writing to it — guards a TOCTOU swap to a
    password input between classification and the fill. Unknown state fails closed."""
    try:
        is_password = bool(await page.evaluate(_LIVE_PASSWORD_JS, selector))
    except Exception as e:
        raise CustomSelectClassifyError(selector) from e
    if is_password:
        raise CustomSelectPasswordError(selector)


async def do_select_option(
    page: Any,
    selector: str,
    value: str,
    *,
    by_label: bool = False,
    timeout: int = 30000,
    restore_value_on_failure: bool = False,
    fail_closed_on_unknown: bool = False,
) -> str | None:
    """Deterministic custom-select pipeline: classify the control, open (click) or
    filter (fill) it, scope scan-observed options to it, click the unique match, then
    verify a committed-state transition. Returns None to defer to the native path."""
    probe_timeout = min(timeout, _NATIVE_OPTION_PROBE_TIMEOUT_MS)
    try:
        control = page.locator(selector).first
        target = await control.evaluate(_CUSTOM_SELECT_TARGET_JS, timeout=probe_timeout)
    except Exception as e:
        LOG.debug("custom-select classification probe failed", selector=selector, exc_info=True)
        # Unknown classification: the caller decides (native deferral for direct calls,
        # fail-closed before value-bearing AI fallback) — never silently defer to the LLM.
        raise CustomSelectClassifyError(selector) from e

    if target.get("isPassword") or (
        isinstance(target, dict)
        and target.get("tag") == "input"
        and str(target.get("type") or "").casefold() == "password"
    ):
        # Terminal, no fallback: never fill a password field and never let its value reach
        # the native SDK / AI-fallback LLM payload via a hybrid selector+intent call.
        raise CustomSelectPasswordError(selector)
    if not isinstance(target, dict) or target.get("tag") == "select":
        return None
    bare_typeahead = bool(target.get("tag") == "input" and target.get("editable") and not target.get("related"))
    dom_fallback = target.get("role") not in {"combobox", "listbox"} and not target.get("haspopup")
    if dom_fallback or bare_typeahead:
        dom_elements = await _get_dom_observe_elements(page)
    else:
        dom_elements = []
    before_option_selectors = {
        str(element["selector"])
        for element in dom_elements
        if bare_typeahead and element.get("role") == "option" and element.get("selector")
    }
    if dom_fallback:
        observed_selectors = [str(element["selector"]) for element in dom_elements if element.get("selector")]
        try:
            observed = await control.evaluate(_CUSTOM_SELECT_IS_OBSERVED_JS, observed_selectors, timeout=probe_timeout)
        except Exception as e:
            if fail_closed_on_unknown:
                raise CustomSelectClassifyError(selector) from e
            return None
        if not observed:
            return None
        scan_observed_shape = observed and (target.get("optionish") or target.get("related"))
        bare_typeahead = bool(bare_typeahead and observed)
        if not target.get("selectlike") and not scan_observed_shape and not bare_typeahead:
            return None

    started_at = time.monotonic()
    deadline = started_at + timeout / 1000
    # Deliberately overrides larger caller timeouts: dom-fallback ticks re-scan the page,
    # and a control this ambiguous should fail fast to the native path, not poll for minutes.
    option_deadline = min(deadline, started_at + _DOM_FALLBACK_OPTION_WAIT_MS / 1000) if dom_fallback else deadline
    original_value: str | None = None
    if target.get("editable"):
        if restore_value_on_failure:
            try:
                original_value = str(
                    await control.evaluate(
                        "element => element.value ?? element.textContent ?? ''", timeout=probe_timeout
                    )
                )
            except Exception:
                original_value = None
    if target.get("editable"):
        await _assert_live_target_not_password(page, selector)
    try:
        if target.get("editable"):
            await control.fill(value, timeout=timeout)
        else:
            await control.click(timeout=timeout)
    except Exception as e:
        if target.get("editable") and restore_value_on_failure:
            await _restore_custom_select_value(control, original_value, probe_timeout)
        raise CustomSelectOpenError(str(e) or type(e).__name__) from e

    requested = _normalized_option(value)
    options: list[dict[str, Any]] = []
    observed_options: list[str] = []
    matches: list[dict[str, Any]] = []
    commit_target: dict[str, Any] = {}
    before_commit: dict[str, Any] | None = None
    last_click_error: Exception | None = None
    # Owned roots are exclusive scope, so the per-tick re-scan can walk just those
    # subtrees instead of the whole document (full-page walks get expensive on real pages).
    raw_scan_selectors = target.get("ownedSelectors")
    scan_selectors = (
        [str(scan_selector) for scan_selector in raw_scan_selectors if str(scan_selector)]
        if isinstance(raw_scan_selectors, list)
        else None
    )
    key_event_retry_at = min(option_deadline, started_at + _CUSTOM_SELECT_KEY_EVENT_RETRY_MS / 1000)
    used_key_event_retry = False
    saw_scoped_options = False
    poll_delay = 0.1
    # Each tick re-scans fresh: the pre-open/pre-fill scan can only see the collapsed
    # control's display text (not the real options), and matching that display against
    # the requested value clicks a no-op display node instead of a real option.
    while (now := time.monotonic()) < option_deadline:
        options = await _scoped_custom_options(
            page,
            control,
            before_option_selectors=before_option_selectors,
            bare_input=bare_typeahead,
            scan_selectors=scan_selectors,
        )
        saw_scoped_options = saw_scoped_options or bool(options)
        observed_options = _extract_options({"children": options}) or observed_options
        matches = []
        for option in options:
            candidates = {
                _normalized_option(option.get("name")),
                _normalized_option(option.get("label")),
            } - {""}
            if not by_label:
                candidates.add(_normalized_option(option.get("value")))
            if requested in candidates:
                matches.append(option)
        if len(matches) == 1:
            matched_selector = str(matches[0]["selector"])
            commit_target = {
                "matched": matched_selector,
                "options": [str(option["selector"]) for option in options],
            }
            try:
                before_commit = await control.evaluate(_CUSTOM_SELECT_COMMIT_JS, commit_target, timeout=probe_timeout)
            except Exception:
                matches = []
            else:
                try:
                    await page.locator(matched_selector).first.click(timeout=probe_timeout)
                except Exception as e:
                    last_click_error = e
                    matches = []
                else:
                    last_click_error = None
                    break
        if target.get("editable") and not used_key_event_retry and not saw_scoped_options and now >= key_event_retry_at:
            retry_dom_elements = await _custom_select_dom_elements(page, scan_selectors)
            if bare_typeahead:
                before_option_selectors = {
                    str(element["selector"])
                    for element in retry_dom_elements
                    if element.get("role") == "option" and element.get("selector")
                }
            await _assert_live_target_not_password(page, selector)
            try:
                await control.fill("", timeout=timeout)
                await control.press_sequentially(value, timeout=timeout)
            except Exception as e:
                if restore_value_on_failure:
                    await _restore_custom_select_value(control, original_value, probe_timeout)
                raise CustomSelectOpenError(str(e) or type(e).__name__) from e
            used_key_event_retry = True
            poll_delay = 0.1
            continue
        await asyncio.sleep(poll_delay)
        poll_delay = min(poll_delay * 1.5, 0.5)

    if len(matches) != 1:
        if last_click_error is not None:
            raise RuntimeError(str(last_click_error)) from last_click_error
        # Pre-option-click failure: no option was acted on, so a caller that will retry
        # through another path (AI fallback) can safely see the pre-fill value again.
        # Post-option failures above leave the widget alone — replaying them is unsafe.
        if target.get("editable") and restore_value_on_failure:
            await _restore_custom_select_value(control, original_value, probe_timeout)
        if dom_fallback and not bare_typeahead and not observed_options:
            return None
        raise CustomSelectMatchError(selector, value, observed_options)

    matched = matches[0]
    matched_label = str(matched.get("name") or matched.get("label") or value)
    matched_value = str(matched.get("value") or "")
    expected_values = {requested, _normalized_option(matched_value)} - {""}
    display_labels = {
        _normalized_option(matched.get("name")),
        _normalized_option(matched.get("label")),
    } - {""}
    # For any editable target the helper's own fill() wrote the control value, so it is
    # not commit evidence — only the specific data-value, option state, and display text count.
    # Only explicitly attributable selection channels (element value / data-value / hidden
    # inputs) count — never arbitrary data-* like data-loading.
    editable_target = bool(target.get("editable"))
    before = before_commit if isinstance(before_commit, dict) else {}

    def _stable_values(snapshot: dict[str, Any]) -> set[str]:
        values = {_normalized_option(snapshot.get("dataValue"))}
        if not editable_target:
            values.add(_normalized_option(snapshot.get("value")))
        return values - {""}

    before_stable_values = _stable_values(before)
    before_channels = _custom_select_commit_channels(before)
    before_channel_values = {_normalized_option(v) for v in (before.get("channelValues") or [])} - {""}
    before_text = _normalized_option(before.get("text"))
    while time.monotonic() < deadline:
        committed = await control.evaluate(_CUSTOM_SELECT_COMMIT_JS, commit_target, timeout=probe_timeout)
        if isinstance(committed, dict):
            committed_stable_values = _stable_values(committed)
            committed_channels = _custom_select_commit_channels(committed)
            text = _normalized_option(committed.get("text"))
            value_transition = bool(expected_values.intersection(committed_stable_values - before_stable_values))
            # A container channel proves commit when it newly carries the requested value.
            # Same-key change → compare against that key's own before value. New key (possible
            # sibling-removal key shift) → require the value to be absent from all before
            # channels so an unchanged input cannot masquerade as newly added.
            container_transition = editable_target and any(
                value
                and value in expected_values
                and value != before_channels.get(key)
                and (key in before_channels or value not in before_channel_values)
                for key, value in committed_channels.items()
            )
            # Deselection veto: only when the matched option is STILL PRESENT and its selected
            # state flips true->false. An unmounted option (widget closed on commit) is NOT a
            # deselection signal and must fall through to the positive-evidence checks.
            selected_to_unselected = bool(
                before.get("optionSelected") and committed.get("optionPresent") and not committed.get("optionSelected")
            )
            if selected_to_unselected:
                break
            selected_transition = not before.get("optionSelected") and bool(committed.get("optionSelected"))
            stable_idempotent = bool(
                expected_values.intersection(before_stable_values).intersection(committed_stable_values)
            ) or bool(before.get("optionSelected") and committed.get("optionSelected"))
            text_transition = any(
                not (before_text == label or re.search(rf"(?<!\w){re.escape(label)}(?!\w)", before_text))
                and (text == label or re.search(rf"(?<!\w){re.escape(label)}(?!\w)", text))
                for label in display_labels
            )
            if value_transition or container_transition or selected_transition or text_transition or stable_idempotent:
                return matched_label
            attributable_list_close = bool(
                editable_target
                and not before_channels
                and not committed_channels
                and before.get("optionVisible")
                and not committed.get("optionVisible")
            )
            if attributable_list_close:
                return matched_label
        await asyncio.sleep(poll_delay)
        poll_delay = min(poll_delay * 1.5, 0.5)

    raise RuntimeError(f"Custom select did not commit {matched_label!r}")
