"""Skyvern dependency diagnostics."""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import os
import platform
import re
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

GENERATED_CREDENTIALS_FILE = Path(".skyvern/credentials.toml")
LEGACY_STREAMLIT_CREDENTIALS_FILE = Path(".streamlit/secrets.toml")


@dataclass
class CheckResult:
    name: str
    status: Literal["ok", "warn", "error"]
    detail: str
    hint: str = field(default="")


FRONTEND_RUNTIME_URL_VARS: dict[str, tuple[str, tuple[str, ...]]] = {
    "VITE_API_BASE_URL": ("http://localhost:8000/api/v1", ("http://", "https://")),
    "VITE_WSS_BASE_URL": ("ws://localhost:8000/api/v1", ("ws://", "wss://")),
    "VITE_ARTIFACT_API_BASE_URL": ("http://localhost:9090", ("http://", "https://")),
}
LOCAL_STREAMING_MODE = "cdp"
FRONTEND_STREAMING_MODE_VAR = "VITE_BROWSER_STREAMING_MODE"
BACKEND_STREAMING_MODE_VAR = "BROWSER_STREAMING_MODE"
ALLOWED_STREAMING_MODES = {"cdp", "vnc"}

FRONTEND_BUNDLE_PLACEHOLDERS = {
    "__VITE_API_BASE_URL_PLACEHOLDER__",
    "__VITE_WSS_BASE_URL_PLACEHOLDER__",
    "__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__",
    "__SKYVERN_API_KEY_PLACEHOLDER__",
    "__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__",
}


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

        compose_check = _check_compose_database_connection()
        if compose_check is not None:
            return compose_check

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


