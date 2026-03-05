"""Setup commands to register Skyvern with AI coding tools."""

from __future__ import annotations

import copy
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import typer
from dotenv import load_dotenv
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from skyvern.analytics import capture_setup_event
from skyvern.cli.auth_command import run_signup
from skyvern.cli.console import console
from skyvern.cli.skill_commands import get_skill_dirs
from skyvern.utils.env_paths import resolve_backend_env_path

# NOTE: skyvern/cli/mcp.py has older setup_*_config() helpers called from
# `skyvern init`. This module supersedes them with remote-first defaults,
# dry-run support, and API key protection. The init-path helpers should be
# migrated to use _upsert_mcp_config() in a follow-up.
setup_app = typer.Typer(
    help="Register Skyvern MCP with AI coding tools.",
    invoke_without_command=True,
)

_DEFAULT_REMOTE_URL = "https://api.skyvern.com/mcp/"


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
    """Build an HTTP MCP entry for remote/cloud hosting."""
    entry: dict = {
        "type": "http",
        "url": url,
    }
    if api_key:
        entry["headers"] = {"x-api-key": api_key}
    return entry


def _build_mcp_remote_bridge_entry(api_key: str, url: str = _DEFAULT_REMOTE_URL) -> dict:
    """Build an npx mcp-remote entry for clients that only support stdio (e.g. Claude Desktop)."""
    args = ["mcp-remote", url]
    if api_key:
        args.extend(["--header", f"x-api-key:{api_key}"])
    return {
        "command": "npx",
        "args": args,
    }


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
    """Check whether an MCP config entry carries an API key (remote, local, or mcp-remote bridge format)."""
    if not entry:
        return False
    if entry.get("headers", {}).get("x-api-key"):
        return True
    if entry.get("env", {}).get("SKYVERN_API_KEY"):
        return True
    # mcp-remote bridge: API key is in args as "--header", "x-api-key:..."
    args = entry.get("args", [])
    return any(isinstance(a, str) and a.startswith("x-api-key:") for a in args)


def _mask_key(key: str) -> str:
    """Mask an API key for display. Always masks, even short keys."""
    if len(key) > 8:
        return key[:4] + "****" + key[-4:]
    if len(key) > 2:
        return key[:2] + "****"
    return "****"


def _mask_secrets(entry: dict) -> dict:
    """Return a copy of an MCP config entry with API keys masked for display."""
    masked = copy.deepcopy(entry)

    # Remote HTTP format: headers.x-api-key
    if "headers" in masked and "x-api-key" in masked["headers"]:
        key = masked["headers"]["x-api-key"]
        masked["headers"]["x-api-key"] = _mask_key(key)

    # Local stdio format: env.SKYVERN_API_KEY
    if "env" in masked and "SKYVERN_API_KEY" in masked["env"]:
        key = masked["env"]["SKYVERN_API_KEY"]
        masked["env"]["SKYVERN_API_KEY"] = _mask_key(key)

    # mcp-remote bridge format: args contain "x-api-key:..."
    if "args" in masked:
        masked["args"] = [
            (
                "x-api-key:" + _mask_key(a[len("x-api-key:") :])
                if isinstance(a, str) and a.startswith("x-api-key:")
                else a
            )
            for a in masked["args"]
        ]

    return masked


def _upsert_mcp_config(
    config_path: Path,
    tool_name: str,
    skyvern_entry: dict,
    server_key: str = "skyvern",
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
        console.print(Syntax(json.dumps(_mask_secrets(current), indent=2), "json"))
    else:
        console.print(f"[bold]Adding Skyvern MCP config for {tool_name}:[/bold]")

    console.print("\n[bold]New:[/bold]")
    console.print(Syntax(json.dumps(_mask_secrets(skyvern_entry), indent=2), "json"))

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
    use_mcp_remote_bridge: bool = False,
) -> dict:
    if local:
        return _build_local_mcp_entry(api_key, base_url, use_python_path=use_python_path)
    remote_url = url or _DEFAULT_REMOTE_URL
    parsed = urlparse(remote_url)
    if parsed.scheme not in ("http", "https"):
        console.print(f"[red]Invalid URL: {remote_url} (must start with http:// or https://)[/red]")
        raise typer.Exit(code=1)
    if use_mcp_remote_bridge:
        return _build_mcp_remote_bridge_entry(api_key, url=remote_url)
    return _build_remote_mcp_entry(api_key, url=remote_url)


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedTool:
    """Describes an AI coding tool that can be auto-detected and configured."""

    name: str
    config_path_fn: Callable[[], Path]
    is_installed_fn: Callable[[], bool]
    use_mcp_remote_bridge: bool = False


def _is_claude_code_installed() -> bool:
    return shutil.which("claude") is not None


