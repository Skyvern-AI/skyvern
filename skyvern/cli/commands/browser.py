from __future__ import annotations

import asyncio
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
from skyvern.cli.core.guards import GuardError, check_password_prompt, validate_wait_until
from skyvern.cli.core.session_ops import do_session_close, do_session_create, do_session_list

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