def _check_compose_database_connection() -> CheckResult | None:
    """Return an ok result if the running Compose backend can reach its database."""
    if not _docker_compose_available():
        return None

    script = r"""
import json
import sqlalchemy

from skyvern.config import settings

engine = sqlalchemy.create_engine(settings.DATABASE_STRING)
with engine.connect():
    pass

print(json.dumps({"status": "ok"}))
"""
    try:
        result = _run_docker_compose_exec("skyvern", script, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None

    if payload.get("status") == "ok":
        return CheckResult(name="Database", status="ok", detail="Docker Compose backend can connect to database")

    return None


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


def _read_credential_file(path: Path) -> str:
    if not path.exists():
        return ""
    m = re.search(r'(?<![A-Za-z0-9_])cred\s*=\s*"([^"]*)"', path.read_text())
    return m.group(1) if m else ""


def _read_generated_credential() -> str:
    return _read_credential_file(GENERATED_CREDENTIALS_FILE)


def _read_legacy_streamlit_credential() -> str:
    return _read_credential_file(LEGACY_STREAMLIT_CREDENTIALS_FILE)


def _check_api_key_consistency() -> CheckResult:
    """Check that local API keys are consistent.

    Generated Docker credentials are only a fallback for compose startup; backend .env
    remains the preferred source when present.
    """
    from dotenv import dotenv_values

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")

    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    frontend_raw = dotenv_values(frontend_env).get("VITE_SKYVERN_API_KEY", "") if frontend_env.exists() else ""
    frontend_key = "" if frontend_raw in ("", "YOUR_API_KEY") else frontend_raw
    generated_key = _read_generated_credential()
    legacy_key = _read_legacy_streamlit_credential()

    # Docker Compose can inject the generated credentials file into the UI
    # without requiring VITE_SKYVERN_API_KEY in skyvern-frontend/.env.
    # Legacy installs may only have the API key in the old compatibility file;
    # treat it as a migration source.
    canonical = backend_key or generated_key or legacy_key
    if not canonical:
        return CheckResult(
            name="API Key Consistency",
            status="warn",
            detail="No API key found in backend .env or generated credentials",
            hint="Run `skyvern init` or `skyvern quickstart` to generate an API key",
        )

    mismatches: list[str] = []
    if not backend_key and not generated_key:
        mismatches.append("SKYVERN_API_KEY not set in backend .env")
    if not frontend_env.exists():
        mismatches.append("skyvern-frontend/.env missing")
    elif not frontend_key and not generated_key:
        mismatches.append("VITE_SKYVERN_API_KEY not set in frontend .env")
    elif frontend_key and frontend_key != canonical:
        mismatches.append("frontend .env differs from backend")

    if mismatches:
        return CheckResult(
            name="API Key Consistency",
            status="error",
            detail="; ".join(mismatches),
            hint="Run `skyvern doctor --fix` to sync API keys",
        )

    return CheckResult(name="API Key Consistency", status="ok", detail="Backend and frontend API keys are consistent")


def _check_legacy_streamlit_secrets() -> CheckResult:
    if not LEGACY_STREAMLIT_CREDENTIALS_FILE.exists():
        return CheckResult(name="Legacy Streamlit Secrets", status="ok", detail="not present")

    from dotenv import dotenv_values

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    legacy_key = _read_legacy_streamlit_credential()

    if not legacy_key:
        return CheckResult(
            name="Legacy Streamlit Secrets",
            status="warn",
            detail=".streamlit/secrets.toml exists but no cred value was found",
            hint="Remove the file, or run `skyvern doctor --fix` to remove the deprecated file",
        )

    if not backend_key:
        return CheckResult(
            name="Legacy Streamlit Secrets",
            status="warn",
            detail=".streamlit/secrets.toml has a legacy API key but backend .env is missing SKYVERN_API_KEY",
            hint="Run `skyvern doctor --fix` to migrate the key into .env and skyvern-frontend/.env",
        )

    if legacy_key != backend_key:
        return CheckResult(
            name="Legacy Streamlit Secrets",
            status="warn",
            detail=".streamlit/secrets.toml is deprecated and differs from backend .env",
            hint="Run `skyvern doctor --fix` to remove the deprecated file",
        )

    return CheckResult(
        name="Legacy Streamlit Secrets",
        status="warn",
        detail=".streamlit/secrets.toml matches backend .env; deprecated compatibility file only",
        hint="Run `skyvern doctor --fix` to remove the deprecated file",
    )


def _fingerprint(value: str) -> str:
    if not value:
        return "missing"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _run_docker_compose_exec(service: str, script: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _docker_compose_available() -> bool:
    return shutil.which("docker") is not None and Path("docker-compose.yml").exists()


def _is_placeholder_env_value(value: str) -> bool:
    normalized = value.strip()
    return (
        normalized in ("", "PLACEHOLDER", "YOUR_API_KEY")
        or normalized in FRONTEND_BUNDLE_PLACEHOLDERS
        or (normalized.startswith("__") and normalized.endswith("__") and "PLACEHOLDER" in normalized)
    )


def _validate_frontend_runtime_url_values(values: dict[str, str], source: str) -> list[str]:
    problems: list[str] = []
    for name, (_default, allowed_prefixes) in FRONTEND_RUNTIME_URL_VARS.items():
        value = values.get(name, "").strip()
        if not value:
            problems.append(f"{source} {name} is missing")
        elif _is_placeholder_env_value(value):
            problems.append(f"{source} {name} is still {value}")
        elif not value.startswith(allowed_prefixes):
            allowed = " or ".join(allowed_prefixes)
            problems.append(f"{source} {name} must start with {allowed}; got {value}")
    return problems


def _normalize_streaming_mode(value: str | None) -> str:
    return (value or "").strip().lower()


def _validate_streaming_mode_value(value: str | None, source: str) -> list[str]:
    mode = _normalize_streaming_mode(value)
    if not mode:
        return [f"{source} streaming mode is missing"]
    if _is_placeholder_env_value(value or ""):
        return [f"{source} streaming mode is still {value}"]
    if mode not in ALLOWED_STREAMING_MODES:
        allowed = ", ".join(sorted(ALLOWED_STREAMING_MODES))
        return [f"{source} streaming mode must be one of {allowed}; got {value}"]
    return []


def _check_local_streaming_mode() -> CheckResult:
    """Check that local self-hosted installs default to CDP livestreaming."""
    from dotenv import dotenv_values

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")

    backend_raw = dotenv_values(backend_env).get(BACKEND_STREAMING_MODE_VAR, "") if backend_env.exists() else ""
    frontend_raw = dotenv_values(frontend_env).get(FRONTEND_STREAMING_MODE_VAR, "") if frontend_env.exists() else ""

    problems: list[str] = []
    if backend_env.exists():
        problems.extend(_validate_streaming_mode_value(str(backend_raw or ""), "backend .env"))
        if _normalize_streaming_mode(str(backend_raw or "")) != LOCAL_STREAMING_MODE:
            problems.append(f"backend .env {BACKEND_STREAMING_MODE_VAR} should be {LOCAL_STREAMING_MODE}")
    else:
        problems.append("backend .env is missing")

    if frontend_env.exists():
        problems.extend(_validate_streaming_mode_value(str(frontend_raw or ""), "skyvern-frontend/.env"))
        if _normalize_streaming_mode(str(frontend_raw or "")) != LOCAL_STREAMING_MODE:
            problems.append(
                f"skyvern-frontend/.env {FRONTEND_STREAMING_MODE_VAR} should be {LOCAL_STREAMING_MODE}"
            )
    else:
        problems.append("skyvern-frontend/.env is missing")

    if problems:
        return CheckResult(
            name="Local Streaming Mode",
            status="warn",
            detail="; ".join(problems),
            hint="Run `skyvern doctor --fix` to enable CDP livestreaming for backend and UI",
        )

    if not _docker_compose_available():
        return CheckResult(
            name="Local Streaming Mode",
            status="ok",
            detail="backend and frontend env files enable CDP livestreaming",
        )

    container_problems: list[str] = []
    checked_containers = 0
    backend_result = subprocess.run(
        ["docker", "compose", "exec", "-T", "skyvern", "printenv", BACKEND_STREAMING_MODE_VAR],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if backend_result.returncode == 0:
        checked_containers += 1
        backend_container_mode = _normalize_streaming_mode(backend_result.stdout)
        if backend_container_mode != LOCAL_STREAMING_MODE:
            container_problems.append(
                f"running skyvern {BACKEND_STREAMING_MODE_VAR} is {backend_container_mode or 'missing'}"
            )

    ui_result = subprocess.run(
        ["docker", "compose", "exec", "-T", "skyvern-ui", "printenv", FRONTEND_STREAMING_MODE_VAR],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ui_result.returncode == 0:
        checked_containers += 1
        ui_container_mode = _normalize_streaming_mode(ui_result.stdout)
        if ui_container_mode != LOCAL_STREAMING_MODE:
            container_problems.append(
                f"running skyvern-ui {FRONTEND_STREAMING_MODE_VAR} is {ui_container_mode or 'missing'}"
            )

    if container_problems:
        return CheckResult(
            name="Local Streaming Mode",
            status="warn",
            detail="; ".join(container_problems),
            hint="Run `docker compose up -d --force-recreate skyvern skyvern-ui`",
        )

    if checked_containers == 0:
        return CheckResult(
            name="Local Streaming Mode",
            status="ok",
            detail="backend and frontend env files enable CDP livestreaming; running containers not checked",
        )

    return CheckResult(
        name="Local Streaming Mode",
        status="ok",
        detail="CDP livestreaming is enabled for backend, UI, and running containers",
    )


def _check_frontend_runtime_env() -> CheckResult:
    """Check Vite runtime config before the UI can white-screen on placeholders."""
    frontend_dir = Path("skyvern-frontend")
    compose_file = Path("docker-compose.yml")
    if not frontend_dir.exists() and not compose_file.exists():
        return CheckResult(name="Frontend Runtime Env", status="ok", detail="not checked (no frontend or compose)")

    from dotenv import dotenv_values

    frontend_env = frontend_dir / ".env"
    frontend_example = frontend_dir / ".env.example"
    if not frontend_env.exists():
        hint = "Run `skyvern doctor --fix` to create skyvern-frontend/.env"
        if frontend_example.exists():
            hint += ", or copy skyvern-frontend/.env.example to skyvern-frontend/.env"
        return CheckResult(
            name="Frontend Runtime Env",
            status="error",
            detail="skyvern-frontend/.env is missing; skyvern-ui will fall back to Dockerfile placeholders",
            hint=hint,
        )

    host_values_raw = dotenv_values(frontend_env)
    host_values = {name: str(host_values_raw.get(name) or "") for name in FRONTEND_RUNTIME_URL_VARS}
    host_problems = _validate_frontend_runtime_url_values(host_values, "skyvern-frontend/.env")
    host_problems.extend(
        _validate_streaming_mode_value(
            str(host_values_raw.get(FRONTEND_STREAMING_MODE_VAR) or ""),
            "skyvern-frontend/.env",
        )
    )
    if host_problems:
        return CheckResult(
            name="Frontend Runtime Env",
            status="error",
            detail="; ".join(host_problems),
            hint="Run `skyvern doctor --fix`, then `docker compose up -d --force-recreate skyvern-ui`",
        )

    if not _docker_compose_available():
        return CheckResult(
            name="Frontend Runtime Env",
            status="ok",
            detail="skyvern-frontend/.env has valid Vite runtime URLs",
        )

    script = r"""
const fs = require("fs");
const path = require("path");

const names = [
  "VITE_API_BASE_URL",
  "VITE_WSS_BASE_URL",
  "VITE_ARTIFACT_API_BASE_URL",
  "VITE_BROWSER_STREAMING_MODE",
];
const placeholders = [
  "__VITE_API_BASE_URL_PLACEHOLDER__",
  "__VITE_WSS_BASE_URL_PLACEHOLDER__",
  "__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__",
  "__SKYVERN_API_KEY_PLACEHOLDER__",
  "__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__",
];

const values = {};
for (const name of names) {
  values[name] = process.env[name] || "";
}

const bundlePlaceholders = new Set();
let bundleError = "";
try {
  const assetsDir = "/app/dist/assets";
  for (const file of fs.readdirSync(assetsDir)) {
    if (!file.endsWith(".js")) {
      continue;
    }
    const contents = fs.readFileSync(path.join(assetsDir, file), "utf8");
    for (const placeholder of placeholders) {
      if (contents.includes(placeholder)) {
        bundlePlaceholders.add(placeholder);
      }
    }
  }
} catch (error) {
  bundleError = String(error);
}

console.log(JSON.stringify({
  values,
  bundle_placeholders: Array.from(bundlePlaceholders),
  bundle_error: bundleError,
}));
"""
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "skyvern-ui", "node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return CheckResult(
            name="Frontend Runtime Env",
            status="warn",
            detail=f"host frontend env is valid; running skyvern-ui not checked: {exc}",
            hint="Start skyvern-ui and rerun `skyvern doctor`",
        )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "skyvern-ui compose service is not running"
        return CheckResult(
            name="Frontend Runtime Env",
            status="warn",
            detail=f"host frontend env is valid; running skyvern-ui not checked: {detail[:240]}",
            hint="Run `docker compose up -d skyvern-ui` and rerun `skyvern doctor`",
        )

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return CheckResult(
            name="Frontend Runtime Env",
            status="warn",
            detail=f"could not parse skyvern-ui env diagnostic output: {result.stdout.strip()[:200]}",
        )

    container_values = {
        name: str((payload.get("values") or {}).get(name) or "") for name in FRONTEND_RUNTIME_URL_VARS
    }
    container_problems = _validate_frontend_runtime_url_values(container_values, "running skyvern-ui")
    container_problems.extend(
        _validate_streaming_mode_value(
            str((payload.get("values") or {}).get(FRONTEND_STREAMING_MODE_VAR) or ""),
            "running skyvern-ui",
        )
    )
    if container_problems:
        return CheckResult(
            name="Frontend Runtime Env",
            status="error",
            detail="; ".join(container_problems),
            hint=(
                "Recreate the UI after fixing skyvern-frontend/.env: "
                "`docker compose up -d --force-recreate skyvern-ui`"
            ),
        )

    bundle_placeholders = [str(value) for value in payload.get("bundle_placeholders") or []]
    if bundle_placeholders:
        return CheckResult(
            name="Frontend Runtime Env",
            status="error",
            detail="running UI bundle still contains placeholders: " + ", ".join(bundle_placeholders),
            hint="Run `docker compose up -d --force-recreate skyvern-ui` after fixing skyvern-frontend/.env",
        )

    bundle_error = str(payload.get("bundle_error") or "")
    if bundle_error:
        return CheckResult(
            name="Frontend Runtime Env",
            status="warn",
            detail=f"could not inspect running UI bundle: {bundle_error[:240]}",
        )

    return CheckResult(
        name="Frontend Runtime Env",
        status="ok",
        detail="skyvern-frontend/.env, running skyvern-ui env, and UI bundle placeholders look valid",
    )


def _check_docker_local_auth() -> CheckResult:
    """Validate that the running Docker backend accepts its configured local API key."""
    if not _docker_compose_available():
        return CheckResult(name="Docker Local Auth", status="ok", detail="not checked (no docker-compose.yml)")

    script = r"""
import json
import os
import urllib.error
import urllib.request

token = os.environ.get("SKYVERN_API_KEY", "").strip()
if not token or token == "PLACEHOLDER":
    print(json.dumps({"status": "missing_container_api_key"}))
    raise SystemExit(0)

request = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/internal/auth/status",
    headers={"x-api-key": token},
)

try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

    if isinstance(payload, dict) and payload.get("status"):
        print(json.dumps(payload))
    else:
        print(json.dumps({
            "status": "http_error",
            "status_code": exc.code,
            "detail": payload.get("detail") if isinstance(payload, dict) else body or str(exc),
        }))
"""

    try:
        result = _run_docker_compose_exec("skyvern", script, timeout=60)
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            name="Docker Local Auth",
            status="warn",
            detail=f"not checked: Docker auth diagnostic timed out after {exc.timeout}s",
            hint="Start Docker Compose and rerun `skyvern doctor`",
        )
    except FileNotFoundError as exc:
        return CheckResult(
            name="Docker Local Auth",
            status="warn",
            detail=f"not checked: {exc}",
            hint="Start Docker Compose and rerun `skyvern doctor`",
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = stderr or result.stdout.strip() or "skyvern compose service is not running"
        return CheckResult(
            name="Docker Local Auth",
            status="warn",
            detail=detail[:300],
            hint="Run `docker compose up -d` to enable Docker auth diagnostics",
        )

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return CheckResult(
            name="Docker Local Auth",
            status="warn",
            detail=f"could not parse diagnostic output: {result.stdout.strip()[:200]}",
        )

    status_value = payload.get("status") or ("http_error" if payload.get("detail") else None)
    org_id = payload.get("organization_id", "")
    if status_value == "ok":
        return CheckResult(
            name="Docker Local Auth", status="ok", detail=f"running backend accepts API key for {org_id}"
        )

    detail_text = str(payload.get("detail") or "")
    details = {
        "missing_container_api_key": "running backend has no SKYVERN_API_KEY",
        "missing_api_key": "running backend has no SKYVERN_API_KEY",
        "invalid_format": "running backend cannot decode SKYVERN_API_KEY",
        "invalid": "running backend cannot validate SKYVERN_API_KEY",
        "expired": f"running backend API key is expired for {org_id}",
        "not_found": "API key decodes, but its organization is missing from Docker DB",
        "organization_not_found": f"API key decodes, but organization {org_id} is missing from Docker DB",
        "token_not_in_database": f"organization {org_id} exists, but this API key is not in Docker DB",
        "db_token_invalid": f"organization {org_id} exists, but this API key is marked invalid",
        "http_error": "running backend auth diagnostics endpoint returned an HTTP error",
    }
    detail = details.get(str(status_value), f"unknown Docker auth status: {status_value}")
    if detail_text:
        detail = f"{detail}: {detail_text}"
    return CheckResult(
        name="Docker Local Auth",
        status="error",
        detail=detail,
        hint="Run `skyvern doctor --fix` to create a Docker-local org/key and sync env files",
    )


def _check_frontend_build_api_key() -> CheckResult:
    """Check whether the running production UI bundle was built with the current Vite API key."""
    if not _docker_compose_available():
        return CheckResult(name="Frontend Build API Key", status="ok", detail="not checked (no docker-compose.yml)")

    script = r"""
const fs = require("fs");
const path = require("path");

function readGeneratedKey() {
  const credentialsFile = process.env.SKYVERN_CREDENTIALS_FILE || "/app/.skyvern/credentials.toml";
  try {
    const contents = fs.readFileSync(credentialsFile, "utf8");
    const match = contents.match(/(?:^|[^A-Za-z0-9_])cred\s*=\s*"([^"]*)"/);
    return match ? match[1].trim() : "";
  } catch (_error) {
    return "";
  }
}

const envKey = (process.env.VITE_SKYVERN_API_KEY || "").trim();
const generatedKey = readGeneratedKey();
const key = envKey && envKey !== "YOUR_API_KEY" ? envKey : generatedKey;
if (!key) {
  console.log(JSON.stringify({ status: "missing_env" }));
  process.exit(0);
}

const assetsDir = "/app/dist/assets";
let matches = 0;
try {
  for (const file of fs.readdirSync(assetsDir)) {
    if (!file.endsWith(".js")) {
      continue;
    }
    const contents = fs.readFileSync(path.join(assetsDir, file), "utf8");
    if (contents.includes(key)) {
      matches += 1;
    }
  }
} catch (error) {
  console.log(JSON.stringify({ status: "missing_bundle", detail: String(error) }));
  process.exit(0);
}

console.log(JSON.stringify({ status: matches > 0 ? "ok" : "stale_or_missing_bundle", matches }));
"""
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "skyvern-ui", "node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return CheckResult(
            name="Frontend Build API Key",
            status="warn",
            detail=f"not checked: {exc}",
            hint="Start skyvern-ui and rerun `skyvern doctor`",
        )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "skyvern-ui compose service is not running"
        return CheckResult(
            name="Frontend Build API Key",
            status="warn",
            detail=detail[:300],
            hint="Run `docker compose up -d skyvern-ui` to enable UI bundle diagnostics",
        )

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return CheckResult(
            name="Frontend Build API Key",
            status="warn",
            detail=f"could not parse diagnostic output: {result.stdout.strip()[:200]}",
        )

    if payload.get("status") == "ok":
        return CheckResult(name="Frontend Build API Key", status="ok", detail="running UI bundle contains Vite API key")
    if payload.get("status") == "missing_env":
        return CheckResult(
            name="Frontend Build API Key",
            status="error",
            detail="running skyvern-ui has no VITE_SKYVERN_API_KEY or generated credentials file",
            hint="Run `skyvern doctor --fix` to sync env files and recreate skyvern-ui",
        )
    return CheckResult(
        name="Frontend Build API Key",
        status="error",
        detail="running UI bundle does not contain the current VITE_SKYVERN_API_KEY",
        hint="Run `skyvern doctor --fix` or recreate skyvern-ui after editing skyvern-frontend/.env",
    )


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
    _check_legacy_streamlit_secrets,
    _check_local_streaming_mode,
    _check_frontend_runtime_env,
    _check_docker_local_auth,
    _check_frontend_build_api_key,
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
    if result.name == "Legacy Streamlit Secrets" and result.status == "warn":
        return _fix_legacy_streamlit_secrets()
    if result.name == "Local Streaming Mode" and result.status in {"warn", "error"}:
        return _fix_local_streaming_mode()
    if result.name == "Frontend Runtime Env" and result.status == "error":
        return _fix_frontend_runtime_env()
    if result.name == "Docker Local Auth" and result.status == "error":
        return _fix_docker_local_auth()
    if result.name == "Frontend Build API Key" and result.status == "error":
        return _recreate_docker_services(["skyvern-ui"])
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
    from dotenv import dotenv_values, set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")

    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    generated_key = _read_generated_credential()
    legacy_key = _read_legacy_streamlit_credential()

    canonical = backend_key or generated_key or legacy_key
    if not canonical:
        console.print("  [yellow]→ No source API key found to sync from[/yellow]")
        return False

    if not backend_env.exists():
        backend_env.touch()

    set_key(str(backend_env), "SKYVERN_API_KEY", canonical, quote_mode="never")

    if not frontend_env.exists() and frontend_example.exists():
        shutil.copy(frontend_example, frontend_env)
        console.print("  [cyan]Created skyvern-frontend/.env from .env.example[/cyan]")

    if not frontend_env.exists():
        console.print("  [yellow]→ skyvern-frontend/.env not found and no .env.example to copy[/yellow]")
        return False

    set_key(str(frontend_env), "VITE_SKYVERN_API_KEY", canonical, quote_mode="never")
    source = (
        "backend .env"
        if backend_key
        else ".skyvern/credentials.toml"
        if generated_key
        else "legacy .streamlit/secrets.toml"
    )
    console.print(f"  [green]✅ Synced local API key across backend and frontend (from {source})[/green]")
    return True


def _fix_legacy_streamlit_secrets() -> bool:
    """Best-effort migration for old .streamlit/secrets.toml installs."""
    from dotenv import dotenv_values, set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    legacy_key = _read_legacy_streamlit_credential()

    if not LEGACY_STREAMLIT_CREDENTIALS_FILE.exists():
        return False

    if not legacy_key:
        console.print(
            "  [yellow]→ Legacy .streamlit/secrets.toml has no cred value; leaving it for inspection[/yellow]"
        )
        return False

    if not backend_key and legacy_key:
        if not backend_env.exists():
            backend_env.touch()
        set_key(str(backend_env), "SKYVERN_API_KEY", legacy_key, quote_mode="never")
        if not frontend_env.exists() and frontend_example.exists():
            shutil.copy(frontend_example, frontend_env)
        if frontend_env.exists():
            set_key(str(frontend_env), "VITE_SKYVERN_API_KEY", legacy_key, quote_mode="never")
        console.print("  [green]✅ Migrated legacy .streamlit API key into .env files[/green]")

    LEGACY_STREAMLIT_CREDENTIALS_FILE.unlink()
    console.print("  [green]✅ Removed deprecated .streamlit/secrets.toml[/green]")
    return True


def _ensure_frontend_env_exists() -> Path:
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    if not frontend_env.exists():
        frontend_env.parent.mkdir(parents=True, exist_ok=True)
        if frontend_example.exists():
            shutil.copy(frontend_example, frontend_env)
            console.print("  [cyan]Created skyvern-frontend/.env from .env.example[/cyan]")
        else:
            frontend_env.touch()
            console.print("  [cyan]Created empty skyvern-frontend/.env[/cyan]")
    return frontend_env


def _fix_local_streaming_mode() -> bool:
    """Enable local CDP livestreaming in backend and frontend env files."""
    from dotenv import set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    backend_env = resolve_backend_env_path()
    if not backend_env.exists():
        backend_env.touch()
    set_key(str(backend_env), BACKEND_STREAMING_MODE_VAR, LOCAL_STREAMING_MODE, quote_mode="never")
    console.print(f"  [cyan]Set {BACKEND_STREAMING_MODE_VAR}={LOCAL_STREAMING_MODE} in backend .env[/cyan]")

    frontend_env = _ensure_frontend_env_exists()
    set_key(str(frontend_env), FRONTEND_STREAMING_MODE_VAR, LOCAL_STREAMING_MODE, quote_mode="never")
    console.print(
        f"  [cyan]Set {FRONTEND_STREAMING_MODE_VAR}={LOCAL_STREAMING_MODE} in skyvern-frontend/.env[/cyan]"
    )

    if _docker_compose_available():
        return _recreate_docker_services(["skyvern", "skyvern-ui"])

    return True


def _fix_frontend_runtime_env() -> bool:
    """Create/fill frontend Vite env values and recreate the UI container."""
    from dotenv import dotenv_values, set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    frontend_env = _ensure_frontend_env_exists()

    values = dotenv_values(frontend_env)
    changed = False
    for name, (default, allowed_prefixes) in FRONTEND_RUNTIME_URL_VARS.items():
        value = str(values.get(name) or "").strip()
        if not value or _is_placeholder_env_value(value) or not value.startswith(allowed_prefixes):
            set_key(str(frontend_env), name, default, quote_mode="never")
            console.print(f"  [cyan]Set {name}={default} in skyvern-frontend/.env[/cyan]")
            changed = True

    streaming_mode = str(values.get(FRONTEND_STREAMING_MODE_VAR) or "").strip()
    if _normalize_streaming_mode(streaming_mode) != LOCAL_STREAMING_MODE:
        set_key(str(frontend_env), FRONTEND_STREAMING_MODE_VAR, LOCAL_STREAMING_MODE, quote_mode="never")
        console.print(
            f"  [cyan]Set {FRONTEND_STREAMING_MODE_VAR}={LOCAL_STREAMING_MODE} in skyvern-frontend/.env[/cyan]"
        )
        changed = True

    backend_env = resolve_backend_env_path()
    backend_key = dotenv_values(backend_env).get("SKYVERN_API_KEY", "") if backend_env.exists() else ""
    generated_key = _read_generated_credential()
    legacy_key = _read_legacy_streamlit_credential()
    canonical_key = str(backend_key or generated_key or legacy_key or "").strip()
    frontend_api_key = str(values.get("VITE_SKYVERN_API_KEY") or "").strip()
    if canonical_key and _is_placeholder_env_value(frontend_api_key):
        set_key(str(frontend_env), "VITE_SKYVERN_API_KEY", canonical_key, quote_mode="never")
        console.print("  [cyan]Synced VITE_SKYVERN_API_KEY in skyvern-frontend/.env[/cyan]")
        changed = True
    elif not canonical_key:
        console.print("  [yellow]→ No backend API key found to sync into skyvern-frontend/.env[/yellow]")

    if _docker_compose_available():
        return _recreate_docker_services(["skyvern-ui"]) or changed

    return changed


def _fix_docker_local_auth() -> bool:
    """Create a fresh local org/token inside Docker Postgres and sync host env files."""
    from dotenv import set_key

    from skyvern.utils.env_paths import resolve_backend_env_path

    script = r"""
import asyncio
import json

from skyvern.forge import app
from skyvern.forge.sdk.services.org_auth_token_service import create_org_api_token


async def main():
    org = await app.DATABASE.organizations.create_organization("Skyvern Local Demo")
    token = await create_org_api_token(org.organization_id)
    print(json.dumps({"api_key": token.token, "organization_id": org.organization_id}))


asyncio.run(main())
"""
    console.print("  [cyan]Creating a fresh local org/API key in Docker Postgres...[/cyan]")
    try:
        result = _run_docker_compose_exec("skyvern", script, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        console.print(f"  [red]Failed to run Docker auth repair: {exc}[/red]")
        return False

    if result.returncode != 0:
        console.print(f"  [red]Failed to create Docker-local API key: {(result.stderr or result.stdout)[:500]}[/red]")
        return False

    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        console.print(f"  [red]Could not parse Docker auth repair output: {result.stdout[:500]}[/red]")
        return False

    api_key = str(payload.get("api_key") or "")
    organization_id = str(payload.get("organization_id") or "")
    if not api_key:
        console.print("  [red]Docker auth repair did not return an API key[/red]")
        return False

    backend_env = resolve_backend_env_path()
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")

    if not backend_env.exists():
        backend_env.touch()
    if not frontend_env.exists() and frontend_example.exists():
        shutil.copy(frontend_example, frontend_env)
        console.print("  [cyan]Created skyvern-frontend/.env from .env.example[/cyan]")
    if not frontend_env.exists():
        console.print("  [red]skyvern-frontend/.env not found and no .env.example to copy[/red]")
        return False

    set_key(str(backend_env), "SKYVERN_API_KEY", api_key, quote_mode="never")
    set_key(str(frontend_env), "VITE_SKYVERN_API_KEY", api_key, quote_mode="never")
    GENERATED_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_CREDENTIALS_FILE.write_text(f'[general]\ncred = "{api_key}"\n')
    if LEGACY_STREAMLIT_CREDENTIALS_FILE.exists():
        LEGACY_STREAMLIT_CREDENTIALS_FILE.unlink()

    console.print(
        "  [green]✅ Synced fresh Docker-local API key "
        f"for {organization_id} (fingerprint {_fingerprint(api_key)})[/green]"
    )
    return _recreate_docker_services(["skyvern", "skyvern-ui"])


def _recreate_docker_services(services: list[str]) -> bool:
    if not _docker_compose_available():
        return False

    console.print(f"  [cyan]Recreating Docker service(s): {', '.join(services)}...[/cyan]")
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--force-recreate", *services],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        console.print("  [green]✅ Docker services recreated[/green]")
        return True

    console.print(f"  [red]Failed to recreate Docker services: {(result.stderr or result.stdout)[:500]}[/red]")
    return False


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