def _is_cursor_installed() -> bool:
    return (Path.home() / ".cursor").is_dir()


def _is_windsurf_installed() -> bool:
    return (Path.home() / ".codeium" / "windsurf").is_dir()


def _is_claude_desktop_installed() -> bool:
    system = platform.system()
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support" / "Claude").is_dir()
    if system == "Linux":
        return (Path.home() / ".config" / "Claude").is_dir()
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return bool(appdata) and (Path(appdata) / "Claude").is_dir()
    return False


def _get_known_tools() -> list[DetectedTool]:
    """Return all known AI coding tools in detection order.

    Note: Codex is not included — it has no stable local config path to detect.
    Users can configure it manually via `skyvern setup codex` if/when that subcommand is added.
    """
    return [
        DetectedTool(
            name="Claude Code",
            config_path_fn=_claude_code_global_config_path,
            is_installed_fn=_is_claude_code_installed,
        ),
        DetectedTool(
            name="Cursor",
            config_path_fn=_cursor_config_path,
            is_installed_fn=_is_cursor_installed,
        ),
        DetectedTool(
            name="Windsurf",
            config_path_fn=_windsurf_config_path,
            is_installed_fn=_is_windsurf_installed,
        ),
        DetectedTool(
            name="Claude Desktop",
            config_path_fn=_claude_desktop_config_path,
            is_installed_fn=_is_claude_desktop_installed,
            use_mcp_remote_bridge=True,
        ),
    ]


def _detect_installed_tools() -> tuple[list[DetectedTool], list[DetectedTool]]:
    """Detect which AI coding tools are installed.

    Returns (detected, not_detected) lists.
    """
    detected: list[DetectedTool] = []
    not_detected: list[DetectedTool] = []
    for tool in _get_known_tools():
        try:
            if tool.is_installed_fn():
                detected.append(tool)
            else:
                not_detected.append(tool)
        except Exception:
            not_detected.append(tool)
    return detected, not_detected


def _acquire_api_key(api_key_flag: str | None, yes: bool) -> str:
    """Resolve an API key from flag, environment, or interactive login.

    Priority: --api-key flag > env/dotenv > interactive browser login.
    """
    if api_key_flag:
        return api_key_flag

    env_key, _ = _get_env_credentials()
    if env_key:
        return env_key

    if yes:
        console.print(
            "[red bold]Error:[/red bold] No API key found. Use --api-key to provide one, or run `skyvern login` first."
        )
        raise typer.Exit(code=1)

    console.print("No API key found. Opening browser to log in...")
    key = run_signup()
    if not key:
        console.print("[red]Login did not return an API key.[/red]")
        raise typer.Exit(code=1)

    return key


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
_url_opt = typer.Option(None, "--url", help="Remote MCP endpoint URL (default: https://api.skyvern.com/mcp/)")


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
    *,
    use_mcp_remote_bridge: bool = False,
) -> None:
    resolved_key = _acquire_api_key(api_key, yes)
    _, env_url = _get_env_credentials()
    entry = _build_entry(
        resolved_key,
        env_url,
        local=local,
        use_python_path=use_python_path,
        url=url,
        use_mcp_remote_bridge=use_mcp_remote_bridge,
    )
    _upsert_mcp_config(config_path, tool_name, entry, dry_run=dry_run, yes=yes)


def _install_skills(project_dir: Path, dry_run: bool = False) -> None:
    """Install bundled skills into a project's .claude/skills/ directory.

    Skips skills that already exist at the destination (non-destructive).
    """
    skills_dst = project_dir / ".claude" / "skills"
    dirs = get_skill_dirs()
    if not dirs:
        return

    installed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in dirs:
        target = skills_dst / d.name
        if target.exists():
            skipped.append(d.name)
            continue
        if not dry_run:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(d, target, ignore=ignore)
            except OSError as e:
                console.print(f"[yellow]Warning: failed to install skill '{d.name}': {e}[/yellow]")
                failed.append(d.name)
                continue
        installed.append(d.name)

    if installed:
        names = ", ".join(installed)
        if dry_run:
            console.print(f"\n[yellow]Dry run — would install skills: {names}[/yellow]")
        else:
            console.print(f"\n[green]Installed skills to {skills_dst}: {names}[/green]")
            if "qa" in installed:
                console.print("[bold]Tip:[/bold] Make a frontend change and type /qa to test it in a real browser.")
    if skipped:
        console.print(f"[dim]Skills already installed: {', '.join(skipped)}[/dim]")


# ---------------------------------------------------------------------------
# Guided quickstart (bare `skyvern setup`)
# ---------------------------------------------------------------------------


