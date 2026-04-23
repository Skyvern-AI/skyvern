"""Skyvern dependency diagnostics."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import typer
from rich.panel import Panel
from rich.table import Table

from skyvern.cli.console import console

doctor_app = typer.Typer(help="Check Skyvern installation health.")


@dataclass
class CheckResult:
    name: str
    status: Literal["ok", "warn", "error"]
    detail: str
    hint: str = field(default="")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python_version() -> CheckResult:
    vi = sys.version_info
    detail = sys.version
    if vi < (3, 11):
        return CheckResult(
            name="Python Version",
            status="error",
            detail=detail,
            hint="Skyvern requires Python 3.11-3.13",
        )
    if vi >= (3, 14):
        return CheckResult(
            name="Python Version",
            status="warn",
            detail=detail,
            hint="Skyvern requires Python 3.11-3.13",
        )
    return CheckResult(name="Python Version", status="ok", detail=detail)


def _check_config() -> CheckResult:
    from dotenv import load_dotenv

    from skyvern.utils.env_paths import resolve_backend_env_path

    env_path = resolve_backend_env_path()
    if env_path.exists():
        # Intentional: loads .env so subsequent checks (connectivity) use configured values
        load_dotenv(env_path, override=False)

    api_key = os.environ.get("SKYVERN_API_KEY")
    detail = f".env: {env_path}"
    if api_key:
        return CheckResult(name="Config / Credentials", status="ok", detail=detail)
    return CheckResult(
        name="Config / Credentials",
        status="warn",
        detail=detail,
        hint="Run `skyvern login` or set SKYVERN_API_KEY in your .env",
    )


def _check_playwright_browser() -> CheckResult:
    # First try: dry-run via playwright CLI
    try:
        result = subprocess.run(
            ["playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(name="Playwright Browser", status="ok", detail="chromium found (playwright --dry-run)")
    except Exception:
        pass

    # Second try: filesystem check
    browsers_path_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path_env:
        candidates = [Path(browsers_path_env)]
    else:
        system = platform.system()
        if system == "Darwin":
            candidates = [Path.home() / "Library" / "Caches" / "ms-playwright"]
        elif system == "Windows":
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            candidates = [Path(local_appdata) / "ms-playwright"] if local_appdata else []
        else:
            candidates = [Path.home() / ".cache" / "ms-playwright"]

    for cache_dir in candidates:
        if cache_dir.is_dir():
            chromium_dirs = list(cache_dir.glob("chromium-*"))
            if chromium_dirs:
                return CheckResult(
                    name="Playwright Browser",
                    status="ok",
                    detail=f"chromium found in {cache_dir}",
                )

    return CheckResult(
        name="Playwright Browser",
        status="error",
        detail="chromium not found",
        hint="Run: playwright install chromium",
    )


def _check_port_8000() -> CheckResult:
    try:
        port = int(os.environ.get("PORT", "8000"))
    except (ValueError, TypeError):
        port = 8000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        return CheckResult(
            name=f"Port {port}",
            status="warn",
            detail=f"port {port} is in use (server may already be running)",
            hint="If unintentional, run `skyvern stop` or set PORT=XXXX",
        )
    except (ConnectionRefusedError, OSError):
        return CheckResult(name=f"Port {port}", status="ok", detail=f"port {port} is free")
    finally:
        sock.close()


def _check_api_connectivity() -> CheckResult:
    base_url = os.environ.get("SKYVERN_BASE_URL", "https://api.skyvern.com")
    url = base_url.rstrip("/") + "/api/v1/heartbeat"
    try:
        response = httpx.get(url, timeout=3.0)
        if response.status_code == 200:
            return CheckResult(
                name="API Connectivity",
                status="ok",
                detail=f"GET {url} -> {response.status_code}",
            )
        hint = "Check your internet connection or firewall settings"
        if 400 <= response.status_code < 500:
            hint = "Check SKYVERN_BASE_URL and SKYVERN_API_KEY configuration"
        return CheckResult(
            name="API Connectivity",
            status="warn",
            detail=f"GET {url} -> {response.status_code}",
            hint=hint,
        )
    except Exception as exc:
        return CheckResult(
            name="API Connectivity",
            status="warn",
            detail=f"unreachable: {exc}",
            hint="Check your internet connection or firewall settings",
        )


def _mcp_config_paths() -> list[tuple[str, Path]]:
    """Return (tool_name, config_path) pairs for known AI coding tools. Stdlib only."""
    home = Path.home()
    system = platform.system()
    appdata = os.environ.get("APPDATA", "")

    # WSL: MCP tools write configs to the Windows user home, not the Linux home
    wsl_home: Path | None = None
    try:
        from skyvern.utils import detect_os, get_windows_appdata_roaming  # noqa: PLC0415

        if detect_os() == "wsl":
            roaming = get_windows_appdata_roaming()
            if roaming:
                wsl_home = roaming.parent.parent
    except Exception:
        pass
    tool_home = wsl_home or home

    paths: list[tuple[str, Path]] = []

    # Claude Code: global ~/.claude.json and project-scoped .mcp.json
    paths.append(("Claude Code", home / ".claude.json"))
    project_mcp = Path.cwd() / ".mcp.json"
    if project_mcp.exists():
        paths.append(("Claude Code (project)", project_mcp))

    # Cursor — setup_commands.py always writes to ~/.cursor/mcp.json
    paths.append(("Cursor", tool_home / ".cursor" / "mcp.json"))

    # Windsurf
    paths.append(("Windsurf", tool_home / ".codeium" / "windsurf" / "mcp_config.json"))

    # Claude Desktop
    if system == "Darwin":
        paths.append(
            ("Claude Desktop", home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
        )
    elif system == "Windows" and appdata:
        paths.append(("Claude Desktop", Path(appdata) / "Claude" / "claude_desktop_config.json"))
    else:
        # Linux: try common candidates — check for the config file, not just the directory
        for candidate_dir in [
            home / ".config" / "Claude",
            home / ".local" / "share" / "Claude",
            home / "Claude",
        ]:
            config_file = candidate_dir / "claude_desktop_config.json"
            if config_file.exists():
                paths.append(("Claude Desktop", config_file))
                break
        else:
            paths.append(("Claude Desktop", home / ".config" / "Claude" / "claude_desktop_config.json"))

    return paths


def _has_skyvern_in_mcp_config(config_path: Path) -> bool:
    """Return True if the JSON config file has a mcpServers key containing 'skyvern'."""
    if not config_path.exists():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            return False
        return any("skyvern" in key.lower() for key in servers)
    except Exception:
        return False


def _check_mcp_config() -> CheckResult:
    tool_paths = _mcp_config_paths()
    configured: list[str] = []

    for tool_name, config_path in tool_paths:
        if _has_skyvern_in_mcp_config(config_path):
            configured.append(tool_name)

    if configured:
        detail = "configured in: " + ", ".join(configured)
        return CheckResult(name="MCP Config", status="ok", detail=detail)

    # Check if any tools are even installed (binary on PATH or config dir exists)
    installed_tools: list[str] = []
    if shutil.which("claude"):
        installed_tools.append("Claude Code")
    if (Path.home() / ".cursor").is_dir():
        installed_tools.append("Cursor")
    if (Path.home() / ".codeium").is_dir():
        installed_tools.append("Windsurf")
    for _, config_path in tool_paths:
        if "claude_desktop_config" in config_path.name and config_path.exists():
            installed_tools.append("Claude Desktop")
            break

    if installed_tools:
        detail = f"tools detected ({', '.join(installed_tools)}) but skyvern not in mcpServers"
    else:
        detail = "no AI tools detected or configured"

    return CheckResult(
        name="MCP Config",
        status="warn",
        detail=detail,
        hint="Run `skyvern setup` to configure MCP for your AI tools",
    )


def _check_importable(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _check_dependency_groups() -> CheckResult:
    # CLI+MCP group
    mcp_ok = _check_importable("fastmcp")
    playwright_ok = _check_importable("playwright")
    cli_mcp_status = "ok" if (mcp_ok and playwright_ok) else "warn"
    cli_mcp_missing = []
    if not mcp_ok:
        cli_mcp_missing.append("fastmcp")
    if not playwright_ok:
        cli_mcp_missing.append("playwright")

    # Local/self-host group
    local_modules = ["sqlalchemy", "alembic", "litellm", "anthropic", "openai"]
    local_missing = [m for m in local_modules if not _check_importable(m)]
    local_status = "ok" if not local_missing else "warn"

    lines: list[str] = []
    if cli_mcp_status == "ok":
        lines.append("CLI+MCP: ok (fastmcp, playwright)")
    else:
        lines.append(f"CLI+MCP: missing {', '.join(cli_mcp_missing)}")

    if local_missing:
        lines.append(f"Local/self-host: missing {', '.join(local_missing)}")
    else:
        lines.append("Local/self-host: ok (sqlalchemy, alembic, litellm, anthropic, openai)")

    # Core SDK is always ok if this code is running
    detail = "Core SDK: ok | " + " | ".join(lines)

    overall: Literal["ok", "warn", "error"] = "ok" if cli_mcp_status == "ok" and local_status == "ok" else "warn"
    hint = ""
    if cli_mcp_missing or local_missing:
        missing_all = cli_mcp_missing + local_missing
        hint = f"Run: pip install {' '.join(missing_all)} (or uv pip install)"

    return CheckResult(name="Dependency Groups", status=overall, detail=detail, hint=hint)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS = [
    _check_python_version,
    _check_config,
    _check_playwright_browser,
    _check_port_8000,
    _check_api_connectivity,
    _check_mcp_config,
    _check_dependency_groups,
]

_STATUS_STYLE = {
    "ok": "[green]ok[/green]",
    "warn": "[yellow]warn[/yellow]",
    "error": "[red]error[/red]",
}


@doctor_app.callback(invoke_without_command=True)
def doctor(ctx: typer.Context) -> None:
    """Run diagnostic checks on the Skyvern installation."""
    if ctx.invoked_subcommand is not None:
        return

    results: list[CheckResult] = []
    for check_fn in _CHECKS:
        try:
            result = check_fn()
        except Exception:
            result = CheckResult(
                name=getattr(check_fn, "__name__", "unknown").removeprefix("_check_"),
                status="error",
                detail=traceback.format_exc(limit=3),
                hint="Unexpected error — please report this",
            )
        results.append(result)

    table = Table(show_header=True, header_style="bold", title="Skyvern Doctor")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    table.add_column("Fix")

    for r in results:
        table.add_row(r.name, _STATUS_STYLE.get(r.status, r.status), r.detail, r.hint)

    console.print(table)

    n_ok = sum(1 for r in results if r.status == "ok")
    n_warn = sum(1 for r in results if r.status == "warn")
    n_err = sum(1 for r in results if r.status == "error")

    if n_err > 0:
        parts = []
        if n_ok:
            parts.append(f"[green]{n_ok} passed[/green]")
        if n_warn:
            parts.append(f"[yellow]{n_warn} warning{'s' if n_warn > 1 else ''}[/yellow]")
        parts.append(f"[red]{n_err} error{'s' if n_err > 1 else ''}[/red]")
        summary = ", ".join(parts)
        console.print(Panel(summary, border_style="red"))
        raise typer.Exit(code=1)
    elif n_warn > 0:
        parts = []
        if n_ok:
            parts.append(f"[green]{n_ok} passed[/green]")
        parts.append(f"[yellow]{n_warn} warning{'s' if n_warn > 1 else ''}[/yellow]")
        summary = ", ".join(parts)
        console.print(Panel(summary, border_style="yellow"))
    else:
        console.print(Panel(f"[green]{n_ok} passed[/green]", border_style="green"))
