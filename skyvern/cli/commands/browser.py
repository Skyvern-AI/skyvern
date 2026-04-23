from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from skyvern.cli.core.browser_launcher import LocalBrowserInfo

import typer

from skyvern.cli.commands._output import (
    console,
)
from skyvern.cli.commands._output import emit_tool_result as shared_emit_tool_result
from skyvern.cli.commands._output import (
    output,
    output_error,
)
from skyvern.cli.commands._state import CLIState, clear_state, load_state, save_state
from skyvern.cli.core.artifacts import save_artifact
from skyvern.cli.core.browser_ops import (
    do_act,
    do_extract,
    do_find,
    do_frame_list,
    do_frame_main,
    do_frame_switch,
    do_get_html,
    do_get_styles,
    do_get_value,
    do_navigate,
    do_screenshot,
    do_state_load,
    do_state_save,
)
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
from skyvern.cli.core.ngrok import check_ngrok_auth, detect_ngrok, offer_install_ngrok, offer_setup_auth
from skyvern.cli.core.session_ops import do_session_close, do_session_create, do_session_list
from skyvern.cli.core.telemetry import capture_cli_tool_call
from skyvern.cli.mcp_tools.browser import skyvern_login as tool_login
from skyvern.cli.mcp_tools.browser import skyvern_run_task as tool_run_task
from skyvern.cli.mcp_tools.inspection import skyvern_har_start, skyvern_har_stop

browser_app = typer.Typer(help="Browser automation commands.", no_args_is_help=True)
session_app = typer.Typer(help="Manage browser sessions.", no_args_is_help=True)
frame_app = typer.Typer(help="Manage iframe context.", no_args_is_help=True)
state_app = typer.Typer(help="Save and load browser auth state.", no_args_is_help=True)
storage_app = typer.Typer(help="Read, write, and clear web storage.", no_args_is_help=True)
network_app = typer.Typer(help="Network inspection and interception.", no_args_is_help=True)
browser_app.add_typer(session_app, name="session")
browser_app.add_typer(frame_app, name="frame")
browser_app.add_typer(state_app, name="state")
browser_app.add_typer(storage_app, name="storage")
browser_app.add_typer(network_app, name="network")


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


async def _apply_cli_frame_state(page: Any) -> None:
    """Re-apply saved frame state from CLIState to a fresh SkyvernBrowserPage.

    CLI commands get a new page object each invocation. If the user previously
    ran ``skyvern browser frame switch``, the target frame is persisted in
    CLIState and must be re-entered before executing the action.
    """
    state = load_state()
    if not state:
        return
    selector = state.frame_selector
    name = state.frame_name
    index = state.frame_index
    if selector is None and name is None and index is None:
        return
    try:
        await do_frame_switch(page, selector=selector, name=name, index=index)
    except Exception as e:
        console.print(f"[yellow]Warning: saved frame state is stale, clearing ({e})[/yellow]")
        state.frame_selector = None
        state.frame_name = None
        state.frame_index = None
        save_state(state)


def _validate_wait_state(state: str) -> None:
    if state not in VALID_ELEMENT_STATES:
        raise GuardError(f"Invalid state: {state}", "Use visible, hidden, attached, or detached")


def _emit_tool_result(
    result: dict[str, Any],
    *,
    json_output: bool,
    action: str,
    telemetry_tool_name: str | None = None,
) -> None:
    shared_emit_tool_result(
        result,
        json_output=json_output,
        action=action,
        telemetry_tool_name=telemetry_tool_name,
    )


def _handle_tool_error(e: Exception, *, tool: str, hint: str, json_output: bool) -> None:
    """Common error handler for CLI commands: emit telemetry + output error."""
    capture_cli_tool_call(tool, ok=False, error=e)
    if isinstance(e, GuardError):
        output_error(str(e), hint=e.hint, json_mode=json_output)
    else:
        output_error(str(e), hint=hint, json_mode=json_output)


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
        capture_cli_tool_call("skyvern_browser_session_create", ok=True)
        output(data, action="session_create", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_browser_session_create",
            hint="Check your API key and network connection.",
            json_output=json_output,
        )


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
        capture_cli_tool_call("skyvern_browser_session_close", ok=True)
        output(data, action="session_close", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_browser_session_close",
            hint="Verify the session ID or CDP URL is correct.",
            json_output=json_output,
        )


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
        capture_cli_tool_call("skyvern_browser_session_connect", ok=True)
        output(data, action="session_connect", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_browser_session_connect",
            hint="Verify the session ID or CDP URL is reachable.",
            json_output=json_output,
        )


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
        capture_cli_tool_call("skyvern_browser_session_list", ok=True)
        output(data, action="session_list", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_browser_session_list",
            hint="Check your API key and network connection.",
            json_output=json_output,
        )


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
        capture_cli_tool_call("skyvern_browser_session_get", ok=True)
        output(data, action="session_get", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_browser_session_get",
            hint="Verify the session ID exists and is accessible.",
            json_output=json_output,
        )


# ---------------------------------------------------------------------------
# Network commands
# ---------------------------------------------------------------------------


