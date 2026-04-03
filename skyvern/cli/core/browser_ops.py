"""Shared browser operations for MCP tools and CLI commands.

Each function: validate inputs -> call SDK -> return typed result.
Session resolution and output formatting are caller responsibilities.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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
    if selector:
        element = page.locator(selector)
        data = await element.screenshot()
    else:
        data = await page.screenshot(full_page=full_page)
    return ScreenshotResult(data=data, full_page=full_page)


async def do_act(page: Any, prompt: str) -> ActResult:
    await page.act(prompt)
    return ActResult(prompt=prompt, completed=True)


async def do_extract(
    page: Any,
    prompt: str,
    schema: str | dict[str, Any] | None = None,
) -> ExtractResult:
    parsed_schema = parse_extract_schema(schema)
    extracted = await page.extract(prompt=prompt, schema=parsed_schema)
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
