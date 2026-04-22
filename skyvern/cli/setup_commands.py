"""Setup commands to register Skyvern with AI coding tools."""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, cast
from urllib.parse import urlparse

import json5
import typer
import yaml
from dotenv import load_dotenv
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from skyvern.analytics import capture_setup_event
from skyvern.cli.auth_command import run_signup
from skyvern.cli.console import console
from skyvern.cli.skill_commands import get_skill_dirs
from skyvern.utils import detect_os, get_windows_appdata_roaming
from skyvern.utils.env_paths import resolve_backend_env_path

# NOTE: These helpers back both `skyvern setup ...` commands and the
# interactive MCP step used by `skyvern init` / `skyvern quickstart`, plus
# the MCP switcher in `skyvern/cli/mcp_commands.py`.
# Keep shared config parsing/writes, local stdio setup, and Claude Code skill
# installation behavior here so the standalone and wizard flows stay aligned.
setup_app = typer.Typer(
    help="Register Skyvern MCP with AI coding tools.",
    invoke_without_command=True,
)

_DEFAULT_REMOTE_URL = "https://api.skyvern.com/mcp/"
_DEFAULT_CLAUDE_DESKTOP_BUNDLE_URL = (
    "https://github.com/Skyvern-AI/skyvern/raw/main/skyvern/cli/mcpb/releases/skyvern-claude-desktop.mcpb"
)
_JSON5_COMMENT_RE = re.compile(r"(?<!:)//|/\*")
_JSON5_TRAILING_COMMA_RE = re.compile(r",\s*[}\]]")
_JSON5_UNQUOTED_KEY_RE = re.compile(r"(^|[{,]\s*)([A-Za-z_$][\w$-]*)\s*:", re.MULTILINE)
_JSON5_SINGLE_QUOTED_STRING_RE = re.compile(r"(^|[:[{,]\s*)'(?:[^'\\]|\\.)*'", re.MULTILINE)


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


def _get_local_env_credentials() -> tuple[str, str]:
    """Read local SKYVERN_API_KEY and SKYVERN_BASE_URL from environment or .env."""
    backend_env = resolve_backend_env_path()
    if backend_env.exists():
        load_dotenv(backend_env, override=False)

    api_key = os.environ.get("SKYVERN_API_KEY", "")
    base_url = os.environ.get("SKYVERN_BASE_URL", "")
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


def _has_node_runtime() -> bool:
    return shutil.which("node") is not None and shutil.which("npx") is not None


def _supports_claude_desktop_bundle() -> bool:
    return platform.system() in {"Darwin", "Windows"}


def _claude_desktop_bundle_message() -> str:
    if not _supports_claude_desktop_bundle():
        return "Claude Desktop remote setup on this platform still requires Node.js because the one-click `.mcpb` installer is only available in Claude Desktop for macOS and Windows."
    return (
        "Claude Desktop remote setup via JSON still uses `mcp-remote`, which requires Node.js.\n"
        f"Download the latest one-click Skyvern bundle (`skyvern-claude-desktop.mcpb`) from: {_DEFAULT_CLAUDE_DESKTOP_BUNDLE_URL}\n"
        "Then double-click the downloaded `.mcpb`, click Install in Claude Desktop, paste your API key, and click Save."
    )


