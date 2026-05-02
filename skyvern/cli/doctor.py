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


def _check_database() -> CheckResult:
    from dotenv import load_dotenv

    from skyvern.utils.env_paths import resolve_backend_env_path

    load_dotenv(resolve_backend_env_path(), override=False)
    db_string = os.environ.get("DATABASE_STRING", "")
    if not db_string:
        return CheckResult(
            name="Database",
            status="warn",
            detail="DATABASE_STRING not set",
            hint="Run `skyvern init` or set DATABASE_STRING in .env",
        )

    if db_string.startswith("sqlite"):
        db_path = db_string.split("///")[-1] if "///" in db_string else ""
        if db_path and Path(db_path).exists():
            return CheckResult(name="Database", status="ok", detail=f"SQLite: {db_path}")
        if db_path:
            return CheckResult(
                name="Database",
                status="error",
                detail=f"SQLite file not found: {db_path}",
                hint="Run `alembic upgrade head` to initialize the database",
            )
        return CheckResult(name="Database", status="ok", detail="SQLite (in-memory or relative)")

    # PostgreSQL — try connecting
    try:
        result = subprocess.run(
            ["python", "-c", f"import sqlalchemy; e = sqlalchemy.create_engine('{db_string}'); e.connect().close()"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return CheckResult(name="Database", status="ok", detail=f"Connected: {_redact_password(db_string)}")
        stderr = result.stderr.strip()
        if "does not exist" in stderr:
            return CheckResult(
                name="Database",
                status="error",
                detail=f"Database does not exist: {_redact_password(db_string)}",
                hint="Run `skyvern doctor --fix` to create it, or: createdb <dbname>",
            )
        if "Connection refused" in stderr or "could not connect" in stderr:
            return CheckResult(
                name="Database",
                status="error",
                detail=f"Cannot connect: {_redact_password(db_string)}",
                hint="Check that PostgreSQL is running (docker ps, pg_isready)",
            )
        return CheckResult(
            name="Database",
            status="error",
            detail=f"Connection failed: {_redact_password(db_string)}",
            hint=stderr[:200] if stderr else "Check DATABASE_STRING in .env",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Database",
            status="error",
            detail=f"Connection timed out: {_redact_password(db_string)}",
            hint="Check that PostgreSQL is reachable",
        )
    except FileNotFoundError:
        return CheckResult(
            name="Database",
            status="warn",
            detail="Cannot verify (python not on PATH)",
        )


def _check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            name="Docker",
            status="warn",
            detail="not installed",
            hint="Install from https://docs.docker.com/get-docker/",
        )

    result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if result.returncode != 0:
        return CheckResult(
            name="Docker",
            status="warn",
            detail="installed but not running",
            hint="Start Docker Desktop",
        )

    # Check for postgres container
    ps_result = subprocess.run(
        ["docker", "ps", "--filter", "name=postgresql-container", "--format", "{{.Status}}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    standalone_pg = ps_result.stdout.strip()

    compose_result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    compose_running = compose_result.returncode == 0 and compose_result.stdout.strip()

    parts = ["running"]
    if standalone_pg:
        parts.append(f"postgresql-container: {standalone_pg}")
    if compose_running:
        parts.append("compose services active")
    if not standalone_pg and not compose_running:
        parts.append("no Skyvern containers detected")

    return CheckResult(name="Docker", status="ok", detail=", ".join(parts))


def _check_llm_config() -> CheckResult:
    from dotenv import load_dotenv

    from skyvern.utils.env_paths import resolve_backend_env_path

    load_dotenv(resolve_backend_env_path(), override=False)

    llm_key = os.environ.get("LLM_KEY", "")

    providers: dict[str, dict[str, str | None]] = {
        "OPENAI": {"enable": "ENABLE_OPENAI", "key": "OPENAI_API_KEY"},
        "ANTHROPIC": {"enable": "ENABLE_ANTHROPIC", "key": "ANTHROPIC_API_KEY"},
        "GEMINI": {"enable": "ENABLE_GEMINI", "key": "GEMINI_API_KEY"},
        "AZURE": {"enable": "ENABLE_AZURE", "key": "AZURE_API_KEY"},
        "BEDROCK": {"enable": "ENABLE_BEDROCK", "key": None},
        "OLLAMA": {"enable": "ENABLE_OLLAMA", "key": None},
        "OPENROUTER": {"enable": "ENABLE_OPENROUTER", "key": "OPENROUTER_API_KEY"},
        "GROQ": {"enable": "ENABLE_GROQ", "key": "GROQ_API_KEY"},
    }

    enabled = []
    missing_key = []
    for name, cfg in providers.items():
        enable_var = cfg["enable"]
        if enable_var and os.environ.get(enable_var, "").lower() in ("true", "1", "yes"):
            enabled.append(name)
            api_key_var = cfg.get("key")
            if api_key_var and not os.environ.get(api_key_var):
                missing_key.append(f"{name} ({api_key_var})")

    if not enabled:
        return CheckResult(
            name="LLM Provider",
            status="error",
            detail=f"LLM_KEY={llm_key} but no provider is enabled",
            hint="Run `skyvern init llm` or set ENABLE_<PROVIDER>=true + API key in .env",
        )

    if missing_key:
        return CheckResult(
            name="LLM Provider",
            status="error",
            detail=f"Enabled: {', '.join(enabled)} — missing API keys: {', '.join(missing_key)}",
            hint="Set the missing API keys in .env",
        )

    return CheckResult(
        name="LLM Provider",
        status="ok",
        detail=f"LLM_KEY={llm_key}, enabled: {', '.join(enabled)}",
    )


def _check_api_key_consistency() -> CheckResult:
    """Check that API keys are consistent across backend .env, frontend .env, and secrets.toml."""
    import re

    from dotenv import dotenv_values

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")
    secrets_toml = Path(".streamlit/secrets.toml")

    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    frontend_raw = dotenv_values(frontend_env).get("VITE_SKYVERN_API_KEY", "") if frontend_env.exists() else ""
    frontend_key = "" if frontend_raw in ("", "YOUR_API_KEY") else frontend_raw

    secrets_key = ""
    if secrets_toml.exists():
        m = re.search(r'cred\s*=\s*"([^"]*)"', secrets_toml.read_text())
        if m:
            secrets_key = m.group(1)

    canonical = secrets_key or backend_key
    if not canonical:
        return CheckResult(
            name="API Key Consistency",
            status="warn",
            detail="No API key found in backend .env or .streamlit/secrets.toml",
            hint="Run `skyvern init` or `skyvern quickstart` to generate an API key",
        )

    mismatches: list[str] = []
    if backend_key and backend_key != canonical:
        mismatches.append("backend .env differs from secrets.toml")
    if not frontend_env.exists():
        mismatches.append("skyvern-frontend/.env missing")
    elif not frontend_key:
        mismatches.append("VITE_SKYVERN_API_KEY not set in frontend .env")
    elif frontend_key != canonical:
        mismatches.append("frontend .env differs from backend")

    if mismatches:
        return CheckResult(
            name="API Key Consistency",
            status="error",
            detail="; ".join(mismatches),
            hint="Run `skyvern doctor --fix` to sync API keys",
        )

    return CheckResult(name="API Key Consistency", status="ok", detail="Keys consistent across all config files")


def _redact_password(db_string: str) -> str:
    """Replace password in a database URL with ***."""
    import re

    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", db_string)


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
    _check_database,
    _check_docker,
    _check_llm_config,
    _check_api_key_consistency,
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


def _try_fix(result: CheckResult) -> bool:
    """Attempt to auto-fix a failing check. Returns True if fixed."""
    if result.status == "ok":
        return False

    if result.name == "Database" and "does not exist" in result.detail:
        return _fix_create_database()
    if result.name == "Database" and "Cannot connect" in result.detail:
        return _fix_start_postgres()
    if result.name == "Playwright Browser" and result.status == "error":
        return _fix_install_playwright()
    if result.name == "API Key Consistency" and result.status == "error":
        return _fix_api_key_consistency()
    if result.name == "Docker" and "not running" in result.detail:
        console.print("  [yellow]→ Please start Docker Desktop manually[/yellow]")
        return False

    return False


def _fix_create_database() -> bool:
    from dotenv import load_dotenv

    from skyvern.utils.env_paths import resolve_backend_env_path

    load_dotenv(resolve_backend_env_path(), override=False)
    db_string = os.environ.get("DATABASE_STRING", "")
    if not db_string:
        return False

    import re

    m = re.match(r".*://(?P<user>[^:]+):(?P<pass>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)/(?P<db>[^?]+)", db_string)
    if not m:
        return False

    console.print(f"  [cyan]Creating database '{m.group('db')}'...[/cyan]")
    env = {
        **os.environ,
        "PGHOST": m.group("host"),
        "PGPORT": m.group("port"),
        "PGUSER": m.group("user"),
        "PGPASSWORD": m.group("pass"),
    }
    result = subprocess.run(["createdb", m.group("db")], capture_output=True, text=True, env=env)
    if result.returncode == 0 or "already exists" in (result.stderr or ""):
        console.print(f"  [green]✅ Database '{m.group('db')}' created[/green]")
        return True
    # Try via docker if local createdb isn't available
    docker_result = subprocess.run(
        ["docker", "exec", "postgresql-container", "createdb", "-U", m.group("user"), m.group("db")],
        capture_output=True,
        text=True,
    )
    if docker_result.returncode == 0 or "already exists" in (docker_result.stderr or ""):
        console.print(f"  [green]✅ Database '{m.group('db')}' created via Docker[/green]")
        return True
    console.print(f"  [red]Failed to create database: {result.stderr or docker_result.stderr}[/red]")
    return False


def _fix_start_postgres() -> bool:
    # Try starting existing docker container
    result = subprocess.run(
        ["docker", "start", "postgresql-container"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        console.print("  [green]✅ Started postgresql-container[/green]")
        return True
    # Try docker compose
    result = subprocess.run(["docker", "compose", "up", "-d", "postgres"], capture_output=True, text=True)
    if result.returncode == 0:
        console.print("  [green]✅ Started postgres via Docker Compose[/green]")
        return True
    console.print("  [yellow]→ Could not start PostgreSQL automatically. Start it manually.[/yellow]")
    return False


def _fix_api_key_consistency() -> bool:
    import re

    from dotenv import dotenv_values, set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    secrets_toml = Path(".streamlit/secrets.toml")

    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    secrets_key = ""
    if secrets_toml.exists():
        m = re.search(r'cred\s*=\s*"([^"]*)"', secrets_toml.read_text())
        if m:
            secrets_key = m.group(1)

    canonical = secrets_key or backend_key
    if not canonical:
        console.print("  [yellow]→ No source API key found to sync from[/yellow]")
        return False

    if not frontend_env.exists() and frontend_example.exists():
        import shutil

        shutil.copy(frontend_example, frontend_env)
        console.print("  [cyan]Created skyvern-frontend/.env from .env.example[/cyan]")

    if not frontend_env.exists():
        console.print("  [yellow]→ skyvern-frontend/.env not found and no .env.example to copy[/yellow]")
        return False

    set_key(str(frontend_env), "VITE_SKYVERN_API_KEY", canonical)
    source = "secrets.toml" if secrets_key else "backend .env"
    console.print(f"  [green]✅ Synced VITE_SKYVERN_API_KEY in skyvern-frontend/.env (from {source})[/green]")
    return True


def _fix_install_playwright() -> bool:
    console.print("  [cyan]Installing Chromium via Playwright...[/cyan]")
    result = subprocess.run(["playwright", "install", "chromium"], capture_output=True, text=True)
    if result.returncode == 0:
        console.print("  [green]✅ Chromium installed[/green]")
        return True
    console.print(f"  [red]Failed: {result.stderr[:200]}[/red]")
    return False


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    fix: bool = typer.Option(False, "--fix", help="Attempt to auto-fix issues found"),
) -> None:
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

    if fix and any(r.status in ("error", "warn") for r in results):
        console.print("\n[bold blue]Attempting auto-fixes...[/bold blue]")
        fixed = 0
        for r in results:
            if r.status in ("error", "warn"):
                if _try_fix(r):
                    fixed += 1
        if fixed:
            console.print(
                f"\n[green]Fixed {fixed} issue{'s' if fixed > 1 else ''}. Re-run `skyvern doctor` to verify.[/green]"
            )
        else:
            console.print("\n[yellow]No issues could be auto-fixed. See hints above.[/yellow]")
        return

    if n_err > 0:
        parts = []
        if n_ok:
            parts.append(f"[green]{n_ok} passed[/green]")
        if n_warn:
            parts.append(f"[yellow]{n_warn} warning{'s' if n_warn > 1 else ''}[/yellow]")
        parts.append(f"[red]{n_err} error{'s' if n_err > 1 else ''}[/red]")
        summary = ", ".join(parts)
        console.print(
            Panel(summary + " — run [bold]skyvern doctor --fix[/bold] to attempt repairs", border_style="red")
        )
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
