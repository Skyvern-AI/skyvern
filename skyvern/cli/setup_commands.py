"""Setup commands to register Skyvern with AI coding tools."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from urllib.parse import urlparse

import typer
from dotenv import load_dotenv
from rich.syntax import Syntax

from skyvern.cli.console import console
from skyvern.utils.env_paths import resolve_backend_env_path

# NOTE: skyvern/cli/mcp.py has older setup_*_config() helpers called from
# `skyvern init`. This module supersedes them with remote-first defaults,
# dry-run support, and API key protection. The init-path helpers should be
# migrated to use _upsert_mcp_config() in a follow-up.
setup_app = typer.Typer(help="Register Skyvern MCP with AI coding tools.")

_DEFAULT_REMOTE_URL = "https://mcp.skyvern.com/mcp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_env_credentials() -> tuple[str, str]:
    """Read SKYVERN_API_KEY and SKYVERN_BASE_URL from environment or .env."""
    backend_env = resolve_backend_env_path()
    if backend_env.exists():
        load_dotenv(backend_env, override=False)

    api_key = os.environ.get("SKYVERN_API_KEY", "")
    base_url = os.environ.get("SKYVERN_BASE_URL", "https://api.skyvern.com")
    return api_key, base_url


def _build_remote_mcp_entry(api_key: str, url: str = _DEFAULT_REMOTE_URL) -> dict:
    """Build a streamable-http MCP entry for remote/cloud hosting."""
    entry: dict = {
        "type": "streamable-http",
        "url": url,
    }
    if api_key:
        entry["headers"] = {"x-api-key": api_key}
    return entry


def _build_local_mcp_entry(
    api_key: str,
    base_url: str,
    use_python_path: bool = False,
) -> dict:
    """Build a stdio MCP entry for local self-hosted mode."""
    env_block: dict[str, str] = {}
    if base_url:
        env_block["SKYVERN_BASE_URL"] = base_url
    if api_key:
        env_block["SKYVERN_API_KEY"] = api_key

    if use_python_path:
        return {
            "command": sys.executable,
            "args": ["-m", "skyvern", "run", "mcp"],
            "env": env_block,
        }
    return {
        "command": "skyvern",
        "args": ["run", "mcp"],
        "env": env_block,
    }


def _has_api_key(entry: dict | None) -> bool:
    """Check whether an MCP config entry carries an API key (remote or local format)."""
    if not entry:
        return False
    if entry.get("headers", {}).get("x-api-key"):
        return True
    if entry.get("env", {}).get("SKYVERN_API_KEY"):
        return True
    return False


def _upsert_mcp_config(
    config_path: Path,
    tool_name: str,
    skyvern_entry: dict,
    server_key: str = "Skyvern",
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Read config, diff, prompt, and write. Idempotent."""
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            console.print(f"[red]Cannot parse {config_path}. Fix the JSON and re-run.[/red]")
            raise typer.Exit(code=1)
    else:
        existing = {}

    servers = existing.setdefault("mcpServers", {})
    current = servers.get(server_key)

    if current == skyvern_entry:
        console.print(f"[green]Already configured for {tool_name} (no changes)[/green]")
        return

    # Block any attempt to overwrite an existing API key with an empty one
    if _has_api_key(current) and not _has_api_key(skyvern_entry):
        console.print(
            "[red bold]Error:[/red bold] Existing config has an API key but the new "
            "config does not. Pass --api-key or set SKYVERN_API_KEY in your environment.",
        )
        raise typer.Exit(code=1)

    if current is not None:
        console.print(f"[yellow]Config differs from expected for {tool_name}[/yellow]")
        console.print("\n[bold]Current:[/bold]")
        console.print(Syntax(json.dumps(current, indent=2), "json"))
    else:
        console.print(f"[bold]Adding Skyvern MCP config for {tool_name}:[/bold]")

    console.print("\n[bold]New:[/bold]")
    console.print(Syntax(json.dumps(skyvern_entry, indent=2), "json"))

    if dry_run:
        console.print(f"\n[yellow]Dry run -- no changes written to {config_path}[/yellow]")
        return

    if not yes:
        if not typer.confirm("\nApply changes?"):
            raise typer.Abort()

    servers[server_key] = skyvern_entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]Configured {tool_name} at {config_path}[/green]")