def _build_local_mcp_entry(
    api_key: str,
    base_url: str,
    use_python_path: bool = False,
    command: str | None = None,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> dict:
    """Build a stdio MCP entry for local self-hosted mode.

    The active interpreter path is always used so local venv and editable
    installs work without relying on a `skyvern` binary on PATH.
    """
    env_block: dict[str, str] = {}
    if base_url:
        env_block["SKYVERN_BASE_URL"] = base_url
    if api_key:
        env_block["SKYVERN_API_KEY"] = api_key
    if browser_type:
        env_block["BROWSER_TYPE"] = browser_type
    if browser_remote_debugging_url:
        env_block["BROWSER_REMOTE_DEBUGGING_URL"] = browser_remote_debugging_url

    _ = use_python_path

    command_name = command or sys.executable
    if command_name == "skyvern":
        return {
            "command": command_name,
            "args": ["run", "mcp"],
            "env": env_block,
        }

    return {
        "command": command_name,
        "args": ["-m", "skyvern", "run", "mcp"],
        "env": env_block,
    }


def _build_openclaw_mcp_entry(api_key: str, url: str = _DEFAULT_REMOTE_URL) -> dict:
    """Build an OpenClaw remote MCP entry."""
    entry: dict = {
        "url": url,
        "transport": "streamable-http",
    }
    if api_key:
        entry["headers"] = {"x-api-key": api_key}
    return entry


def _normalize_openclaw_remote_entry(entry: dict, *, api_key: str, url: str) -> dict:
    """Normalize an OpenClaw remote entry to `{url, transport, headers}` shape.

    This repairs legacy/generic HTTP shapes that may have been copied from
    other MCP clients by migrating non-auth `http_headers` into `headers`,
    removing generic fields like `type`, and enforcing a transport field.
    """
    normalized = copy.deepcopy(entry)
    headers = dict(normalized.get("headers") or {})
    legacy_headers = normalized.pop("http_headers", None)
    if isinstance(legacy_headers, dict):
        for key, value in legacy_headers.items():
            if key != "x-api-key":
                headers.setdefault(key, value)
    headers["x-api-key"] = api_key
    normalized["headers"] = headers
    normalized.pop("type", None)
    normalized["transport"] = str(normalized.get("transport") or "streamable-http")
    normalized["url"] = url
    return normalized


def _walk_nested_path(config: dict, path: list[str], *, create: bool = False) -> tuple[dict | None, str | None]:
    """Walk a nested mapping path and optionally create missing nodes."""
    node = config
    walked: list[str] = []
    full_path = ".".join(path)
    for key in path:
        walked.append(key)
        child = node.get(key)
        if child is None:
            if not create:
                return {}, None
            child = {}
            node[key] = child
        if not isinstance(child, dict):
            return None, f"Invalid nested structure: {'.'.join(walked)} in {full_path} must be a mapping."
        node = child
    return node, None


def _has_api_key(entry: dict | None) -> bool:
    """Check whether an MCP config entry carries an API key (remote, local, or mcp-remote bridge format)."""
    if not entry:
        return False
    if entry.get("headers", {}).get("x-api-key"):
        return True
    if entry.get("http_headers", {}).get("x-api-key"):
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


def _load_yaml_config(path: Path) -> dict | None:
    """Load a YAML config file. Returns empty dict if missing, ``None`` on parse failure."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            console.print(
                f"[yellow]Warning: {path} is not a YAML mapping — skipping to preserve original file[/yellow]"
            )
            return None
        return data
    except Exception:
        console.print(f"[yellow]Warning: could not parse {path} — skipping update to preserve original file[/yellow]")
        return None


def _save_yaml_config(path: Path, data: dict) -> None:
    """Write a dict to a YAML config file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _mask_secrets(entry: dict) -> dict:
    """Return a copy of an MCP config entry with API keys masked for display."""
    masked = copy.deepcopy(entry)

    # Remote HTTP format: headers.x-api-key
    if "headers" in masked and "x-api-key" in masked["headers"]:
        key = masked["headers"]["x-api-key"]
        masked["headers"]["x-api-key"] = _mask_key(key)

    if "http_headers" in masked and "x-api-key" in masked["http_headers"]:
        key = masked["http_headers"]["x-api-key"]
        masked["http_headers"]["x-api-key"] = _mask_key(key)

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


def _load_mcp_config(config_path: Path) -> tuple[dict | None, str | None]:
    """Load and validate an MCP config file, returning (config, error)."""
    if not config_path.exists():
        return {}, None

    try:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, f"Cannot parse {config_path}. Fix the JSON and re-run."

    if not isinstance(existing, dict):
        return None, f"{config_path} must contain a top-level JSON object."

    servers = existing.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return None, f"{config_path} has invalid `mcpServers`; expected a JSON object."

    return existing, None


def _load_openclaw_config(config_path: Path) -> tuple[dict | None, str | None]:
    """Load an OpenClaw config file, accepting OpenClaw's JSON5-on-read format.

    OpenClaw accepts JSON5 syntax in `openclaw.json`. Skyvern normalizes writes
    back to standard JSON, so comments and JSON5-only formatting are not
    preserved when this file is rewritten.
    """
    if not config_path.exists():
        return {}, None

    try:
        existing = json5.loads(config_path.read_text(encoding="utf-8"))
    except ValueError:
        return None, f"Cannot parse {config_path}. Fix the JSON5/JSON and re-run."

    if not isinstance(existing, dict):
        return None, f"{config_path} must contain a top-level JSON object."

    return existing, None


def _read_mcp_config(config_path: Path) -> dict:
    """Load an MCP config or exit with a user-friendly message."""
    existing, error = _load_mcp_config(config_path)
    if error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1)
    return existing or {}