@network_app.command("requests")
def network_requests_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    url_pattern: str | None = typer.Option(None, "--url", help="Filter by URL regex pattern."),
    status_code: int | None = typer.Option(None, "--status", help="Filter by HTTP status code."),
    method: str | None = typer.Option(None, "--method", help="Filter by HTTP method."),
    resource_type: str | None = typer.Option(None, "--type", help="Filter by resource type (xhr, fetch, script, etc)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List captured network requests."""
    from skyvern.cli.mcp_tools.inspection import skyvern_network_requests

    state = load_state()

    async def _run() -> dict:
        return await skyvern_network_requests(
            session_id=session or (state.session_id if state else None),
            cdp_url=cdp or (state.cdp_url if state else None),
            url_pattern=url_pattern,
            status_code=status_code,
            method=method,
            resource_type=resource_type,
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="network_requests",
            telemetry_tool_name="skyvern_network_requests",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_network_requests", ok=False, error=e)
        output_error(str(e), hint="Ensure a browser session is active.", json_mode=json_output)


@network_app.command("detail")
def network_detail_cmd(
    request_id: int = typer.Argument(..., help="Request ID from network requests output."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show full details (headers + body) for a specific network request."""
    from skyvern.cli.mcp_tools.inspection import skyvern_network_request_detail

    state = load_state()

    async def _run() -> dict:
        return await skyvern_network_request_detail(
            request_id=request_id,
            session_id=session or (state.session_id if state else None),
            cdp_url=cdp or (state.cdp_url if state else None),
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="network_detail",
            telemetry_tool_name="skyvern_network_request_detail",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_network_request_detail", ok=False, error=e)
        output_error(str(e), hint="Ensure a browser session is active.", json_mode=json_output)


@network_app.command("route")
def network_route_cmd(
    url_pattern: str = typer.Argument(..., help="URL glob pattern to intercept. Example: '**/api/*'"),
    action: str = typer.Option("abort", help="Action: 'abort' or 'mock'."),
    mock_status: int = typer.Option(200, "--mock-status", help="HTTP status for mock responses."),
    mock_body: str | None = typer.Option(None, "--mock-body", help="Response body for mock action."),
    mock_content_type: str | None = typer.Option(None, "--mock-content-type", help="Content-Type for mock responses."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Intercept network requests matching a URL pattern (abort or mock)."""
    from skyvern.cli.mcp_tools.inspection import skyvern_network_route

    if action not in ("abort", "mock"):
        output_error(f"Invalid action: {action!r}", hint="Use 'abort' or 'mock'.", json_mode=json_output)
        return

    state = load_state()

    async def _run() -> dict:
        return await skyvern_network_route(
            url_pattern=url_pattern,
            action=cast(Literal["abort", "mock"], action),
            mock_status=mock_status,
            mock_body=mock_body,
            mock_content_type=mock_content_type,
            session_id=session or (state.session_id if state else None),
            cdp_url=cdp or (state.cdp_url if state else None),
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="network_route",
            telemetry_tool_name="skyvern_network_route",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_network_route", ok=False, error=e)
        output_error(str(e), hint="Ensure a browser session is active.", json_mode=json_output)


@network_app.command("unroute")
def network_unroute_cmd(
    url_pattern: str = typer.Argument(..., help="URL pattern to stop intercepting."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Remove a network interception rule."""
    from skyvern.cli.mcp_tools.inspection import skyvern_network_unroute

    state = load_state()

    async def _run() -> dict:
        return await skyvern_network_unroute(
            url_pattern=url_pattern,
            session_id=session or (state.session_id if state else None),
            cdp_url=cdp or (state.cdp_url if state else None),
        )

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="network_unroute",
            telemetry_tool_name="skyvern_network_unroute",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_network_unroute", ok=False, error=e)
        output_error(str(e), hint="Ensure a browser session is active.", json_mode=json_output)


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
    try:
        validate_wait_until(wait_until)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_navigate(page, url, timeout=timeout, wait_until=wait_until)
        cli_state = load_state()
        if cli_state:
            cli_state.frame_selector = None
            cli_state.frame_name = None
            cli_state.frame_index = None
            save_state(cli_state)
        return {"url": result.url, "title": result.title}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_navigate", ok=True)
        output(data, action="navigate", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_navigate",
            hint="Check the URL is valid and the session is active.",
            json_output=json_output,
        )


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
        await _apply_cli_frame_state(page)
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
        capture_cli_tool_call("skyvern_screenshot", ok=True)
        output(data, action="screenshot", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_screenshot",
            hint="Ensure the session is active and the page has loaded.",
            json_output=json_output,
        )


@browser_app.command("evaluate")
def evaluate(
    expression: str = typer.Option(..., help="JavaScript expression to evaluate."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run JavaScript on the current page."""
    try:
        check_js_password(expression)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)
        result = await page.evaluate(expression)
        return {"result": result}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_evaluate", ok=True)
        output(data, action="evaluate", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_evaluate", hint="Check JavaScript syntax and page state.", json_output=json_output
        )


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
    try:
        validate_button(button)
        ai_mode = _resolve_ai_target(selector, intent, operation="click")
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

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
        capture_cli_tool_call("skyvern_click", ok=True)
        output(data, action="click", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_click",
            hint="Element may be hidden, disabled, or not yet available.",
            json_output=json_output,
        )


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
    try:
        ai_mode = _resolve_ai_target(selector, intent, operation="hover")
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

        if ai_mode is not None:
            locator = page.locator(selector=selector, prompt=intent, ai=ai_mode)  # type: ignore[arg-type]
        else:
            assert selector is not None
            locator = page.locator(selector)
        await locator.hover(timeout=timeout)
        return {"selector": selector, "intent": intent, "ai_mode": ai_mode}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_hover", ok=True)
        output(data, action="hover", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_hover", hint="Element may be hidden or not interactable.", json_output=json_output
        )


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
    try:
        target_text = f"{intent or ''} {selector or ''}"
        if PASSWORD_PATTERN.search(target_text):
            raise GuardError(
                "Cannot type into password fields — credentials must not be passed through tool calls",
                CREDENTIAL_HINT,
            )
        ai_mode = _resolve_ai_target(selector, intent, operation="type")
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

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
        capture_cli_tool_call("skyvern_type", ok=True)
        output(data, action="type", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_type", hint="Element may not be editable or may be obscured.", json_output=json_output
        )


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
    try:
        valid_directions = ("up", "down", "left", "right")
        if not intent and direction not in valid_directions:
            raise GuardError(f"Invalid direction: {direction}", "Use up, down, left, or right")
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

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
        capture_cli_tool_call("skyvern_scroll", ok=True)
        output(data, action="scroll", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_scroll", hint="Scroll failed; check selector and page readiness.", json_output=json_output
        )


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
    try:
        ai_mode = _resolve_ai_target(selector, intent, operation="select")
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

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
        capture_cli_tool_call("skyvern_select_option", ok=True)
        output(data, action="select", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e,
            tool="skyvern_select_option",
            hint="Check dropdown selector and available options.",
            json_output=json_output,
        )


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
    ai_mode: str | None = None
    try:
        if intent or selector:
            ai_mode, err = resolve_ai_mode(selector, intent)
            if err:
                raise GuardError(
                    "Must provide intent, selector, or both",
                    "Use intent='describe where to press' or selector='#css-selector'",
                )
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

        if intent or selector:
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
        capture_cli_tool_call("skyvern_press_key", ok=True)
        output(data, action="press_key", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_press_key", hint="Check key name and focused target.", json_output=json_output
        )


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
    try:
        _validate_wait_state(state)
        if time_ms is None and not selector and not intent:
            raise GuardError(
                "Must provide intent, selector, or time_ms",
                "Use --time, --selector, or --intent to specify what to wait for",
            )
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)

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
        capture_cli_tool_call("skyvern_wait", ok=True)
        output(data, action="wait", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_wait", hint="Condition was not met within timeout.", json_output=json_output
        )


@browser_app.command("act")
def act(
    prompt: str = typer.Option(..., help="Natural language action to perform."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Perform a natural language action on the current page."""
    try:
        check_password_prompt(prompt)
    except GuardError as e:
        output_error(str(e), hint=e.hint, json_mode=json_output)
        return

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await _apply_cli_frame_state(page)
        result = await do_act(page, prompt)
        return {"prompt": result.prompt, "completed": result.completed}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_act", ok=True)
        output(data, action="act", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_act", hint="Simplify the prompt or break into steps.", json_output=json_output
        )


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
        await _apply_cli_frame_state(page)
        result = await do_extract(page, prompt, schema=schema)
        return {"prompt": prompt, "extracted": result.extracted}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_extract", ok=True)
        output(data, action="extract", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_extract", hint="Simplify the prompt or provide a JSON schema.", json_output=json_output
        )


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
        await _apply_cli_frame_state(page)
        valid = await page.validate(prompt)
        return {"prompt": prompt, "valid": valid}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_validate", ok=True)
        output(data, action="validate", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(
            e, tool="skyvern_validate", hint="Check the page state and validation prompt.", json_output=json_output
        )


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
        _emit_tool_result(
            result,
            json_output=json_output,
            action="run_task",
            telemetry_tool_name="skyvern_run_task",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_run_task", ok=False, error=e)
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
        _emit_tool_result(
            result,
            json_output=json_output,
            action="login",
            telemetry_tool_name="skyvern_login",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_login", ok=False, error=e)
        output_error(
            str(e), hint="Check credential inputs, active connection, and timeout settings.", json_mode=json_output
        )


# ---------------------------------------------------------------------------
# Browser serve command (POC for local browser + tunnel)
# ---------------------------------------------------------------------------


@browser_app.command("serve")
def serve(
    port: int = typer.Option(9222, help="Server port (exposes CDP proxy + file server)."),
    profile_dir: str | None = typer.Option(
        None,
        "--profile-dir",
        help="Chrome user data directory. Uses existing cookies/auth if specified.",
    ),
    use_local_profile: bool = typer.Option(
        False,
        "--use-local-profile",
        help="Copy cookies/logins from your local Chrome profile so Skyvern can reuse existing sessions.",
    ),
    chrome_profile_name: str = typer.Option(
        "Default",
        "--chrome-profile-name",
        help="Chrome profile subdirectory to copy from (e.g. 'Default', 'Profile 1'). Only used with --use-local-profile.",
    ),
    download_dir: str | None = typer.Option(
        None,
        "--download-dir",
        help="Directory where browser downloads files. Defaults to ~/.skyvern/downloads/{browser_id}.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="SKYVERN_BROWSER_SERVE_API_KEY",
        help="API key for authenticating requests. If set, requires x-api-key header.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Run Chrome in headless mode."),
    full_profile_copy: bool = typer.Option(
        False,
        "--full-profile-copy",
        help="Copy the entire Chrome user data directory instead of just auth-relevant files. Requires Chrome to be closed.",
    ),
    chrome_path: str | None = typer.Option(
        None,
        "--chrome-path",
        help="Path to Chrome executable. Auto-detects if not specified.",
    ),
    tunnel: bool = typer.Option(False, "--tunnel", help="Set up an ngrok tunnel automatically."),
    json_output: bool = typer.Option(False, "--json", help="Output connection info as JSON."),
) -> None:
    """Launch a local Chrome browser with CDP server for Skyvern Cloud.

    This command starts a unified server that provides:
    - CDP WebSocket proxy at /devtools/* (forwards to Chrome)
    - CDP JSON API at /json/* (for listing targets)

    \b
    Downloads are automatically captured by Skyvern via CDP and saved to the
    Skyvern worker's disk.

    \b
    Quick start:
      1. Start the browser serve:
         skyvern browser serve

      2. (Optional) Auto-tunnel with ngrok:
         skyvern browser serve --tunnel

      3. Or manually set up a tunnel:
         ngrok http 9222

      4. Use the tunnel URL for browser_address in your task:
         wss://YOUR_TUNNEL_URL/devtools/browser/...
    """
    import signal

    from skyvern.cli.core.browser_launcher import (
        clone_local_chrome_profile,
        generate_browser_id,
        get_default_chrome_path,
        get_default_download_dir,
        get_default_profile_dir,
        is_chrome_running,
        launch_chrome_with_cdp,
        terminate_browser,
    )
    from skyvern.cli.core.unified_server import UnifiedServer, UnifiedServerConfig
    from skyvern.cli.run_commands import get_pids_on_port

    if use_local_profile and profile_dir:
        raise typer.BadParameter("--use-local-profile and --profile-dir are mutually exclusive.")

    if full_profile_copy and not use_local_profile:
        raise typer.BadParameter("--full-profile-copy requires --use-local-profile.")

    # Full copy needs Chrome closed; selective copy works while Chrome is open
    if use_local_profile and full_profile_copy and is_chrome_running():
        output_error(
            "Chrome is currently running. Please close all Chrome windows first.",
            hint="--full-profile-copy needs to copy your entire Chrome profile, which requires Chrome to be closed. "
            "Close Chrome, then re-run this command. "
            "Or omit --full-profile-copy for a fast selective copy that works while Chrome is open.",
            json_mode=json_output,
        )
        raise SystemExit(1)

    # Chrome runs on internal port, unified server on exposed port
    chrome_internal_port = port + 1000  # e.g., 9222 -> 10222

    # Check for port conflicts
    for check_port, name in [(port, "unified server"), (chrome_internal_port, "Chrome CDP (internal)")]:
        existing_pids = get_pids_on_port(check_port)
        if existing_pids:
            output_error(
                f"Port {check_port} ({name}) is already in use by process(es): {existing_pids}",
                hint="Use a different --port or stop the existing process.",
                json_mode=json_output,
            )
            raise SystemExit(1)

    # Generate unique browser ID for this instance
    browser_id = generate_browser_id()

    # Resolve paths for display
    resolved_chrome_path = chrome_path or get_default_chrome_path()
    resolved_profile_dir = profile_dir or get_default_profile_dir()
    # Use unique download directory if not specified
    resolved_download_dir = download_dir or get_default_download_dir(browser_id)

    if use_local_profile:
        try:
            if full_profile_copy and not json_output:
                with console.status("Copying full Chrome profile — this may take 10-20 seconds..."):
                    clone_local_chrome_profile(chrome_profile_name, Path(resolved_profile_dir), full=True)
            else:
                clone_local_chrome_profile(chrome_profile_name, Path(resolved_profile_dir), full=full_profile_copy)
        except (FileNotFoundError, ValueError, PermissionError) as e:
            output_error(str(e), json_mode=json_output)
            raise SystemExit(1)
        if not json_output:
            copy_mode = "full" if full_profile_copy else "selective"
            output(
                {
                    "status": "profile_cloned",
                    "profile_name": chrome_profile_name,
                    "dest": resolved_profile_dir,
                    "copy_mode": copy_mode,
                },
                action="profile_clone",
                json_mode=False,
            )

    if not json_output:
        output(
            {
                "status": "starting",
                "browser_id": browser_id,
                "chrome_path": resolved_chrome_path,
                "profile_dir": resolved_profile_dir,
                "download_dir": resolved_download_dir,
                "port": port,
                "headless": headless,
                "auth_enabled": api_key is not None,
            },
            action="serve_starting",
            json_mode=False,
        )

    if tunnel and json_output:
        raise typer.BadParameter("--tunnel and --json cannot be used together.")

    browser_info: LocalBrowserInfo | None = None
    unified_server: UnifiedServer | None = None
    ngrok_process: subprocess.Popen[bytes] | None = None
    shutdown_requested = False

    def signal_handler(signum: int, frame: Any) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    # Set up signal handlers
    original_sigint = signal.signal(signal.SIGINT, signal_handler)
    original_sigterm = signal.signal(signal.SIGTERM, signal_handler)

    async def run_serve() -> None:
        nonlocal browser_info, unified_server, shutdown_requested

        # Launch Chrome on internal port
        browser_info = await launch_chrome_with_cdp(
            port=chrome_internal_port,
            profile_dir=resolved_profile_dir,
            headless=headless,
            chrome_path=chrome_path,
            download_dir=resolved_download_dir,
            profile_name=chrome_profile_name if use_local_profile else None,
        )

        # Start unified server on exposed port
        config = UnifiedServerConfig(
            port=port,
            chrome_cdp_port=chrome_internal_port,
            api_key=api_key,
        )
        unified_server = UnifiedServer(config)
        await unified_server.start()

        # Extract browser path from Chrome's CDP URL
        browser_path = browser_info.cdp_ws_url.split("/devtools/browser/")[-1]

        # Output success info
        result = {
            "status": "running",
            "browser_id": browser_id,
            "server_url": f"http://127.0.0.1:{port}",
            "cdp_ws_url": f"ws://127.0.0.1:{port}/devtools/browser/{browser_path}",
            "port": port,
            "profile_dir": browser_info.profile_dir,
            "auth_enabled": api_key is not None,
        }

        if json_output:
            output(result, action="serve", json_mode=True)
        else:
            _print_serve_instructions_unified(result, browser_path)

        # Offer ngrok tunnel
        nonlocal ngrok_process
        if not json_output:
            ngrok_process = await _maybe_start_ngrok_tunnel(port, browser_path, auto=tunnel)

        # Keep the event loop running while server is active
        # This is required because aiohttp's TCPSite needs the event loop to accept connections
        try:
            while not shutdown_requested:
                # Check if Chrome process is still alive
                if browser_info.process.poll() is not None:
                    if not json_output:
                        output_error(
                            "Chrome process exited unexpectedly",
                            hint="Check Chrome logs or try restarting with a different profile.",
                            json_mode=False,
                        )
                    raise SystemExit(1)

                # Sleep briefly to avoid busy-waiting
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run_serve())
    except FileNotFoundError as e:
        output_error(
            str(e),
            hint="Install Chrome or specify the path with --chrome-path.",
            json_mode=json_output,
        )
        raise SystemExit(1)
    except TimeoutError as e:
        output_error(
            str(e),
            hint="Chrome may have failed to start. Check if the port is available.",
            json_mode=json_output,
        )
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass  # Normal shutdown via Ctrl+C
    except Exception as e:
        output_error(str(e), hint="Failed to launch Chrome or unified server.", json_mode=json_output)
        raise SystemExit(1)
    finally:
        # Restore signal handlers
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)

        if not json_output:
            output({"status": "shutting_down"}, action="serve_shutdown", json_mode=False)

        # Stop ngrok
        if ngrok_process is not None:
            ngrok_process.terminate()
            try:
                ngrok_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ngrok_process.kill()

        # Stop unified server
        if unified_server is not None:
            asyncio.run(unified_server.stop())

        # Clean up browser
        if browser_info is not None:
            terminate_browser(browser_info)

        if not json_output:
            output({"status": "stopped"}, action="serve_stopped", json_mode=False)


async def _maybe_start_ngrok_tunnel(port: int, browser_path: str, *, auto: bool) -> subprocess.Popen[bytes] | None:
    """Interactively offer an ngrok tunnel, or start one automatically with ``auto=True``.

    When *auto* is ``True`` (i.e. ``--tunnel`` was passed explicitly), missing
    ngrok is treated as a hard error so scripts don't silently lose tunnel
    functionality.
    """
    import aiohttp
    from rich.panel import Panel
    from rich.prompt import Confirm

    from skyvern.cli.console import console

    # Ask unless --tunnel was passed
    if not auto:
        want = Confirm.ask(
            "\nWould you like to set up an [bold yellow]ngrok tunnel[/bold yellow]?",
            default=False,
        )
        if not want:
            return None

    # Check if ngrok is installed
    ngrok_path = detect_ngrok()
    if not ngrok_path:
        # When auto=True (--tunnel in CI), skip interactive prompts and fail fast
        ngrok_path = offer_install_ngrok(interactive=not auto)
        if not ngrok_path:
            if auto:
                raise SystemExit(1)
            return None

    # Check if ngrok auth token is configured
    # Note: check_ngrok_auth is best-effort (validates config syntax, not token
    # validity). The real validation happens when ngrok tries to start a tunnel.
    if not check_ngrok_auth(ngrok_path):
        if not offer_setup_auth(ngrok_path, interactive=not auto):
            if auto:
                raise SystemExit(1)
            return None

    console.print()
    console.print(f"  Starting ngrok tunnel on port [cyan]{port}[/cyan]...")

    # Capture stderr so we can surface ngrok errors (bad auth, port conflict)
    process = subprocess.Popen(
        [ngrok_path, "http", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Poll the ngrok local API for the tunnel URL (up to 5 seconds)
    tunnel_url: str | None = None
    async with aiohttp.ClientSession() as session:
        for _ in range(10):
            # Check if ngrok died (e.g. bad auth token, port conflict)
            if process.poll() is not None:
                stderr_output = ""
                if process.stderr:
                    stderr_output = process.stderr.read().decode(errors="replace").strip()
                msg = f"ngrok exited with code {process.returncode}"
                if stderr_output:
                    msg += f": {stderr_output}"
                console.print(f"  [bold red]{msg}[/bold red]")
                process.wait()  # Reap the dead process to avoid zombie
                if auto:
                    raise SystemExit(1)
                return None

            try:
                async with session.get("http://127.0.0.1:4040/api/tunnels") as resp:
                    data = await resp.json()
                    for t in data.get("tunnels", []):
                        public_url = t.get("public_url", "")
                        if public_url.startswith("https://"):
                            tunnel_url = public_url
                            break
                    if tunnel_url:
                        break
            except (aiohttp.ClientError, ConnectionError, OSError, ValueError):
                pass
            await asyncio.sleep(0.5)

    if tunnel_url:
        ws_host = tunnel_url.replace("https://", "")
        cdp_url = f"wss://{ws_host}/devtools/browser/{browser_path}"
        console.print()
        console.print(
            Panel(
                f"[bold]Tunnel URL:[/bold]  [cyan]{tunnel_url}[/cyan]\n"
                f"[bold]CDP URL:[/bold]     [cyan]{cdp_url}[/cyan]",
                title="[bold green]Tunnel Active[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        console.print()
    else:
        console.print("  [yellow]ngrok started but tunnel URL not available yet.[/yellow]")
        console.print("  [dim]Check: http://127.0.0.1:4040[/dim]")
        console.print()
        if auto:
            raise SystemExit(1)

    return process


def _print_serve_instructions_unified(result: dict[str, Any], browser_path: str) -> None:
    """Print user-friendly instructions for using the unified server."""
    from rich.panel import Panel

    from skyvern.cli.console import console

    console.print()
    console.print(
        Panel(
            "[bold green]Unified Browser Server Running[/bold green]",
            border_style="green",
            expand=False,
        )
    )
    console.print()
    console.print(f"  [bold]Browser ID:[/bold]         [cyan]{result['browser_id']}[/cyan]")
    console.print(f"  [bold]Server URL:[/bold]         [cyan]{result['server_url']}[/cyan]")
    console.print(f"  [bold]CDP WebSocket URL:[/bold]  [cyan]{result['cdp_ws_url']}[/cyan]")
    console.print(f"  [bold]Port:[/bold]               [cyan]{result['port']}[/cyan]")
    console.print(f"  [bold]Profile directory:[/bold]  [cyan]{result['profile_dir']}[/cyan]")
    console.print(
        f"  [bold]Authentication:[/bold]     [cyan]{'Enabled' if result['auth_enabled'] else 'Disabled'}[/cyan]"
    )
    console.print()

    console.print("[bold yellow]Endpoints:[/bold yellow]")
    console.print()
    console.print("  [bold]CDP Proxy:[/bold]")
    console.print("    GET  /json             - CDP browser info")
    console.print("    GET  /json/list        - List browser targets")
    console.print("    WS   /devtools/...     - CDP WebSocket connection")
    console.print()

    console.print("[bold yellow]Next steps:[/bold yellow]")
    console.print()
    console.print(f"  [bold]1.[/bold] Set up a single HTTP tunnel (port {result['port']}):")
    console.print()
    console.print(f"     [green]ngrok http {result['port']}[/green]")
    console.print()

    console.print("  [bold]2.[/bold] Use the tunnel URL for browser_address in your task:")
    console.print()
    console.print(f"     [cyan]wss://YOUR_TUNNEL_URL/devtools/browser/{browser_path}[/cyan]")
    console.print()
    console.print("     [dim](Replace YOUR_TUNNEL_URL with the ngrok URL)[/dim]")
    console.print()

    console.print("[bold]Press Ctrl+C to stop.[/bold]")
    console.print()


# ---------------------------------------------------------------------------
# Frame commands (iframe switching)
# ---------------------------------------------------------------------------


@frame_app.command("switch")
def frame_switch(
    selector: str | None = typer.Option(None, "--selector", "-s", help="CSS selector for the iframe element."),
    name: str | None = typer.Option(None, "--name", "-n", help="Frame name attribute."),
    index: int | None = typer.Option(None, "--index", "-i", help="Frame index (0 = main)."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Switch into an iframe for subsequent commands."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_frame_switch(page, selector=selector, name=name, index=index)
        state = load_state()
        if state:
            state.frame_selector = selector
            state.frame_name = name
            state.frame_index = index
            save_state(state)
        return {"frame_name": result.name, "frame_url": result.url}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_frame_switch", ok=True)
        output(data, action="frame_switch", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_frame_switch", ok=False, error=e)
        if isinstance(e, GuardError):
            output_error(str(e), hint=e.hint, json_mode=json_output)
        elif isinstance(e, ValueError):
            output_error(str(e), hint="Use 'skyvern browser frame list' to find frames.", json_mode=json_output)
        else:
            output_error(str(e), json_mode=json_output)


@frame_app.command("main")
def frame_main_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Switch back to the main page frame."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        do_frame_main(page)
        state = load_state()
        if state:
            state.frame_selector = None
            state.frame_name = None
            state.frame_index = None
            save_state(state)
        return {"status": "switched_to_main_frame"}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_frame_main", ok=True)
        output(data, action="frame_main", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_frame_main", hint="", json_output=json_output)


@frame_app.command("list")
def frame_list_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List all frames on the current page."""

    async def _run() -> list:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        frames = await do_frame_list(page)
        return [{"index": f.index, "name": f.name, "url": f.url, "is_main": f.is_main} for f in frames]

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_frame_list", ok=True)
        output(data, action="frame_list", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_frame_list", hint="", json_output=json_output)


# ── State persistence commands ──────────────────────────────────────


@state_app.command("save")
def state_save_cmd(
    file_path: str = typer.Argument(help="Path to save state file (JSON)."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Save browser auth state (cookies + localStorage + sessionStorage) to a file."""
    from skyvern.cli.mcp_tools.state import _validate_state_path

    async def _run() -> dict:
        resolved = _validate_state_path(file_path)
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_state_save(page.page, browser, resolved)
        return {
            "file_path": result.file_path,
            "cookie_count": result.cookie_count,
            "local_storage_count": result.local_storage_count,
            "session_storage_count": result.session_storage_count,
            "url": result.url,
        }

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_state_save", ok=True)
        output(data, action="state_save", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_state_save", hint="", json_output=json_output)


@state_app.command("load")
def state_load_cmd(
    file_path: str = typer.Argument(help="Path to state file (JSON) from state save."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Load browser auth state (cookies + localStorage + sessionStorage) from a file."""
    from urllib.parse import urlparse

    from skyvern.cli.mcp_tools.state import _validate_state_path

    async def _run() -> dict:
        resolved = _validate_state_path(file_path, must_exist=True)
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        current_domain = urlparse(page.page.url).hostname or ""
        result = await do_state_load(page.page, browser, resolved, current_domain)
        return {
            "cookie_count": result.cookie_count,
            "local_storage_count": result.local_storage_count,
            "session_storage_count": result.session_storage_count,
            "source_url": result.source_url,
            "skipped_cookies": result.skipped_cookies,
        }

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_state_load", ok=True)
        output(data, action="state_load", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_state_load", hint="", json_output=json_output)


# ── Web storage commands ────────────────────────────────────────────


@storage_app.command("get-session")
def storage_get_session_cmd(
    keys: list[str] | None = typer.Argument(None, help="Specific keys to retrieve. Omit for all."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Read sessionStorage values from the current page."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        if keys:
            items = {}
            for key in keys:
                val = await page.page.evaluate(f"() => window.sessionStorage.getItem({json.dumps(key)})")
                if val is not None:
                    items[key] = val
        else:
            items = await page.page.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")
        return {"items": items, "count": len(items)}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_get_session_storage", ok=True)
        output(data, action="get_session_storage", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_get_session_storage", hint="", json_output=json_output)


@storage_app.command("set-session")
def storage_set_session_cmd(
    key: str = typer.Argument(help="The key to set."),
    value: str = typer.Argument(help="The value to store."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Set a sessionStorage key-value pair."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        await page.page.evaluate("(args) => window.sessionStorage.setItem(args[0], args[1])", [key, value])
        return {"key": key, "value_length": len(value)}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_set_session_storage", ok=True)
        output(data, action="set_session_storage", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_set_session_storage", hint="", json_output=json_output)


@storage_app.command("clear-session")
def storage_clear_session_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Clear all sessionStorage entries."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        count = await page.page.evaluate(
            "() => { const n = window.sessionStorage.length; window.sessionStorage.clear(); return n; }"
        )
        return {"cleared_count": count}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_clear_session_storage", ok=True)
        output(data, action="clear_session_storage", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_clear_session_storage", hint="", json_output=json_output)


@storage_app.command("clear-local")
def storage_clear_local_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Clear all localStorage entries."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        count = await page.page.evaluate(
            "() => { const n = window.localStorage.length; window.localStorage.clear(); return n; }"
        )
        return {"cleared_count": count}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_clear_local_storage", ok=True)
        output(data, action="clear_local_storage", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_clear_local_storage", hint="", json_output=json_output)


# ── Page JS errors command ───────────────────────────────────────────


@browser_app.command("get-errors")
def get_errors_cmd(
    text: str | None = typer.Option(None, "--text", help="Filter by substring match (case-insensitive)."),
    clear: bool = typer.Option(False, "--clear", help="Clear the buffer after reading."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Read uncaught JavaScript errors from the browser page."""
    from skyvern.cli.mcp_tools.inspection import skyvern_get_errors

    async def _run() -> dict:
        return await skyvern_get_errors(text=text, clear=clear, session_id=session, cdp_url=cdp)

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="get_errors",
            telemetry_tool_name="skyvern_get_errors",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_get_errors", ok=False, error=e)
        output_error(str(e), json_mode=json_output)


# ── HAR recording commands ───────────────────────────────────────────


@browser_app.command("har-start")
def har_start_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Start recording network traffic in HAR format."""

    async def _run() -> dict:
        return await skyvern_har_start(session_id=session, cdp_url=cdp)

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="har_start",
            telemetry_tool_name="skyvern_har_start",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_har_start", ok=False, error=e)
        output_error(str(e), json_mode=json_output)


@browser_app.command("har-stop")
def har_stop_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Stop HAR recording and return captured traffic."""

    async def _run() -> dict:
        return await skyvern_har_stop(session_id=session, cdp_url=cdp)

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="har_stop",
            telemetry_tool_name="skyvern_har_stop",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_har_stop", ok=False, error=e)
        output_error(str(e), json_mode=json_output)


# ── DOM Inspection commands ──────────────────────────────────────────


@browser_app.command("get-html")
def get_html_cmd(
    selector: str = typer.Argument(help="CSS or XPath selector for the element."),
    outer: bool = typer.Option(False, "--outer", help="Return outerHTML instead of innerHTML."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get the HTML content of a DOM element."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        html = await do_get_html(page.page, selector, outer=outer)
        return {"html": html, "selector": selector, "outer": outer, "length": len(html)}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_get_html", ok=True)
        output(data, action="get_html", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_get_html", hint="", json_output=json_output)


@browser_app.command("get-value")
def get_value_cmd(
    selector: str = typer.Argument(help="CSS or XPath selector for the input element."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get the current value of a form input element."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        value = await do_get_value(page.page, selector)
        return {"value": value, "selector": selector}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_get_value", ok=True)
        output(data, action="get_value", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_get_value", hint="", json_output=json_output)


@browser_app.command("get-styles")
def get_styles_cmd(
    selector: str = typer.Argument(help="CSS or XPath selector for the element."),
    properties: list[str] | None = typer.Argument(None, help="Specific CSS properties (e.g. color font-size)."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get computed CSS styles from a DOM element."""

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        styles = await do_get_styles(page.page, selector, properties=properties)
        return {"styles": styles, "selector": selector, "count": len(styles)}

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_get_styles", ok=True)
        output(data, action="get_styles", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_get_styles", hint="", json_output=json_output)


# -- Semantic locator command --


@browser_app.command("find")
def find_cmd(
    by: str = typer.Argument(help="Locator type: role, text, label, placeholder, alt, testid."),
    value: str = typer.Argument(help="The text/role/label to match."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Find elements using Playwright semantic locators (role, text, label, etc.)."""
    from skyvern.cli.core.browser_ops import LOCATOR_TYPES

    if by not in LOCATOR_TYPES:
        output_error(
            f"Invalid locator type: {by!r}. Must be one of: {', '.join(sorted(LOCATOR_TYPES))}", json_mode=json_output
        )
        raise typer.Exit(code=2)

    async def _run() -> dict:
        connection = _resolve_connection(session, cdp)
        browser = await _connect_browser(connection)
        page = await browser.get_working_page()
        result = await do_find(page, by=by, value=value)
        return asdict(result)

    try:
        data = asyncio.run(_run())
        capture_cli_tool_call("skyvern_find", ok=True)
        output(data, action="find", json_mode=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        _handle_tool_error(e, tool="skyvern_find", hint="", json_output=json_output)


# ---------------------------------------------------------------------------


@browser_app.command("clipboard-read")
def clipboard_read_cmd(
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Read text from the browser clipboard."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    async def _run() -> dict:
        return await skyvern_clipboard_read(session_id=session, cdp_url=cdp)

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="clipboard_read",
            telemetry_tool_name="skyvern_clipboard_read",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_clipboard_read", ok=False, error=e)
        output_error(str(e), json_mode=json_output)


@browser_app.command("clipboard-write")
def clipboard_write_cmd(
    text: str = typer.Argument(..., help="Text to write to the clipboard."),
    session: str | None = typer.Option(None, help="Browser session ID."),
    cdp: str | None = typer.Option(None, "--cdp", help="CDP WebSocket URL."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Write text to the browser clipboard."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    async def _run() -> dict:
        return await skyvern_clipboard_write(text=text, session_id=session, cdp_url=cdp)

    try:
        result = asyncio.run(_run())
        _emit_tool_result(
            result,
            json_output=json_output,
            action="clipboard_write",
            telemetry_tool_name="skyvern_clipboard_write",
        )
    except typer.BadParameter:
        raise
    except Exception as e:
        capture_cli_tool_call("skyvern_clipboard_write", ok=False, error=e)
        output_error(str(e), json_mode=json_output)
