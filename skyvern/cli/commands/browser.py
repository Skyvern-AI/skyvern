from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer

from skyvern.cli.commands._output import output, output_error
from skyvern.cli.commands._state import CLIState, clear_state, load_state, save_state
from skyvern.cli.core.artifacts import save_artifact
from skyvern.cli.core.browser_ops import do_act, do_extract, do_navigate, do_screenshot
from skyvern.cli.core.client import get_skyvern
from skyvern.cli.core.guards import (
    CREDENTIAL_HINT,
    PASSWORD_PATTERN,
    VALID_ELEMENT_STATES,
    GuardError,
    check_js_password,
    check_password_prompt,
    resolve_ai_mode,
    validate_button,
    validate_wait_until,
)
from skyvern.cli.core.session_ops import do_session_close, do_session_create, do_session_list
from skyvern.cli.mcp_tools.browser import skyvern_login as tool_login
from skyvern.cli.mcp_tools.browser import skyvern_run_task as tool_run_task

browser_app = typer.Typer(help="Browser automation commands.", no_args_is_help=True)
session_app = typer.Typer(help="Manage browser sessions.", no_args_is_help=True)
browser_app.add_typer(session_app, name="session")


@dataclass(frozen=True)
class ConnectionTarget:
    mode: Literal["cloud", "cdp"]
    session_id: str | None = None
    cdp_url: str | None = None


def _resolve_connection(session: str | None, cdp: str | None) -> ConnectionTarget:
    if session and cdp:
        raise typer.BadParameter("Pass only one of --session or --cdp.")

    if session:
        return ConnectionTarget(mode="cloud", session_id=session)
    if cdp:
        return ConnectionTarget(mode="cdp", cdp_url=cdp)

    state = load_state()
    if state:
        if state.mode == "cdp" and state.cdp_url:
            return ConnectionTarget(mode="cdp", cdp_url=state.cdp_url)
        if state.session_id:
            return ConnectionTarget(mode="cloud", session_id=state.session_id)
        if state.cdp_url:
            return ConnectionTarget(mode="cdp", cdp_url=state.cdp_url)

    raise typer.BadParameter(
        "No active browser connection. Create one with: skyvern browser session create\n"
        "Or connect with: skyvern browser session connect --cdp ws://...\n"
        "Or specify: --session pbs_... / --cdp ws://..."
    )


async def _connect_browser(connection: ConnectionTarget) -> Any:
    skyvern = get_skyvern()
    if connection.mode == "cloud":
        if not connection.session_id:
            raise typer.BadParameter("Cloud mode requires --session or an active cloud session in state.")
        return await skyvern.connect_to_cloud_browser_session(connection.session_id)
    if not connection.cdp_url:
        raise typer.BadParameter("CDP mode requires --cdp or an active CDP URL in state.")
    return await skyvern.connect_to_browser_over_cdp(connection.cdp_url)


def _resolve_ai_target(selector: str | None, intent: str | None, *, operation: str) -> str | None:
    ai_mode, err = resolve_ai_mode(selector, intent)
    if err:
        raise GuardError(
            "Must provide intent, selector, or both",
            (
                f"Use intent='describe what to {operation}' for AI-powered targeting, "
                "or selector='#css-selector' for precise targeting"
            ),
        )
    return ai_mode


def _validate_wait_state(state: str) -> None:
    if state not in VALID_ELEMENT_STATES:
        raise GuardError(f"Invalid state: {state}", "Use visible, hidden, attached, or detached")


def _emit_tool_result(result: dict[str, Any], *, json_output: bool, action: str) -> None:
    if json_output:
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        if not result.get("ok", False):
            raise SystemExit(1)
        return

    if result.get("ok", False):
        output(result.get("data"), action=action, json_mode=False)
        return

    err = result.get("error") or {}
    output_error(str(err.get("message") or "Unknown error"), hint=str(err.get("hint") or ""), json_mode=False)


# ---------------------------------------------------------------------------
# Session commands
# ---------------------------------------------------------------------------


