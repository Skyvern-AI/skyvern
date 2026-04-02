"""Shared browser operations for MCP tools and CLI commands.

Each function: validate inputs -> call SDK -> return typed result.
Session resolution and output formatting are caller responsibilities.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
