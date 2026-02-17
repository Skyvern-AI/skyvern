"""Shared browser operations for MCP tools and CLI commands.

Each function: validate inputs -> call SDK -> return typed result.
Session resolution and output formatting are caller responsibilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