@session_app.command("create")
def session_create(
    timeout: int = typer.Option(60, help="Session timeout in minutes."),
    proxy: str | None = typer.Option(None, help="Proxy location (e.g. RESIDENTIAL)."),
    local: bool = typer.Option(False, "--local", help="Launch a local browser instead of cloud."),
    headless: bool = typer.Option(False, "--headless", help="Run local browser headless."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a new browser session."""
    if local:
        output_error(
            "Local browser sessions are not yet supported in CLI mode.",
            hint="Use MCP (skyvern run mcp) for local browser sessions, or omit --local for cloud sessions.",
            json_mode=json_output,
        )

    async def _run() -> dict:
        skyvern = get_skyvern()
        _browser, result = await do_session_create(
            skyvern,
            timeout=timeout,
            proxy_location=proxy,
        )
        save_state(CLIState(session_id=result.session_id, cdp_url=None, mode="cloud"))
        return {
            "session_id": result.session_id,
            "mode": "cloud",
            "timeout_minutes": result.timeout_minutes,
        }

    try:
        data = asyncio.run(_run())
        output(data, action="session_create", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check your API key and network connection.", json_mode=json_output)


@session_app.command("close")
def session_close(
    session: str | None = typer.Option(None, help="Browser session ID to close."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL to detach from."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Close a browser session."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        if connection.mode == "cdp":
            clear_state()
            return {"cdp_url": connection.cdp_url, "closed": False, "detached": True}

        if not connection.session_id:
            raise typer.BadParameter("Cloud mode requires a browser session ID.")

        skyvern = get_skyvern()
        result = await do_session_close(skyvern, connection.session_id)
        clear_state()
        return {"session_id": result.session_id, "closed": result.closed}

    try:
        data = asyncio.run(_run())
        output(data, action="session_close", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Verify the session ID or CDP URL is correct.", json_mode=json_output)


@session_app.command("connect")
def session_connect(
    session: str | None = typer.Option(None, help="Cloud browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Connect to an existing browser session (cloud or CDP) and persist it as active state."""
    if not session and not cdp:
        raise typer.BadParameter("Specify one of --session or --cdp.")

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        await browser.get_working_page()

        if connection.mode == "cdp":
            save_state(CLIState(session_id=None, cdp_url=connection.cdp_url, mode="cdp"))
            return {"connected": True, "mode": "cdp", "cdp_url": connection.cdp_url}

        save_state(CLIState(session_id=connection.session_id, cdp_url=None, mode="cloud"))
        return {"connected": True, "mode": "cloud", "session_id": connection.session_id}

    try:
        data = asyncio.run(_run())
        output(data, action="session_connect", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Verify the session ID or CDP URL is reachable.", json_mode=json_output)


@session_app.command("list")
def session_list(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List all browser sessions."""

    async def _run() -> list[dict]:
        skyvern = get_skyvern()
        sessions = await do_session_list(skyvern)
        return [asdict(s) for s in sessions]

    try:
        data = asyncio.run(_run())
        output(data, action="session_list", json_mode=json_output)
    except Exception as e:
        output_error(str(e), hint="Check your API key and network connection.", json_mode=json_output)


@session_app.command("get")
def session_get(
    session: str = typer.Option(..., "--session", "--id", help="Browser session ID."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get details for a browser session."""

    async def _run() -> dict:
        skyvern = get_skyvern()
        resolved = await skyvern.get_browser_session(session)
        state = load_state()
        is_current = bool(state and state.mode == "cloud" and state.session_id == session)
        return {
            "session_id": resolved.browser_session_id,
            "status": resolved.status,
            "started_at": resolved.started_at.isoformat() if resolved.started_at else None,
            "completed_at": resolved.completed_at.isoformat() if resolved.completed_at else None,
            "timeout": resolved.timeout,
            "runnable_id": resolved.runnable_id,
            "is_current": is_current,
        }

    try:
        data = asyncio.run(_run())
        output(data, action="session_get", json_mode=json_output)
    except Exception as e:
        output_error(str(e), hint="Verify the session ID exists and is accessible.", json_mode=json_output)


# ---------------------------------------------------------------------------
# Browser commands
# ---------------------------------------------------------------------------


@browser_app.command("navigate")
def navigate(
    url: str = typer.Option(..., help="URL to navigate to."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    timeout: int = typer.Option(30000, help="Navigation timeout in milliseconds."),
    wait_until: str | None = typer.Option(None, help="Wait condition: load, domcontentloaded, networkidle, commit."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Navigate to a URL in the browser session."""

    async def _run() -> dict:
        validate_wait_until(wait_until)
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_navigate(page, url, timeout=timeout, wait_until=wait_until)
        return {"url": result.url, "title": result.title}

    try:
        data = asyncio.run(_run())
        output(data, action="navigate", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check the URL is valid and the session is active.", json_mode=json_output)


@browser_app.command("screenshot")
def screenshot(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    full_page: bool = typer.Option(False, "--full-page", help="Capture the full scrollable page."),
    selector: str | None = typer.Option(None, help="CSS selector to screenshot."),
    output_path: str | None = typer.Option(None, "--output", help="Custom output file path."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Take a screenshot of the current page."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_screenshot(page, full_page=full_page, selector=selector)

        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.data)
            return {"path": str(path), "bytes": len(result.data), "full_page": result.full_page}

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        artifact = save_artifact(
            content=result.data,
            kind="screenshot",
            filename=f"screenshot_{timestamp}.png",
            mime="image/png",
            session_id=connection.session_id,
        )
        return {"path": artifact.path, "bytes": artifact.bytes, "full_page": result.full_page}

    try:
        data = asyncio.run(_run())
        output(data, action="screenshot", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Ensure the session is active and the page has loaded.", json_mode=json_output)


@browser_app.command("evaluate")
def evaluate(
    expression: str = typer.Option(..., help="JavaScript expression to evaluate."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run JavaScript on the current page."""

    async def _run() -> dict:
        check_js_password(expression)
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await page.evaluate(expression)
        return {"result": result}

    try:
        data = asyncio.run(_run())
        output(data, action="evaluate", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check JavaScript syntax and page state.", json_mode=json_output)


@browser_app.command("click")
def click(
    intent: str | None = typer.Option(None, help="Natural language description of the element to click."),
    selector: str | None = typer.Option(None, help="CSS selector or XPath for the element to click."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    timeout: int = typer.Option(30000, help="Max wait time in milliseconds."),
    button: str | None = typer.Option(None, help="Mouse button: left, right, or middle."),
    click_count: int | None = typer.Option(None, "--click-count", help="Number of clicks (2 for double-click)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Click an element using selector, intent, or both."""

    async def _run() -> dict:
        validate_button(button)
        ai_mode = _resolve_ai_target(selector, intent, operation="click")
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        kwargs: dict[str, Any] = {"timeout": timeout}
        if button:
            kwargs["button"] = button
        if click_count is not None:
            kwargs["click_count"] = click_count

        if ai_mode is not None:
            resolved = await page.click(selector=selector, prompt=intent, ai=ai_mode, **kwargs)  # type: ignore[arg-type]
        else:
            assert selector is not None
            resolved = await page.click(selector=selector, **kwargs)

        data: dict[str, Any] = {"selector": selector, "intent": intent, "ai_mode": ai_mode}
        if resolved and resolved != selector:
            data["resolved_selector"] = resolved
        return data

    try:
        data = asyncio.run(_run())
        output(data, action="click", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Element may be hidden, disabled, or not yet available.", json_mode=json_output)


@browser_app.command("hover")
def hover(
    intent: str | None = typer.Option(None, help="Natural language description of the element to hover."),
    selector: str | None = typer.Option(None, help="CSS selector or XPath for the element to hover."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    timeout: int = typer.Option(30000, help="Max wait time in milliseconds."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Hover over an element using selector, intent, or both."""

    async def _run() -> dict:
        ai_mode = _resolve_ai_target(selector, intent, operation="hover")
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        if ai_mode is not None:
            locator = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
        else:
            assert selector is not None
            locator = page.locator(selector)
        await locator.hover(timeout=timeout)
        return {"selector": selector, "intent": intent, "ai_mode": ai_mode}

    try:
        data = asyncio.run(_run())
        output(data, action="hover", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Element may be hidden or not interactable.", json_mode=json_output)


@browser_app.command("type")
def type_text(
    text: str = typer.Option(..., help="Text to type into the input."),
    intent: str | None = typer.Option(None, help="Natural language description of the input field."),
    selector: str | None = typer.Option(None, help="CSS selector or XPath for the input field."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    timeout: int = typer.Option(30000, help="Max wait time in milliseconds."),
    clear: bool = typer.Option(True, "--clear/--no-clear", help="Clear existing content before typing."),
    delay: int | None = typer.Option(None, help="Delay between keystrokes in milliseconds."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Type into an input field using selector, intent, or both."""

    async def _run() -> dict:
        target_text = f"{intent or ''} {selector or ''}"
        if PASSWORD_PATTERN.search(target_text):
            raise GuardError(
                "Cannot type into password fields — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            )

        ai_mode = _resolve_ai_target(selector, intent, operation="type")
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        if selector:
            try:
                is_password = await page.evaluate(
                    "(s) => { const el = document.querySelector(s); return !!(el && el.type === 'password'); }",
                    selector,
                )
            except Exception:
                is_password = False
            if is_password:
                raise GuardError(
                    "Cannot type into password fields — credentials must not be passed through tool calls",
                    CREDENTIAL_HINT,
                )

        if clear:
            if ai_mode is not None:
                await page.fill(selector=selector, value=text, prompt=intent, ai=ai_mode, timeout=timeout)  # type: ignore[arg-type]
            else:
                assert selector is not None
                await page.fill(selector, text, timeout=timeout)
        else:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if delay is not None:
                kwargs["delay"] = delay
            if ai_mode is not None:
                locator = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
                await locator.type(text, **kwargs)
            else:
                assert selector is not None
                await page.type(selector, text, **kwargs)

        return {"selector": selector, "intent": intent, "ai_mode": ai_mode, "text_length": len(text)}

    try:
        data = asyncio.run(_run())
        output(data, action="type", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Element may not be editable or may be obscured.", json_mode=json_output)


@browser_app.command("scroll")
def scroll(
    direction: str = typer.Option(..., help="Direction: up, down, left, right."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    amount: int | None = typer.Option(None, help="Pixels to scroll (default 500)."),
    intent: str | None = typer.Option(None, help="Natural language element to scroll into view."),
    selector: str | None = typer.Option(None, help="CSS selector of scrollable element."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Scroll the page or scroll a targeted element into view."""

    async def _run() -> dict:
        valid_directions = ("up", "down", "left", "right")
        if not intent and direction not in valid_directions:
            raise GuardError(f"Invalid direction: {direction}", "Use up, down, left, or right")

        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        if intent:
            ai_mode = "fallback" if selector else "proactive"
            locator = page.locator(selector=selector, prompt=intent, ai=ai_mode)
            await locator.scroll_into_view_if_needed()
            return {"direction": "into_view", "intent": intent, "selector": selector, "ai_mode": ai_mode}

        pixels = amount or 500
        direction_map = {"up": (0, -pixels), "down": (0, pixels), "left": (-pixels, 0), "right": (pixels, 0)}
        dx, dy = direction_map[direction]

        if selector:
            await page.locator(selector).evaluate(f"el => el.scrollBy({dx}, {dy})")
        else:
            await page.evaluate(f"window.scrollBy({dx}, {dy})")

        return {"direction": direction, "pixels": pixels, "selector": selector}

    try:
        data = asyncio.run(_run())
        output(data, action="scroll", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Scroll failed; check selector and page readiness.", json_mode=json_output)


@browser_app.command("select")
def select(
    value: str = typer.Option(..., help="Option value to select."),
    intent: str | None = typer.Option(None, help="Natural language description of the dropdown."),
    selector: str | None = typer.Option(None, help="CSS selector for the dropdown."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    timeout: int = typer.Option(30000, help="Max wait time in milliseconds."),
    by_label: bool = typer.Option(False, "--by-label", help="Select by visible label instead of value."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Select an option from a dropdown."""

    async def _run() -> dict:
        ai_mode = _resolve_ai_target(selector, intent, operation="select")
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        if ai_mode is not None:
            await page.select_option(selector=selector, value=value, prompt=intent, ai=ai_mode, timeout=timeout)  # type: ignore[arg-type]
        else:
            assert selector is not None
            if by_label:
                await page.page.locator(selector).select_option(label=value, timeout=timeout)
            else:
                await page.select_option(selector, value=value, timeout=timeout)

        return {"selector": selector, "intent": intent, "ai_mode": ai_mode, "value": value, "by_label": by_label}

    try:
        data = asyncio.run(_run())
        output(data, action="select", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check dropdown selector and available options.", json_mode=json_output)


@browser_app.command("press-key")
def press_key(
    key: str = typer.Option(..., help="Key to press (e.g., Enter, Tab, Escape)."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    intent: str | None = typer.Option(None, help="Natural language description of element to focus first."),
    selector: str | None = typer.Option(None, help="CSS selector to focus first."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Press a keyboard key."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        if intent or selector:
            ai_mode, err = resolve_ai_mode(selector, intent)
            if err:
                raise GuardError(
                    "Must provide intent, selector, or both",
                    "Use intent='describe where to press' or selector='#css-selector'",
                )
            if ai_mode is not None:
                locator = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
            else:
                assert selector is not None
                locator = page.locator(selector)
            await locator.press(key)
        else:
            await page.keyboard.press(key)

        return {"key": key, "selector": selector, "intent": intent}

    try:
        data = asyncio.run(_run())
        output(data, action="press_key", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check key name and focused target.", json_mode=json_output)


@browser_app.command("wait")
def wait(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    time_ms: int | None = typer.Option(None, "--time", help="Milliseconds to wait."),
    intent: str | None = typer.Option(None, help="Natural language condition to wait for."),
    selector: str | None = typer.Option(None, help="CSS selector to wait for."),
    state: str = typer.Option("visible", help="Element state: visible, hidden, attached, detached."),
    timeout: int = typer.Option(30000, help="Max wait time in milliseconds."),
    poll_interval: int = typer.Option(5000, "--poll-interval", help="Polling interval for intent waits in ms."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Wait for time, selector state, or AI condition."""

    async def _run() -> dict:
        _validate_wait_state(state)
        if time_ms is None and not selector and not intent:
            raise GuardError(
                "Must provide intent, selector, or time_ms",
                "Use --time, --selector, or --intent to specify what to wait for",
            )

        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()

        waited_for = ""
        if time_ms is not None:
            await page.wait_for_timeout(time_ms)
            waited_for = "time"
        elif intent:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout / 1000
            last_error: Exception | None = None
            while True:
                try:
                    ready = await page.validate(intent)
                    last_error = None
                except Exception as poll_error:
                    ready = False
                    last_error = poll_error

                if ready:
                    waited_for = "intent"
                    break
                if loop.time() >= deadline:
                    if last_error:
                        raise RuntimeError(str(last_error))
                    raise TimeoutError(f"Condition not met within {timeout}ms: {intent}")
                await page.wait_for_timeout(poll_interval)
        else:
            assert selector is not None
            await page.wait_for_selector(selector, state=state, timeout=timeout)
            waited_for = "selector"

        return {"waited_for": waited_for, "state": state, "selector": selector, "intent": intent}

    try:
        data = asyncio.run(_run())
        output(data, action="wait", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Condition was not met within timeout.", json_mode=json_output)


@browser_app.command("act")
def act(
    prompt: str = typer.Option(..., help="Natural language action to perform."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Perform a natural language action on the current page."""

    async def _run() -> dict:
        check_password_prompt(prompt)
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_act(page, prompt)
        return {"prompt": result.prompt, "completed": result.completed}

    try:
        data = asyncio.run(_run())
        output(data, action="act", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Simplify the prompt or break into steps.", json_mode=json_output)


@browser_app.command("extract")
def extract(
    prompt: str = typer.Option(..., help="What data to extract from the page."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    schema: str | None = typer.Option(None, help="JSON schema for structured extraction."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Extract data from the current page using natural language."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_extract(page, prompt, schema=schema)
        return {"prompt": prompt, "extracted": result.extracted}

    try:
        data = asyncio.run(_run())
        output(data, action="extract", json_mode=json_output)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Simplify the prompt or provide a JSON schema.", json_mode=json_output)


@browser_app.command("validate")
def validate(
    prompt: str = typer.Option(..., help="Validation condition to check."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Check whether a natural language condition is true on the current page."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        valid = await page.validate(prompt)
        return {"prompt": prompt, "valid": valid}

    try:
        data = asyncio.run(_run())
        output(data, action="validate", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check the page state and validation prompt.", json_mode=json_output)


@browser_app.command("run-task")
def run_task(
    prompt: str = typer.Option(..., help="Natural language description of the task to automate."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    url: str | None = typer.Option(None, help="URL to navigate to before running."),
    data_extraction_schema: str | None = typer.Option(
        None,
        "--schema",
        "--data-extraction-schema",
        help="JSON Schema string defining what data to extract.",
    ),
    max_steps: int | None = typer.Option(None, "--max-steps", min=1, help="Maximum number of agent steps."),
    timeout_seconds: int = typer.Option(180, "--timeout", min=10, max=1800, help="Timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run a quick one-off browser automation task."""

    async def _run() -> dict[str, Any]:
        connection = _resolve_connection(session, cdp)
        return await tool_run_task(
            prompt=prompt,
            session_id=connection.session_id if connection.mode == "cloud" else None,
            cdp_url=connection.cdp_url if connection.mode == "cdp" else None,
            url=url,
            data_extraction_schema=data_extraction_schema,
            max_steps=max_steps,
            timeout_seconds=timeout_seconds,
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(result, json_output=json_output, action="run_task")
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint="Check the prompt, active connection, and timeout settings.", json_mode=json_output)


@browser_app.command("login")
def login(
    credential_type: str = typer.Option(
        "skyvern",
        "--credential-type",
        help="Credential provider: skyvern, bitwarden, 1password, or azure_vault.",
    ),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    url: str | None = typer.Option(None, help="Login page URL."),
    credential_id: str | None = typer.Option(None, "--credential-id", help="Skyvern credential ID for type=skyvern."),
    bitwarden_item_id: str | None = typer.Option(None, "--bitwarden-item-id", help="Bitwarden item ID."),
    bitwarden_collection_id: str | None = typer.Option(
        None, "--bitwarden-collection-id", help="Bitwarden collection ID."
    ),
    onepassword_vault_id: str | None = typer.Option(None, "--onepassword-vault-id", help="1Password vault ID."),
    onepassword_item_id: str | None = typer.Option(None, "--onepassword-item-id", help="1Password item ID."),
    azure_vault_name: str | None = typer.Option(None, "--azure-vault-name", help="Azure Vault name."),
    azure_vault_username_key: str | None = typer.Option(
        None, "--azure-vault-username-key", help="Azure Vault username key."
    ),
    azure_vault_password_key: str | None = typer.Option(
        None, "--azure-vault-password-key", help="Azure Vault password key."
    ),
    azure_vault_totp_secret_key: str | None = typer.Option(
        None, "--azure-vault-totp-secret-key", help="Azure Vault TOTP secret key."
    ),
    prompt: str | None = typer.Option(None, help="Additional login instructions."),
    totp_identifier: str | None = typer.Option(None, "--totp-identifier", help="TOTP identifier for 2FA."),
    totp_url: str | None = typer.Option(None, "--totp-url", help="URL to fetch TOTP codes."),
    timeout_seconds: int = typer.Option(180, "--timeout", min=10, max=600, help="Timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Log into a site using stored credentials from a supported provider."""

    async def _run() -> dict[str, Any]:
        connection = _resolve_connection(session, cdp)
        return await tool_login(
            credential_type=credential_type,
            session_id=connection.session_id if connection.mode == "cloud" else None,
            cdp_url=connection.cdp_url if connection.mode == "cdp" else None,
            url=url,
            credential_id=credential_id,
            bitwarden_item_id=bitwarden_item_id,
            bitwarden_collection_id=bitwarden_collection_id,
            onepassword_vault_id=onepassword_vault_id,
            onepassword_item_id=onepassword_item_id,
            azure_vault_name=azure_vault_name,
            azure_vault_username_key=azure_vault_username_key,
            azure_vault_password_key=azure_vault_password_key,
            azure_vault_totp_secret_key=azure_vault_totp_secret_key,
            prompt=prompt,
            totp_identifier=totp_identifier,
            totp_url=totp_url,
            timeout_seconds=timeout_seconds,
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(result, json_output=json_output, action="login")
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(
            str(e), hint="Check credential inputs, active connection, and timeout settings.", json_mode=json_output
        )