def _build_entry(
    api_key: str,
    base_url: str,
    *,
    local: bool,
    use_python_path: bool,
    url: str | None,
) -> dict:
    if local:
        return _build_local_mcp_entry(api_key, base_url, use_python_path=use_python_path)
    remote_url = url or _DEFAULT_REMOTE_URL
    parsed = urlparse(remote_url)
    if parsed.scheme not in ("http", "https"):
        console.print(f"[red]Invalid URL: {remote_url} (must start with http:// or https://)[/red]")
        raise typer.Exit(code=1)
    return _build_remote_mcp_entry(api_key, url=remote_url)


# ---------------------------------------------------------------------------
# Config path resolvers
# ---------------------------------------------------------------------------


def _claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            console.print("[red]APPDATA environment variable not set on Windows.[/red]")
            raise typer.Exit(code=1)
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    console.print(f"[red]Unsupported platform: {system}[/red]")
    raise typer.Exit(code=1)


def _cursor_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _windsurf_config_path() -> Path:
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def _claude_code_global_config_path() -> Path:
    return Path.home() / ".claude.json"


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

_api_key_opt = typer.Option(None, "--api-key", "-k", help="Skyvern API key (reads from env if omitted)")
_dry_run_opt = typer.Option(False, "--dry-run", help="Show changes without writing")
_yes_opt = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt")
_local_opt = typer.Option(False, "--local", help="Use local stdio transport instead of remote HTTPS")
_python_path_opt = typer.Option(
    False, "--use-python-path", help="(local only) Use python -m skyvern instead of skyvern entrypoint"
)
_url_opt = typer.Option(None, "--url", help="Remote MCP endpoint URL (default: https://mcp.skyvern.com/mcp)")


# ---------------------------------------------------------------------------
# Shared command body
# ---------------------------------------------------------------------------


def _run_setup(
    tool_name: str,
    config_path: Path,
    api_key: str | None,
    dry_run: bool,
    yes: bool,
    local: bool,
    use_python_path: bool,
    url: str | None,
) -> None:
    env_key, env_url = _get_env_credentials()
    key = api_key or env_key
    entry = _build_entry(key, env_url, local=local, use_python_path=use_python_path, url=url)
    _upsert_mcp_config(config_path, tool_name, entry, dry_run=dry_run, yes=yes)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@setup_app.command("claude")
def setup_claude(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
) -> None:
    """Register Skyvern MCP with Claude Desktop (remote by default)."""
    _run_setup("Claude Desktop", _claude_desktop_config_path(), api_key, dry_run, yes, local, use_python_path, url)


@setup_app.command("claude-code")
def setup_claude_code(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    project: bool = typer.Option(False, "--project", help="Write to .mcp.json in current dir instead of global config"),
) -> None:
    """Register Skyvern MCP with Claude Code (remote by default)."""
    config_path = Path.cwd() / ".mcp.json" if project else _claude_code_global_config_path()
    _run_setup("Claude Code", config_path, api_key, dry_run, yes, local, use_python_path, url)


@setup_app.command("cursor")
def setup_cursor(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
) -> None:
    """Register Skyvern MCP with Cursor (remote by default)."""
    _run_setup("Cursor", _cursor_config_path(), api_key, dry_run, yes, local, use_python_path, url)


@setup_app.command("windsurf")
def setup_windsurf(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
) -> None:
    """Register Skyvern MCP with Windsurf (remote by default)."""
    _run_setup("Windsurf", _windsurf_config_path(), api_key, dry_run, yes, local, use_python_path, url)
