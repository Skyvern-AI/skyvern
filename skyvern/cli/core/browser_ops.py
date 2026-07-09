"""Shared browser operations for MCP tools and CLI commands.

Each function: validate inputs -> call SDK -> return typed result.
Session resolution and output formatting are caller responsibilities.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.utils.page import SkyvernFrame

from .guards import GuardError


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

_PASSWORD_NAME_RE = re.compile(
    r"\bpass(?:word|phrase|code)s?\b|\bsecret\b|\btoken\b|\bcredential\b|\bpwd\b|\bpasswd\b|\bpin\b",
    re.IGNORECASE,
)

# Structural fields always kept in serialized output; display fields filtered if empty.
_ELEMENT_KEEP_ALWAYS = frozenset({"ref", "role"})

_OBSERVE_INTERACTABLES_JS = r"""
(scopeSelector) => {
  const root = scopeSelector ? document.querySelector(scopeSelector) : document.body;
  if (!root) return [];

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
  const visible = (element) => {
    if (element.tagName.toLowerCase() === "option") return element.parentElement ? visible(element.parentElement) : false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return !element.hidden && element.getAttribute("aria-hidden") !== "true" && style?.display !== "none" && style?.visibility !== "hidden" && style?.visibility !== "collapse" && rect.width > 0 && rect.height > 0;
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
  const widgetRoles = new Set(["button", "checkbox", "combobox", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "radio", "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem"]);
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
  const knownClass = (element) => {
    const className = classes(element);
    return className.includes("dropdown-item") || className.includes("ui-menu-item") || className.includes("pac-item") || className.includes("rddlItem") || className === "option";
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
  const candidate = (element) => {
    if (!visible(element) || (window.getComputedStyle(element)?.pointerEvents === "none" && !element.disabled)) return false;
    const tag = element.tagName.toLowerCase();
    return Boolean(nativeRole(element) || widgetRoles.has(explicitRole(element)) || staticClick(element) || frameworkClick(element) || knownClass(element) || pointer(element) || (tag === "div" && element.getAttribute("tabindex") === "0") || optionLike(element));
  };

  return Array.from(root.querySelectorAll("*"))
    .filter(candidate)
    .map((element) => {
      const tag = element.tagName.toLowerCase();
      const item = { role: roleFor(element), name: text(element), tag, selector: cssPath(element) };
      if (isPassword(element)) {
        item.value = "";
        item.is_password = true;
      } else if (["input", "textarea", "select", "option"].includes(tag)) {
        item.value = element.value || element.getAttribute("value") || "";
      }
      if (tag === "select") item.options = Array.from(element.options).map((option) => text(option));
      return item;
    })
    .filter((item) => item.name || item.role);
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
        result.append(node)
    for child in node.get("children", []):
        result.extend(_flatten_a11y_tree(child))
    return result


def _is_password_field(role: str, name: str) -> bool:
    """DESIGN-2: Detect password-type fields for value redaction."""
    if _PASSWORD_NAME_RE.search(name):
        return True
    return role == "textbox" and "password" in name.lower()


def _extract_options(node: dict[str, Any]) -> list[str] | None:
    """Extract option labels from combobox/listbox children."""
    children = node.get("children")
    if not children:
        return None
    opts = [c.get("name", "") for c in children if c.get("role") == "option"]
    return opts if opts else None


async def _get_dom_observe_elements(page: Any, selector: str | None = None) -> list[dict[str, Any]]:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return []
    try:
        result = await evaluate(_OBSERVE_INTERACTABLES_JS, selector)
    except Exception:
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
                    if dom_element.get("value") is not None and existing.get("value") is None:
                        existing["value"] = dom_element.get("value")
                    if dom_element.get("is_password"):
                        existing["is_password"] = True
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

    # Any accessible name the DOM identifies as a password field redacts EVERY element
    # sharing that (role, name, tag) key — a duplicate accessible name must not let an
    # unflagged a11y value slip through.
    password_keys = {
        _key(dom_element.get("role", ""), dom_element.get("name", ""), dom_element.get("tag", ""))
        for dom_element in dom_elements
        if dom_element.get("is_password")
    }
    if password_keys:
        # Keys present among the ORIGINAL a11y elements (NOT the appended DOM elements — an
        # appended DOM password would otherwise "match" itself and defeat the fail-closed check).
        a11y_password_keys = {
            _key(e.get("role", ""), e.get("name", ""), _ROLE_TO_TAG.get(e.get("role", ""), "")) for e in a11y_elements
        }
        for element in merged:
            role = element.get("role", "")
            if _key(role, element.get("name", ""), _ROLE_TO_TAG.get(role, "")) in password_keys:
                element["is_password"] = True
        if password_keys - a11y_password_keys:
            # Fail closed: a DOM password field could not be mapped to any original a11y element
            # (unlabeled or divergent accessible name). Redact every a11y textbox value so a
            # password can never leak through an unmatched pairing.
            for element in merged:
                if element.get("role") == "textbox" and element.get("value"):
                    element["is_password"] = True
    return merged


async def do_observe(
    page: Any,
    selector: str | None = None,
    interactive_only: bool = True,
    max_elements: int = 50,
) -> ObserveResult:
    """Capture interactive elements with stable refs for batch operations."""
    accessibility = getattr(page, "accessibility", None)
    if selector and accessibility is not None:
        element_handle = await page.locator(selector).first.element_handle()
        snapshot = await accessibility.snapshot(root=element_handle)
    elif accessibility is not None:
        snapshot = await accessibility.snapshot()
    else:
        snapshot = None

    all_elements = _flatten_a11y_tree(snapshot)
    dom_elements = await _get_dom_observe_elements(page, selector)

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
        value = elem.get("value")

        # DESIGN-2: Redact password field values — by DOM input type (is_password) or name.
        if elem.get("is_password") or (value and _is_password_field(role, name)):
            value = "***"

        key = (role, name)
        match_index = elem.get("_match_index", 0)

        observed.append(
            ObservedElement(
                ref=f"e{i}",
                role=role,
                name=name,
                tag=elem.get("tag") or _ROLE_TO_TAG.get(role, ""),
                selector=elem.get("selector"),
                value=value,
                options=elem.get("options") or _extract_options(elem),
                match_index=match_index,
                needs_disambiguation=full_group_size[key] > 1,
            )
        )

    return ObserveResult(
        url=page.url,
        title=await page.title(),
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


@dataclass
class StepResult:
    step: int
    tool: str
    ok: bool
    wall_ms: int = 0
    data: dict[str, Any] | None = None
    error: str | None = None


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
) -> ExecuteResult:
    """Execute a sequence of deterministic browser operations in one batch.

    dispatch_fn: async callable(step, ref_map) -> dict with tool result
    """
    results: list[StepResult] = []
    ref_map: dict[str, dict[str, Any]] = {}
    nav_failed = False

    for i, step in enumerate(steps):
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
            wall_ms = int((time.monotonic() - t0) * 1000)
            results.append(StepResult(step=i, tool=step.tool, ok=True, wall_ms=wall_ms, data=result))

            # DESIGN-4: Each observe REPLACES the entire ref_map (not merges)
            if step.tool == "observe" and result and "elements" in result:
                ref_map = {elem["ref"]: elem for elem in result["elements"]}

        except Exception as e:
            wall_ms = int((time.monotonic() - t0) * 1000)
            results.append(StepResult(step=i, tool=step.tool, ok=False, wall_ms=wall_ms, error=str(e)))
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
