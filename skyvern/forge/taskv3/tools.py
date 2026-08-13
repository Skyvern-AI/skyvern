"""Raw-browser tools for the Task V3 native harness.

These drive the run's live Playwright page **directly** (raw DOM / CDP) — no calls into
the task/prompt ecosystem (no LLM-backed observe/act/extract). That is the whole point:
the agent perceives via a raw DOM snapshot and acts by selector, so the only LLM in the
loop is the agent's own persistent conversation.

`build_browser_tools(page, ...)` returns `ToolSpec`s bound to one Playwright page, ready
to hand to `run_agent_tool_loop` alongside `make_finish_tool()`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import structlog

from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.forge.taskv3.preflight import PREFLIGHT_TOOL_NAMES, preflight_tool_action

LOG = structlog.get_logger()

# Raw DOM perception: collect visible interactive elements with a stable selector each.
# Elements without a natural selector get a data-tv3 marker so later actions can target them.
_OBSERVE_JS = r"""
() => {
  const q = 'input,textarea,select,button,a[href],[role=button],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[contenteditable=true]';
  const els = document.querySelectorAll(q);
  const out = [];
  let i = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    let selector = null;
    const uniq = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
    if (el.id) { const s = '#' + CSS.escape(el.id); if (uniq(s)) selector = s; }
    if (!selector && el.getAttribute('data-testid')) { const s = '[data-testid="' + el.getAttribute('data-testid') + '"]'; if (uniq(s)) selector = s; }
    if (!selector && el.name) { const s = el.tagName.toLowerCase() + '[name="' + el.name + '"]'; if (uniq(s)) selector = s; }
    if (!selector) { el.setAttribute('data-tv3', 't' + i); selector = '[data-tv3="t' + i + '"]'; }
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
    if (!label && el.labels && el.labels[0]) label = (el.labels[0].innerText || '').trim();
    if (!label) label = (el.innerText || (el.type === 'password' ? '' : el.value) || '').trim();
    const rec = { i, tag: el.tagName.toLowerCase(), type: el.type || null, selector, label: label.slice(0, 140) };
    if (el.tagName === 'SELECT') rec.options = Array.from(el.options).map((o) => o.value + '|' + o.text).slice(0, 60);
    if (el.type === 'password') { if (el.value) rec.value = '(hidden)'; } else if (el.value) rec.value = String(el.value).slice(0, 100);
    if (el.type === 'checkbox' || el.type === 'radio') rec.checked = !!el.checked;
    else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'radio') rec.checked = el.getAttribute('aria-checked') === 'true';
    if (el.getAttribute('aria-required') === 'true' || el.required) rec.required = true;
    out.push(rec);
    if (++i > 250) break;
  }
  return JSON.stringify({ url: location.href, title: document.title, elements: out });
}
"""


def _spec(
    name: str, description: str, params: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=params, handler=handler)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def build_browser_tools(
    page: Any,
    *,
    downloads_dir: str | None = None,
    organization_id: str | None = None,
) -> list[ToolSpec]:
    """Raw-browser tools bound to `page` (a Playwright Page)."""

    async def _url() -> str:
        try:
            return page.url
        except Exception:
            return ""

    async def observe(_args: dict[str, Any]) -> ToolResult:
        # Bound the one perception call so a wedged page can't hang the turn indefinitely.
        raw = await asyncio.wait_for(page.evaluate(_OBSERVE_JS), timeout=30)
        data = json.loads(raw) if isinstance(raw, str) else raw
        elements = data.get("elements", [])
        # Compact rendering keeps the persistent-conversation prefix small (cost is ~linear in it).
        lines = [f"url={data.get('url')} title={data.get('title')!r} ({len(elements)} interactive elements)"]
        for e in elements:
            extra = ""
            if e.get("value"):
                extra += f" value={e['value']!r}"
            if e.get("options"):
                extra += f" options={e['options']}"
            if e.get("checked") is not None:
                extra += f" checked={e['checked']}"
            if e.get("required"):
                extra += " *required"
            lines.append(
                f"[{e['selector']}] {e['tag']}{('/' + e['type']) if e.get('type') else ''} {e.get('label', '')!r}{extra}"
            )
        return ToolResult.ok("\n".join(lines), data={"count": len(elements)})

    async def get_html(args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return ToolResult.error(f"no element for selector {selector!r}")
            html = await el.inner_html()
        else:
            html = await page.content()
        return ToolResult.ok(html[:20000])

    async def click(args: dict[str, Any]) -> ToolResult:
        selector = args["selector"]
        await page.click(selector, timeout=15000)
        return ToolResult.ok(f"clicked {selector} — now at {await _url()}")

    async def type_text(args: dict[str, Any]) -> ToolResult:
        selector = args["selector"]
        text = args.get("text", "")
        if args.get("clear", True):
            await page.fill(selector, text, timeout=15000)
        else:
            await page.type(selector, text, timeout=15000)
        if args.get("press_enter"):
            await page.press(selector, "Enter")
        return ToolResult.ok(f"typed into {selector}")

    async def select_option(args: dict[str, Any]) -> ToolResult:
        selector = args["selector"]
        if args.get("label") is not None:
            await page.select_option(selector, label=args["label"], timeout=15000)
        else:
            await page.select_option(selector, value=args.get("value"), timeout=15000)
        return ToolResult.ok(f"selected on {selector}")

    async def press_key(args: dict[str, Any]) -> ToolResult:
        key = args["key"]
        selector = args.get("selector")
        if selector:
            await page.press(selector, key)
        else:
            await page.keyboard.press(key)
        return ToolResult.ok(f"pressed {key}")

    async def scroll(args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        if selector:
            el = await page.query_selector(selector)
            if el:
                await el.scroll_into_view_if_needed()
                return ToolResult.ok(f"scrolled {selector} into view")
        amount = int(args.get("amount", 800))
        if args.get("direction") == "up":
            amount = -amount
        await page.mouse.wheel(0, amount)
        return ToolResult.ok(f"scrolled {amount}px")

    async def wait(args: dict[str, Any]) -> ToolResult:
        selector = args.get("selector")
        if selector:
            state = args.get("state", "visible")
            # Cap the model-supplied timeout so a single wait can't stall the run (mirrors the 20s sleep cap).
            timeout_ms = min(int(args.get("timeout_ms", 15000)), 30000)
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            return ToolResult.ok(f"{selector} is {state}")
        await asyncio.sleep(min(float(args.get("time_ms", 1000)) / 1000.0, 20.0))
        return ToolResult.ok("waited")

    async def navigate(args: dict[str, Any]) -> ToolResult:
        from skyvern.utils.url_validators import validate_fetch_url

        url = await asyncio.to_thread(validate_fetch_url, args["url"])
        await page.goto(url, timeout=60000, wait_until="load")
        return ToolResult.ok(f"navigated to {await _url()}")

    async def file_upload(args: dict[str, Any]) -> ToolResult:
        # Lazy import: keeps this module importable for unit tests without the full forge/storage graph.
        from skyvern.forge.sdk.api.files import download_file

        selector = args["selector"]
        source = args["file"]
        local_path = await download_file(source, output_dir=downloads_dir, organization_id=organization_id)
        paths = [local_path]
        el = await page.query_selector(selector)
        if el is None:
            return ToolResult.error(f"no file input for selector {selector!r}")
        await el.set_input_files(paths)
        return ToolResult.ok(f"uploaded {paths} to {selector}")

    tools = [
        _spec(
            "observe",
            "Snapshot the page's visible interactive elements (raw DOM) with a CSS selector, label, type, value, and options for each. Call once per page, then act by selector.",
            _obj({}),
            observe,
        ),
        _spec(
            "get_html",
            "Get raw outer/inner HTML of the page or a specific element (for detail beyond observe).",
            _obj({"selector": {"type": "string", "description": "CSS selector; omit for whole page"}}),
            get_html,
        ),
        _spec(
            "click", "Click an element by CSS selector.", _obj({"selector": {"type": "string"}}, ["selector"]), click
        ),
        _spec(
            "type",
            "Type text into an input/textarea by CSS selector (clears first by default).",
            _obj(
                {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "clear": {"type": "boolean"},
                    "press_enter": {"type": "boolean"},
                },
                ["selector", "text"],
            ),
            type_text,
        ),
        _spec(
            "select_option",
            "Choose an option in a <select> by value or visible label.",
            _obj(
                {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}}, ["selector"]
            ),
            select_option,
        ),
        _spec(
            "press_key",
            "Press a keyboard key (optionally focused on a selector), e.g. Enter, Escape, Tab.",
            _obj({"key": {"type": "string"}, "selector": {"type": "string"}}, ["key"]),
            press_key,
        ),
        _spec(
            "scroll",
            "Scroll the page (direction up/down + amount) or scroll a selector into view.",
            _obj(
                {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer"},
                    "selector": {"type": "string"},
                }
            ),
            scroll,
        ),
        _spec(
            "wait",
            "Wait for a selector to reach a state (visible/attached/hidden) or wait a fixed time_ms.",
            _obj(
                {
                    "selector": {"type": "string"},
                    "state": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                    "time_ms": {"type": "integer"},
                }
            ),
            wait,
        ),
        _spec("navigate", "Navigate the browser to a URL.", _obj({"url": {"type": "string"}}, ["url"]), navigate),
        _spec(
            "file_upload",
            "Upload a file (local path or URL) into a file input by CSS selector.",
            _obj({"selector": {"type": "string"}, "file": {"type": "string"}}, ["selector", "file"]),
            file_upload,
        ),
    ]
    for _tool_spec in tools:
        if _tool_spec.name in ("click", "type", "select_option", "press_key", "file_upload"):
            _tool_spec.billable = True
        if _tool_spec.name in PREFLIGHT_TOOL_NAMES:
            _tool_spec.handler = _with_preflight(_tool_spec.name, _tool_spec.handler, page)
    return tools


def _with_preflight(
    name: str, handler: Callable[[dict[str, Any]], Awaitable[ToolResult]], page: Any
) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
    async def wrapped(args: dict[str, Any]) -> ToolResult:
        preflight_tool_action(name, args, page)
        return await handler(args)

    return wrapped