def _find_server_key(servers: dict[object, object], preferred: str = "skyvern") -> str | None:
    """Find an existing server key case-insensitively."""
    for key in servers:
        if isinstance(key, str) and key.lower() == preferred.lower():
            return key
    return None


def _backup_config_path(config_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return config_path.with_name(f"{config_path.name}.bak-{timestamp}")


def _write_mcp_config(config_path: Path, config: dict, create_backup: bool = True) -> Path | None:
    """Write an MCP config, creating a backup of the prior file when overwriting."""
    backup_path: Path | None = None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if create_backup and config_path.exists():
        backup_path = _backup_config_path(config_path)
        shutil.copy2(config_path, backup_path)

    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return backup_path


def _preview_upsert_change(current: dict | None, new_entry: dict, tool_name: str) -> bool:
    """Preview a config upsert and return whether a write should proceed."""
    if current == new_entry:
        console.print(f"[green]Already configured for {tool_name} (no changes)[/green]")
        return False

    if _has_api_key(current) and not _has_api_key(new_entry):
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
    console.print(Syntax(json.dumps(_mask_secrets(new_entry), indent=2), "json"))
    return True


def _warn_openclaw_json_normalization(config_path: Path, *, dry_run: bool) -> None:
    """Warn before rewriting an existing OpenClaw config as standard JSON."""
    if not config_path.exists():
        return
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    if not _looks_like_json5_source(raw_text):
        return

    prefix = "Dry run note:" if dry_run else "Note:"
    console.print(
        "[yellow]"
        f"{prefix} OpenClaw accepts JSON5 in {config_path.name}, but Skyvern rewrites the file as standard JSON. "
        "Comments and JSON5-only formatting, if present, will not be preserved."
        "[/yellow]"
    )


def _looks_like_json5_source(raw_text: str) -> bool:
    stripped_text = _strip_double_quoted_strings(raw_text)
    return bool(
        _JSON5_COMMENT_RE.search(stripped_text)
        or _JSON5_TRAILING_COMMA_RE.search(stripped_text)
        or _JSON5_UNQUOTED_KEY_RE.search(stripped_text)
        or _JSON5_SINGLE_QUOTED_STRING_RE.search(stripped_text)
    )


def _strip_double_quoted_strings(raw_text: str) -> str:
    stripped: list[str] = []
    in_string = False
    escaped = False

    for char in raw_text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            stripped.append(" ")
            continue

        if char == '"':
            in_string = True
            stripped.append(" ")
            continue

        stripped.append(char)

    return "".join(stripped)


def _write_openclaw_config(config_path: Path, config: dict) -> Path | None:
    _warn_openclaw_json_normalization(config_path, dry_run=False)
    return _write_mcp_config(config_path, config, create_backup=True)


def _upsert_mcp_config(
    config_path: Path,
    tool_name: str,
    skyvern_entry: dict,
    server_key: str = "skyvern",
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Read config, diff, prompt, and write. Idempotent."""
    existing = _read_mcp_config(config_path)
    servers = existing.setdefault("mcpServers", {})
    resolved_server_key = _find_server_key(servers, preferred=server_key) or server_key
    current = servers.get(resolved_server_key)

    if not _preview_upsert_change(current, skyvern_entry, tool_name):
        return

    if dry_run:
        console.print(f"\n[yellow]Dry run -- no changes written to {config_path}[/yellow]")
        return

    if not yes:
        if not typer.confirm("\nApply changes?"):
            raise typer.Abort()

    servers[resolved_server_key] = skyvern_entry
    backup_path = _write_mcp_config(config_path, existing, create_backup=True)
    console.print(f"[green]Configured {tool_name} at {config_path}[/green]")
    if backup_path is not None:
        console.print(f"[dim]Backup saved to {backup_path}[/dim]")


def _build_entry(
    api_key: str,
    base_url: str,
    *,
    local: bool,
    use_python_path: bool,
    url: str | None,
    use_mcp_remote_bridge: bool = False,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> dict:
    if local:
        return _build_local_mcp_entry(
            api_key,
            base_url,
            use_python_path=use_python_path,
            browser_type=browser_type,
            browser_remote_debugging_url=browser_remote_debugging_url,
        )
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
    try:
        return _cursor_config_path().parent.is_dir()
    except typer.Exit:
        return False


def _is_windsurf_installed() -> bool:
    try:
        return _windsurf_config_path().parent.is_dir()
    except typer.Exit:
        return False


def _is_claude_desktop_installed() -> bool:
    try:
        return _claude_desktop_config_path().parent.is_dir()
    except typer.Exit:
        return False


def _get_known_tools() -> list[DetectedTool]:
    """Return all known AI coding tools in detection order.

    Note: Codex is not included in the guided setup flow yet.
    Its config is TOML rather than the JSON shape used by the current setup commands.
    OpenClaw is also excluded intentionally because it uses a nested JSON5 config
    shape rather than the flat JSON config handled by the generic guided setup path.
    """
    return [
        DetectedTool(
            name="Claude Code",
            config_path_fn=_claude_code_default_config_path,
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


def _resolve_setup_credentials(*, api_key_flag: str | None, yes: bool, local: bool) -> tuple[str, str]:
    """Resolve credentials/base URL for local or remote MCP setup."""
    if local:
        env_key, env_url = _get_local_env_credentials()
        resolved_key = api_key_flag or env_key
        if not env_url or not resolved_key:
            console.print(
                "[red bold]Error:[/red bold] Local MCP setup needs SKYVERN_BASE_URL and SKYVERN_API_KEY. "
                "Run `skyvern init` or `skyvern quickstart` in local mode first, or set those env vars manually."
            )
            raise typer.Exit(code=1)
        return resolved_key, env_url

    resolved_key = _acquire_api_key(api_key_flag, yes)
    _, env_url = _get_env_credentials()
    return resolved_key, env_url


# ---------------------------------------------------------------------------
# Config path resolvers
# ---------------------------------------------------------------------------


def _claude_desktop_config_path() -> Path:
    system = detect_os()
    if system == "wsl":
        roaming_path = get_windows_appdata_roaming()
        if roaming_path is None:
            console.print("[red]Could not locate Windows AppData\\\\Roaming from WSL.[/red]")
            raise typer.Exit(code=1)
        return Path(roaming_path) / "Claude" / "claude_desktop_config.json"
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "linux":
        candidates = [
            Path.home() / ".config" / "Claude",
            Path.home() / ".local" / "share" / "Claude",
            Path.home() / "Claude",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate / "claude_desktop_config.json"
        return candidates[0] / "claude_desktop_config.json"
    if system == "windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            console.print("[red]APPDATA environment variable not set on Windows.[/red]")
            raise typer.Exit(code=1)
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    console.print(f"[red]Unsupported platform: {system}[/red]")
    raise typer.Exit(code=1)


def _wsl_windows_user_home() -> Path:
    roaming_path = get_windows_appdata_roaming()
    if roaming_path is None:
        console.print("[red]Could not locate Windows AppData\\\\Roaming from WSL.[/red]")
        raise typer.Exit(code=1)
    return roaming_path.parent.parent


def _cursor_config_path() -> Path:
    if detect_os() == "wsl":
        return _wsl_windows_user_home() / ".cursor" / "mcp.json"
    return Path.home() / ".cursor" / "mcp.json"


def _windsurf_config_path() -> Path:
    if detect_os() == "wsl":
        return _wsl_windows_user_home() / ".codeium" / "windsurf" / "mcp_config.json"
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def _claude_code_global_config_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_code_project_config_path() -> Path:
    return Path.cwd() / ".mcp.json"


def _codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _openclaw_config_path() -> Path:
    config_override = os.environ.get("OPENCLAW_CONFIG_PATH")
    if config_override:
        return Path(config_override).expanduser()
    # Unlike Cursor/Windsurf, OpenClaw may run directly inside WSL. Resolve the
    # config from the current runtime's home directory so `skyvern setup
    # openclaw` targets the same file `openclaw` reads in that environment.
    return Path.home() / ".openclaw" / "openclaw.json"


_PROJECT_MARKERS = (
    ".git",
    ".mcp.json",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "setup.py",
    "Cargo.toml",
    "go.mod",
)


def _looks_like_project_dir(path: Path) -> bool:
    return any((path / marker).exists() for marker in _PROJECT_MARKERS)


def _claude_code_config_target(
    *,
    cwd: Path | None = None,
    project: bool = False,
    global_config: bool = False,
) -> tuple[Path, bool]:
    """Resolve Claude Code config path and whether project-local skills should be installed."""
    if project and global_config:
        console.print("[red]Choose only one of --project or --global.[/red]")
        raise typer.Exit(code=1)

    working_dir = cwd or Path.cwd()
    in_project = _looks_like_project_dir(working_dir)

    if project:
        return working_dir / ".mcp.json", True
    if global_config:
        return _claude_code_global_config_path(), in_project
    if in_project:
        return working_dir / ".mcp.json", True
    return _claude_code_global_config_path(), False


def _claude_code_default_config_path() -> Path:
    return _claude_code_config_target()[0]


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

_api_key_opt = typer.Option(None, "--api-key", "-k", help="Skyvern API key (reads from env if omitted)")
_dry_run_opt = typer.Option(False, "--dry-run", help="Show changes without writing")
_yes_opt = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt")
_local_opt = typer.Option(False, "--local", help="Use local stdio transport instead of remote HTTPS")
_python_path_opt = typer.Option(
    False,
    "--use-python-path",
    help="Deprecated compatibility flag. Local stdio setup already uses the active Python interpreter path.",
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
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    if tool_name == "Claude Desktop" and not local and use_mcp_remote_bridge and not _has_node_runtime():
        console.print(f"[yellow]{_claude_desktop_bundle_message()}[/yellow]")
        raise typer.Exit(code=1)

    resolved_key, env_url = _resolve_setup_credentials(api_key_flag=api_key, yes=yes, local=local)
    entry = _build_entry(
        resolved_key,
        env_url,
        local=local,
        use_python_path=use_python_path,
        url=url,
        use_mcp_remote_bridge=use_mcp_remote_bridge,
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
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

    # Step 1: Credentials
    console.print("[bold]Step 1: Credentials[/bold]")
    resolved_key, env_url = _resolve_setup_credentials(api_key_flag=api_key, yes=yes, local=local)
    if local:
        console.print("[green]Local SKYVERN_BASE_URL and SKYVERN_API_KEY ready.[/green]\n")
    else:
        console.print("[green]API key ready.[/green]\n")
    capture_setup_event("quickstart-api-key", success=True, extra_data={"local": local})

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
    bundle_recommended: list[str] = []

    for tool in detected:
        try:
            config_path = tool.config_path_fn()
            use_bridge = tool.use_mcp_remote_bridge and not local
            if tool.name == "Claude Desktop" and use_bridge and not _has_node_runtime():
                bundle_recommended.append(tool.name)
                console.print(
                    f"[yellow]Skipping Claude Desktop JSON setup.[/yellow] {_claude_desktop_bundle_message()}"
                )
                continue
            # Pass browser config from env for local mode
            env_browser_type = os.environ.get("BROWSER_TYPE") if local else None
            env_browser_url = os.environ.get("BROWSER_REMOTE_DEBUGGING_URL") if local else None
            entry = _build_entry(
                resolved_key,
                env_url,
                local=local,
                use_python_path=use_python_path,
                url=url,
                use_mcp_remote_bridge=use_bridge,
                browser_type=env_browser_type,
                browser_remote_debugging_url=env_browser_url,
            )
            _upsert_mcp_config(config_path, tool.name, entry, dry_run=dry_run, yes=True)
            if tool.name == "Claude Code":
                _, install_skills = _claude_code_config_target()
                if install_skills:
                    _install_skills(Path.cwd(), dry_run=dry_run)
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
        if bundle_recommended:
            console.print(f"[yellow]Claude Desktop:[/yellow] {_claude_desktop_bundle_message()}")
        capture_setup_event(
            "quickstart-complete",
            success=True,
            extra_data={"configured": configured, "failed": failed, "bundle_recommended": bundle_recommended},
        )
    else:
        if bundle_recommended and not failed:
            console.print(
                Panel(
                    f"[bold yellow]Claude Desktop detected[/bold yellow]\n\n{_claude_desktop_bundle_message()}",
                    border_style="yellow",
                )
            )
        else:
            console.print("[red]No tools were configured.[/red]")
        capture_setup_event(
            "quickstart-complete",
            success=not failed and bool(bundle_recommended),
            error_type="all_tools_failed" if failed else None,
            extra_data={"failed": failed, "bundle_recommended": bundle_recommended},
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
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    """Register Skyvern MCP with Claude Desktop (remote mode requires Node.js; bundle is recommended otherwise)."""
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
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
    )


@setup_app.command("claude-desktop", hidden=True)
def setup_claude_desktop_alias(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
) -> None:
    """Backward-compatible alias for `skyvern setup claude`."""
    setup_claude(
        api_key, dry_run, yes, local, use_python_path, url, browser_type=None, browser_remote_debugging_url=None
    )


@setup_app.command("claude-code")
def setup_claude_code(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    project: bool = typer.Option(
        False, "--project", help="Write Claude Code MCP config to .mcp.json in the current directory"
    ),
    global_config: bool = typer.Option(
        False,
        "--global",
        help="Write Claude Code MCP config to ~/.claude.json even if the current directory is a project",
    ),
    skip_skills: bool = typer.Option(False, "--skip-skills", help="Don't install Claude Code skills (e.g. /qa)"),
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    """Register Skyvern MCP with Claude Code and install skills (remote by default)."""
    config_path, install_skills = _claude_code_config_target(project=project, global_config=global_config)
    if not project and not global_config:
        target_label = ".mcp.json in the current project" if install_skills else "~/.claude.json"
        console.print(f"[dim]Claude Code target: {target_label}[/dim]")
    _run_setup(
        "Claude Code",
        config_path,
        api_key,
        dry_run,
        yes,
        local,
        use_python_path,
        url,
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
    )

    if not skip_skills:
        if install_skills:
            _install_skills(Path.cwd(), dry_run=dry_run)
        else:
            console.print(
                "[dim]Skipping Claude Code skill installation because the current directory does not look like a project. "
                "Re-run inside your repo or pass --project to install /qa and other bundled skills locally.[/dim]"
            )


@setup_app.command("cursor")
def setup_cursor(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    """Register Skyvern MCP with Cursor (remote by default)."""
    _run_setup(
        "Cursor",
        _cursor_config_path(),
        api_key,
        dry_run,
        yes,
        local,
        use_python_path,
        url,
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
    )


@setup_app.command("windsurf")
def setup_windsurf(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    """Register Skyvern MCP with Windsurf (remote by default)."""
    _run_setup(
        "Windsurf",
        _windsurf_config_path(),
        api_key,
        dry_run,
        yes,
        local,
        use_python_path,
        url,
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
    )


def _merge_openclaw_remote_entry(current: dict | None, desired: dict) -> dict:
    """Preserve OpenClaw-specific remote keys like timeouts when reconfiguring an existing entry."""
    # If the existing entry is stdio-shaped, remote setup intentionally replaces
    # it with the desired OpenClaw remote shape.
    if not isinstance(current, dict) or "command" in current or "args" in current:
        return desired
    return _normalize_openclaw_remote_entry(
        current,
        api_key=str(desired.get("headers", {}).get("x-api-key", "")),
        url=str(desired["url"]),
    )


@setup_app.command("openclaw")
def setup_openclaw(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    use_python_path: bool = _python_path_opt,
    url: str | None = _url_opt,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    """Register Skyvern MCP with OpenClaw (remote by default, --local for stdio)."""
    resolved_key, env_url = _resolve_setup_credentials(api_key_flag=api_key, yes=yes, local=local)
    entry = _build_entry(
        resolved_key,
        env_url,
        local=local,
        use_python_path=use_python_path,
        url=url,
        browser_type=browser_type,
        browser_remote_debugging_url=browser_remote_debugging_url,
    )
    if not local:
        entry = _build_openclaw_mcp_entry(resolved_key, entry["url"])

    config_path = _openclaw_config_path()
    existing, error = _load_openclaw_config(config_path)
    if error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1)

    config = existing or {}
    servers_result, error = _walk_nested_path(config, ["mcp", "servers"], create=True)
    if error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1)
    servers = cast(dict, servers_result)

    server_key = _find_server_key(servers, preferred="skyvern") or "skyvern"
    current = servers.get(server_key)
    if current is not None and not isinstance(current, dict):
        console.print(f"[red]Invalid existing OpenClaw MCP entry for '{server_key}'; expected a JSON object.[/red]")
        raise typer.Exit(code=1)

    next_entry = _merge_openclaw_remote_entry(current, entry) if not local else entry
    if not _preview_upsert_change(current, next_entry, "OpenClaw"):
        return

    if dry_run:
        _warn_openclaw_json_normalization(config_path, dry_run=True)
        console.print(f"\n[yellow]Dry run -- no changes written to {config_path}[/yellow]")
        return

    if not yes and not typer.confirm("\nApply changes?"):
        raise typer.Abort()

    servers[server_key] = next_entry
    backup_path = _write_openclaw_config(config_path, config)
    console.print(f"[green]Configured OpenClaw at {config_path}[/green]")
    if backup_path is not None:
        console.print(f"[dim]Backup saved to {backup_path}[/dim]")


@setup_app.command("hermes")
def setup_hermes(
    api_key: str | None = _api_key_opt,
    dry_run: bool = _dry_run_opt,
    yes: bool = _yes_opt,
    local: bool = _local_opt,
    url: str | None = _url_opt,
) -> None:
    """Register Skyvern MCP with Hermes (remote by default, --local for stdio)."""
    env_key, env_base_url = _get_env_credentials()
    resolved_key = api_key or env_key

    if local:
        # Local stdio mode: Hermes spawns `skyvern run mcp` as a child process
        local_key, local_base_url = _get_local_env_credentials()
        resolved_local_key = api_key or local_key or resolved_key or ""
        resolved_base_url = local_base_url or ""
        if not resolved_base_url:
            console.print(
                "[red]No base URL found for local mode. Set [bold]SKYVERN_BASE_URL[/bold] "
                "(e.g. http://localhost:8000) in your environment, then retry.[/red]"
            )
            raise typer.Exit(code=1)
        if not resolved_local_key:
            console.print(
                "[red]No API key found. Run [bold]skyvern login[/bold] or set "
                "[bold]SKYVERN_API_KEY[/bold] in your environment, then retry.[/red]"
            )
            raise typer.Exit(code=1)
        local_entry = _build_local_mcp_entry(resolved_local_key, resolved_base_url)
        hermes_entry: dict = {
            "command": local_entry.get("command", sys.executable),
            "args": local_entry.get("args", ["-m", "skyvern", "run", "mcp"]),
        }
        if local_entry.get("env"):
            hermes_entry["env"] = local_entry["env"]
    else:
        # Remote HTTP mode: Hermes connects to hosted MCP server
        if not resolved_key:
            console.print(
                "[red]No API key found. Run [bold]skyvern login[/bold] or set "
                "[bold]SKYVERN_API_KEY[/bold] in your environment, then retry.[/red]"
            )
            raise typer.Exit(code=1)

        mcp_url = url or _DEFAULT_REMOTE_URL
        parsed = urlparse(mcp_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            console.print(f"[red]Invalid URL: {mcp_url} (must be a full URL like https://api.skyvern.com/mcp/)[/red]")
            raise typer.Exit(code=1)

        hermes_entry = {
            "url": mcp_url,
            "headers": {"x-api-key": resolved_key},
        }

    # Discover all Hermes config locations: global + per-profile
    hermes_home = Path.home() / ".hermes"
    config_paths: list[Path] = [hermes_home / "config.yaml"]
    profiles_dir = hermes_home / "profiles"
    if profiles_dir.is_dir():
        for profile_dir in sorted(profiles_dir.iterdir()):
            profile_config = profile_dir / "config.yaml"
            if profile_dir.is_dir() and profile_config.exists():
                config_paths.append(profile_config)

    if dry_run:
        for cp in config_paths:
            data = _load_yaml_config(cp)
            if data is None:
                console.print(f"[yellow]Skipping {cp} (could not parse)[/yellow]")
                continue
            servers = data.get("mcp_servers")
            if servers is not None and not isinstance(servers, dict):
                console.print(f"[yellow]Skipping {cp} (mcp_servers is not a mapping)[/yellow]")
                continue
            if servers is None:
                data["mcp_servers"] = {}
            server_key = _find_server_key(data["mcp_servers"], preferred="skyvern") or "skyvern"
            data["mcp_servers"][server_key] = hermes_entry
            masked_data = copy.deepcopy(data)
            masked_data["mcp_servers"][server_key] = _mask_secrets(masked_data["mcp_servers"][server_key])
            console.print(f"[bold]{cp}[/bold]")
            console.print(Syntax(yaml.dump(masked_data, default_flow_style=False, sort_keys=False), "yaml"))
        console.print(f"[yellow]Dry run -- no changes written to {len(config_paths)} config(s)[/yellow]")
        return

    if not yes:
        paths_str = "\n".join(f"  - {cp}" for cp in config_paths)
        console.print(f"[bold]Will add Skyvern MCP to {len(config_paths)} Hermes config(s):[/bold]\n{paths_str}")
        if not typer.confirm("Apply changes?"):
            raise typer.Abort()

    updated: list[str] = []
    backups: list[Path] = []
    for cp in config_paths:
        data = _load_yaml_config(cp)
        if data is None:
            console.print(f"[yellow]Skipping {cp} (could not parse)[/yellow]")
            continue
        servers = data.get("mcp_servers")
        if servers is not None and not isinstance(servers, dict):
            console.print(f"[yellow]Skipping {cp} (mcp_servers is not a mapping)[/yellow]")
            continue
        if servers is None:
            data["mcp_servers"] = {}
        server_key = _find_server_key(data["mcp_servers"], preferred="skyvern") or "skyvern"
        if data["mcp_servers"].get(server_key) == hermes_entry:
            continue
        data["mcp_servers"][server_key] = hermes_entry
        if cp.exists():
            backup = _backup_config_path(cp)
            shutil.copy2(cp, backup)
            backups.append(backup)
        _save_yaml_config(cp, data)
        updated.append(str(cp))

    if not updated:
        console.print("[green]All Hermes configs are already up to date.[/green]")
        return

    masked_key = _mask_key(resolved_key) if resolved_key else "(none)"
    updated_str = "\n".join(f"  {p}" for p in updated)
    console.print(
        Panel(
            f"[bold green]Hermes configured![/bold green]\n\n"
            f"Updated {len(updated)} config(s):\n{updated_str}\n\nAPI key: {masked_key}",
            border_style="green",
        )
    )


@setup_app.command("mcporter")
def setup_mcporter() -> None:
    """Show MCPorter integration status (MCPorter auto-discovers from existing MCP configs)."""
    console.print(
        Panel(
            "[bold]MCPorter Integration[/bold]\n\n"
            "MCPorter automatically discovers MCP servers from existing tool configs.\n"
            "No additional configuration is needed — just ensure at least one tool is set up.",
            border_style="blue",
        )
    )

    config_checks: list[tuple[str, str, Path]] = [
        ("Claude Desktop", "skyvern setup claude", _claude_desktop_config_path()),
        ("Claude Code (global)", "skyvern setup claude-code --global", _claude_code_global_config_path()),
        ("Claude Code (project)", "skyvern setup claude-code --project", _claude_code_project_config_path()),
        ("Cursor", "skyvern setup cursor", _cursor_config_path()),
        ("Windsurf", "skyvern setup windsurf", _windsurf_config_path()),
    ]

    found: list[str] = []
    for name, _cmd, path in config_checks:
        try:
            if not path.exists():
                continue
            cfg, err = _load_mcp_config(path)
            if err or not cfg:
                continue
            servers = cfg.get("mcpServers", {})
            if _find_server_key(servers, "skyvern"):
                console.print(f"  [green]\u2713[/green] {name} \u2014 {path}")
                found.append(name)
        except Exception:
            continue

    if found:
        console.print(f"\n[green]MCPorter can discover Skyvern from {len(found)} config(s).[/green]")
    else:
        console.print(
            "\n[yellow]No existing Skyvern MCP configs found.[/yellow]\n"
            "Set up at least one tool first:\n"
            "  skyvern setup cursor\n"
            "  skyvern setup claude-code\n"
            "  skyvern setup claude"
        )