@setup_app.callback(invoke_without_command=True)
def setup_guided(
    ctx: typer.Context,
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
) -> None:
    """Guided quickstart: detect installed AI tools and configure MCP for all of them."""
    if ctx.invoked_subcommand is not None:
        return

    console.print(
        Panel(
            "[bold]Skyvern MCP Setup[/bold]\n\n"
            "This wizard will:\n"
            "  1. Find or create your Skyvern API key\n"
            "  2. Detect installed AI coding tools\n"
            "  3. Configure MCP for each detected tool",
            border_style="blue",
        )
    )

    # Step 1: API key
    console.print("[bold]Step 1: API Key[/bold]")
    resolved_key = _acquire_api_key(api_key, yes)
    _, env_url = _get_env_credentials()
    console.print("[green]API key ready.[/green]\n")
    capture_setup_event("quickstart-api-key", success=True)

    # Step 2: Detect tools
    console.print("[bold]Step 2: Detecting installed AI tools...[/bold]")
    detected, not_detected = _detect_installed_tools()

    if detected:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Tool")
        table.add_column("Config Path")
        table.add_column("Status")

        for tool in detected:
            try:
                path = str(tool.config_path_fn())
            except (typer.Exit, SystemExit):
                path = "?"
            table.add_row(tool.name, path, "[green]Detected[/green]")

        console.print(table)

    if not_detected:
        names = ", ".join(t.name for t in not_detected)
        console.print(f"Not detected: {names}\n")
    else:
        console.print()

    capture_setup_event(
        "quickstart-detect",
        success=True,
        extra_data={
            "detected": [t.name for t in detected],
            "not_detected": [t.name for t in not_detected],
        },
    )

    if not detected:
        console.print(
            "[yellow]No supported AI tools detected.[/yellow]\n"
            "You can configure a specific tool manually:\n"
            "  skyvern setup claude-code\n"
            "  skyvern setup cursor\n"
            "  skyvern setup windsurf\n"
            "  skyvern setup claude"
        )
        capture_setup_event("quickstart-no-tools", success=True)
        return

    # Step 3: Configure detected tools
    tool_names = ", ".join(t.name for t in detected)
    console.print(f"[bold]Step 3: Configuring {len(detected)} tool(s)...[/bold]")

    if not yes and not dry_run:
        if not typer.confirm(f"Configure Skyvern MCP for: {tool_names}?", default=True):
            console.print("[yellow]Setup cancelled.[/yellow]")
            raise typer.Abort()

    configured: list[str] = []
    failed: list[str] = []

    for tool in detected:
        try:
            config_path = tool.config_path_fn()
            use_bridge = tool.use_mcp_remote_bridge and not local
            entry = _build_entry(
                resolved_key,
                env_url,
                local=local,
                use_python_path=use_python_path,
                url=url,
                use_mcp_remote_bridge=use_bridge,
            )
            _upsert_mcp_config(config_path, tool.name, entry, dry_run=dry_run, yes=True)
            configured.append(tool.name)
        except (typer.Exit, SystemExit):
            failed.append(tool.name)
            console.print(f"[red]Failed to configure {tool.name}[/red]")
        except Exception as exc:
            failed.append(tool.name)
            console.print(f"[red]Failed to configure {tool.name}: {exc}[/red]")

    console.print()

    if configured:
        configured_str = ", ".join(configured)
        console.print(
            Panel(
                f"[bold green]Setup complete![/bold green]\n\n"
                f"Configured {len(configured)} tool(s): {configured_str}\n\n"
                f'Try asking your AI assistant:\n"Use Skyvern to navigate to example.com"',
                border_style="green",
            )
        )
        capture_setup_event(
            "quickstart-complete",
            success=True,
            extra_data={"configured": configured, "failed": failed},
        )
    else:
        console.print("[red]No tools were configured.[/red]")
        capture_setup_event(
            "quickstart-complete",
            success=False,
            error_type="all_tools_failed",
            extra_data={"failed": failed},
        )


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
    """Register Skyvern MCP with Claude Desktop (uses mcp-remote bridge for remote mode)."""
    _run_setup(
        "Claude Desktop",
        _claude_desktop_config_path(),
        api_key,
        dry_run,
        yes,
        local,
        use_python_path,
        url,
        use_mcp_remote_bridge=not local,
    )


@setup_app.command("claude-code")
def setup_claude_code(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    project: bool = typer.Option(False, "--project", help="Write to .mcp.json in current dir instead of global config"),
    skip_skills: bool = typer.Option(False, "--skip-skills", help="Don't install Claude Code skills (e.g. /qa)"),
) -> None:
    """Register Skyvern MCP with Claude Code and install skills (remote by default)."""
    config_path = Path.cwd() / ".mcp.json" if project else _claude_code_global_config_path()
    _run_setup("Claude Code", config_path, api_key, dry_run, yes, local, use_python_path, url)

    if not skip_skills:
        _install_skills(Path.cwd(), dry_run=dry_run)


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
