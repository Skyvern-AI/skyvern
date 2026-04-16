"""CLI commands for switching local MCP client configs and managing optional saved profiles."""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, urlunparse

import toml
import typer
import yaml
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table

from .console import console
from .setup_commands import (
    _backup_config_path,
    _claude_code_global_config_path,
    _claude_code_project_config_path,
    _claude_desktop_config_path,
    _codex_config_path,
    _cursor_config_path,
    _find_server_key,
    _get_env_credentials,
    _load_mcp_config,
    _load_yaml_config,
    _mask_key,
    _mask_secrets,
    _save_yaml_config,
    _windsurf_config_path,
    _write_mcp_config,
)

mcp_app = typer.Typer(help="Manage local MCP configs and optional saved Skyvern profiles.", no_args_is_help=True)
profile_app = typer.Typer(help="Manage saved Skyvern MCP profiles.", no_args_is_help=True)
mcp_app.add_typer(profile_app, name="profile")

_MANUAL_SOURCE_NAME = "Manual entry"
_DEFAULT_BASE_URL = "https://api.skyvern.com"
_PROFILE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONFIG_FORMAT_JSON = "json"
_CONFIG_FORMAT_CODEX = "codex_toml"
_CONFIG_FORMAT_YAML = "yaml"


@dataclass(frozen=True)
class MCPProfile:
    name: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class SwitchTarget:
    name: str
    config_path: Path
    entry_key: str | None
    entry: dict | None
    config_format: str = _CONFIG_FORMAT_JSON
    error: str | None = None


@dataclass
class ProfileChoice:
    label: str
    profile: MCPProfile
    sources: list[str]
    saved_name: str | None = None


@dataclass(frozen=True)
class SwitchTargetSpec:
    name: str
    config_path_fn: Callable[[], Path]
    config_format: str = _CONFIG_FORMAT_JSON


def _sanitize_prompt_response(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value)
    return "".join(ch for ch in cleaned if ch.isprintable()).strip()


def _prompt_text(prompt: str, *, default: str | None = None, password: bool = False) -> str:
    return _sanitize_prompt_response(Prompt.ask(prompt, default=default, password=password))


def _ask_choice(prompt: str, *, choices: list[str], default: str | None = None) -> str:
    allowed = set(choices)
    while True:
        response = _prompt_text(prompt, default=default)
        if response in allowed:
            return response
        console.print(f"[red]Invalid choice. Enter one of: {', '.join(choices)}[/red]")


def _profile_store_dir() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "skyvern" / "mcp-profiles"

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "skyvern" / "mcp-profiles"

    return Path.home() / ".config" / "skyvern" / "mcp-profiles"


def _profile_slug(name: str) -> str:
    slug = _PROFILE_FILENAME_RE.sub("-", name.strip()).strip("-.").lower()
    if not slug:
        raise typer.BadParameter("Profile name must include at least one letter or number.")
    return slug


def _profile_path(name: str) -> Path:
    return _profile_store_dir() / f"{_profile_slug(name)}.json"


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip()
    if not normalized:
        raise typer.BadParameter("Base URL cannot be empty.")

    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise typer.BadParameter("Base URL must start with http:// or https://")

    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[: -len("/mcp")]

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def _profile_to_mcp_url(base_url: str) -> str:
    return f"{_normalize_base_url(base_url)}/mcp/"


def _build_profile(name: str, api_key: str, base_url: str) -> MCPProfile:
    clean_name = name.strip()
    clean_key = api_key.strip()
    if not clean_name:
        raise typer.BadParameter("Profile name cannot be empty.")
    if not clean_key:
        raise typer.BadParameter("API key cannot be empty.")

    return MCPProfile(
        name=clean_name,
        api_key=clean_key,
        base_url=_normalize_base_url(base_url),
    )


def _load_profile_from_path(path: Path) -> MCPProfile:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")

    name = data.get("name", "")
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    for field_name, value in (("name", name), ("api_key", api_key), ("base_url", base_url)):
        if not isinstance(value, str):
            raise ValueError(f"{path} field '{field_name}' must be a string.")

    return _build_profile(
        name,
        api_key,
        base_url,
    )


def _list_profiles() -> list[MCPProfile]:
    store_dir = _profile_store_dir()
    if not store_dir.exists():
        return []

    profiles: list[MCPProfile] = []
    for path in sorted(store_dir.glob("*.json")):
        try:
            profiles.append(_load_profile_from_path(path))
        except Exception as exc:
            console.print(f"[yellow]Skipping invalid profile {path.name}: {exc}[/yellow]")

    return sorted(profiles, key=lambda profile: profile.name.lower())


def _load_profile(name: str) -> MCPProfile:
    path = _profile_path(name)
    if not path.exists():
        console.print(f"[red]No saved MCP profile named '{name}'.[/red]")
        raise typer.Exit(code=1)
    try:
        return _load_profile_from_path(path)
    except Exception as exc:
        console.print(f"[red]Failed to load profile '{name}': {exc}[/red]")
        raise typer.Exit(code=1) from exc


def _save_profile(profile: MCPProfile, overwrite: bool = False) -> Path:
    path = _profile_path(profile.name)
    if path.exists() and not overwrite:
        console.print(
            f"[red]Profile '{profile.name}' already exists at {path}. Re-run with --overwrite to replace it.[/red]"
        )
        raise typer.Exit(code=1)

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

    payload = {
        "name": profile.name,
        "api_key": profile.api_key,
        "base_url": profile.base_url,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


def _prompt_for_manual_source() -> MCPProfile:
    env_key, env_base_url = _get_env_credentials()
    default_base_url = env_base_url or _DEFAULT_BASE_URL

    api_key = _prompt_text("Skyvern API key", password=True)
    base_url = _prompt_text("Skyvern base URL (remote configs derive /mcp automatically)", default=default_base_url)
    return _build_profile(_MANUAL_SOURCE_NAME, api_key, base_url)


def _extract_entry_base_url(entry: dict) -> str:
    location = _entry_location(entry).strip()
    if not location:
        return ""

    try:
        return _normalize_base_url(location)
    except typer.BadParameter:
        return ""


def _server_block_key(config_format: str) -> str:
    if config_format in (_CONFIG_FORMAT_CODEX, _CONFIG_FORMAT_YAML):
        return "mcp_servers"
    return "mcpServers"


def _load_codex_config(config_path: Path) -> tuple[dict | None, str | None]:
    if not config_path.exists():
        return {}, None

    try:
        existing = toml.loads(config_path.read_text(encoding="utf-8"))
    except toml.TomlDecodeError:
        return None, f"Cannot parse {config_path}. Fix the TOML and re-run."

    if not isinstance(existing, dict):
        return None, f"{config_path} must contain a top-level TOML table."

    servers = existing.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        return None, f"{config_path} has invalid `mcp_servers`; expected a TOML table."

    return existing, None


def _load_switch_config(config_path: Path, config_format: str) -> tuple[dict | None, str | None]:
    if config_format == _CONFIG_FORMAT_CODEX:
        return _load_codex_config(config_path)
    if config_format == _CONFIG_FORMAT_YAML:
        data = _load_yaml_config(config_path)
        if data is None:
            return None, f"Cannot parse {config_path}. Fix the YAML and re-run."
        return data, None
    return _load_mcp_config(config_path)


def _write_codex_config(config_path: Path, config: dict, create_backup: bool = True) -> Path | None:
    backup_path: Path | None = None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if create_backup and config_path.exists():
        backup_path = _backup_config_path(config_path)
        shutil.copy2(config_path, backup_path)

    content = toml.dumps(config)
    if not content.endswith("\n"):
        content += "\n"
    config_path.write_text(content, encoding="utf-8")
    return backup_path


def _write_switch_config(config_path: Path, config: dict, config_format: str) -> Path | None:
    if config_format == _CONFIG_FORMAT_CODEX:
        return _write_codex_config(config_path, config, create_backup=True)
    if config_format == _CONFIG_FORMAT_YAML:
        backup_path: Path | None = None
        if config_path.exists():
            backup_path = _backup_config_path(config_path)
            shutil.copy2(config_path, backup_path)
        _save_yaml_config(config_path, config)
        return backup_path
    return _write_mcp_config(config_path, config, create_backup=True)


def _hermes_config_path() -> Path:
    return Path.home() / ".hermes" / "config.yaml"


def _switch_target_specs() -> list[SwitchTargetSpec]:
    specs = [
        SwitchTargetSpec("Claude Code (global)", _claude_code_global_config_path),
        SwitchTargetSpec("Claude Code (project)", _claude_code_project_config_path),
        SwitchTargetSpec("Claude Desktop", _claude_desktop_config_path),
        SwitchTargetSpec("Cursor", _cursor_config_path),
        SwitchTargetSpec("Windsurf", _windsurf_config_path),
        SwitchTargetSpec("Codex", _codex_config_path, config_format=_CONFIG_FORMAT_CODEX),
        SwitchTargetSpec("Hermes", _hermes_config_path, config_format=_CONFIG_FORMAT_YAML),
    ]
    # Discover per-profile Hermes configs alongside the global one
    profiles_dir = Path.home() / ".hermes" / "profiles"
    if profiles_dir.is_dir():
        for profile_dir in sorted(profiles_dir.iterdir()):
            profile_config = profile_dir / "config.yaml"
            if profile_dir.is_dir() and profile_config.exists():
                profile_name = profile_dir.name

                def _make_path_fn(p: Path = profile_config) -> Path:
                    return p

                specs.append(
                    SwitchTargetSpec(
                        f"Hermes ({profile_name})",
                        _make_path_fn,
                        config_format=_CONFIG_FORMAT_YAML,
                    )
                )
    return specs


def _entry_kind(entry: dict | None) -> str:
    if not entry:
        return "missing"

    command_name = Path(str(entry.get("command", ""))).name.lower()
    args = entry.get("args", [])
    if command_name == "npx" and isinstance(args, list) and args and args[0] == "mcp-remote":
        return "mcp-remote bridge"

    if isinstance(entry.get("env"), dict):
        return "local stdio"

    if entry.get("type") == "http" or "url" in entry or isinstance(entry.get("http_headers"), dict):
        return "remote http"

    return "unsupported"


def _extract_entry_api_key(entry: dict) -> str:
    headers = entry.get("headers", {})
    if isinstance(headers, dict):
        api_key = headers.get("x-api-key")
        if isinstance(api_key, str):
            return api_key

    http_headers = entry.get("http_headers", {})
    if isinstance(http_headers, dict):
        api_key = http_headers.get("x-api-key")
        if isinstance(api_key, str):
            return api_key

    env = entry.get("env", {})
    if isinstance(env, dict):
        api_key = env.get("SKYVERN_API_KEY")
        if isinstance(api_key, str):
            return api_key

    args = entry.get("args", [])
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str) and arg.startswith("x-api-key:"):
                return arg[len("x-api-key:") :]

    return ""


def _append_profile_choice(
    choices_by_key: dict[tuple[str, str], ProfileChoice],
    *,
    label: str,
    profile: MCPProfile,
    source: str,
    saved_name: str | None = None,
) -> None:
    key = (profile.api_key, profile.base_url)
    existing = choices_by_key.get(key)
    if existing is None:
        choices_by_key[key] = ProfileChoice(
            label=label,
            profile=profile,
            sources=[source],
            saved_name=saved_name,
        )
        return

    if source not in existing.sources:
        existing.sources.append(source)

    if saved_name and existing.saved_name is None:
        existing.label = label
        existing.saved_name = saved_name


def _profile_from_target(target: SwitchTarget) -> MCPProfile | None:
    if target.entry is None:
        return None

    api_key = _extract_entry_api_key(target.entry).strip()
    base_url = _extract_entry_base_url(target.entry)
    if not api_key or not base_url:
        return None

    return MCPProfile(name=f"{target.name} current", api_key=api_key, base_url=base_url)


def _collect_profile_choices(discovered: list[SwitchTarget]) -> list[ProfileChoice]:
    choices_by_key: dict[tuple[str, str], ProfileChoice] = {}

    env_key, env_base_url = _get_env_credentials()
    if env_key:
        env_profile = _build_profile("Current environment", env_key, env_base_url or _DEFAULT_BASE_URL)
        _append_profile_choice(
            choices_by_key,
            label="Current environment",
            profile=env_profile,
            source="env/.env",
        )

    for saved_profile in _list_profiles():
        _append_profile_choice(
            choices_by_key,
            label=saved_profile.name,
            profile=saved_profile,
            source="saved profile",
            saved_name=saved_profile.name,
        )

    for target in discovered:
        discovered_profile = _profile_from_target(target)
        if discovered_profile is None:
            continue
        _append_profile_choice(
            choices_by_key,
            label=f"{target.name} current config",
            profile=discovered_profile,
            source=target.name,
        )

    return sorted(choices_by_key.values(), key=lambda choice: (choice.label.lower(), choice.profile.base_url))


def _select_profile(profile_name: str | None, discovered: list[SwitchTarget]) -> MCPProfile:
    if profile_name:
        return _load_profile(profile_name)

    choices = _collect_profile_choices(discovered)

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Source")
    table.add_column("From")
    table.add_column("Base URL")
    table.add_column("API Key")

    for index, choice in enumerate(choices, start=1):
        table.add_row(
            str(index),
            choice.label,
            ", ".join(choice.sources),
            choice.profile.base_url,
            _mask_key(choice.profile.api_key),
        )

    manual_choice_index = len(choices) + 1
    table.add_row(
        str(manual_choice_index),
        "Enter manually",
        "prompt",
        "-",
        "-",
    )

    console.print("\n[bold]Available switch sources[/bold]")
    console.print(table)

    selected = _ask_choice(
        "Choose a source number for the Skyvern API key/base URL",
        choices=[str(index) for index in range(1, manual_choice_index + 1)],
        default="1" if choices else str(manual_choice_index),
    )

    if int(selected) == manual_choice_index:
        return _prompt_for_manual_source()

    return choices[int(selected) - 1].profile


def _entry_location(entry: dict) -> str:
    kind = _entry_kind(entry)
    if kind == "local stdio":
        env = entry.get("env", {})
        if isinstance(env, dict):
            return str(env.get("SKYVERN_BASE_URL", ""))
    if kind == "remote http":
        return str(entry.get("url", ""))
    if kind == "mcp-remote bridge":
        args = entry.get("args", [])
        if isinstance(args, list) and len(args) > 1 and isinstance(args[1], str):
            return args[1]
    return ""


def _discover_switch_targets() -> tuple[list[SwitchTarget], list[tuple[str, Path]]]:
    discovered: list[SwitchTarget] = []
    missing: list[tuple[str, Path]] = []

    for spec in _switch_target_specs():
        path = spec.config_path_fn()
        if not path.exists():
            missing.append((spec.name, path))
            continue

        config, error = _load_switch_config(path, spec.config_format)
        if error:
            discovered.append(
                SwitchTarget(
                    name=spec.name,
                    config_path=path,
                    entry_key=None,
                    entry=None,
                    config_format=spec.config_format,
                    error=error,
                )
            )
            continue

        servers = (config or {}).get(_server_block_key(spec.config_format), {})
        server_key = _find_server_key(servers, preferred="skyvern") if isinstance(servers, dict) else None
        entry = servers.get(server_key) if server_key else None
        discovered.append(
            SwitchTarget(
                name=spec.name,
                config_path=path,
                entry_key=server_key,
                entry=entry,
                config_format=spec.config_format,
                error=None,
            )
        )

    return discovered, missing


def _print_discovery_results(discovered: list[SwitchTarget], missing: list[tuple[str, Path]]) -> None:
    if discovered:
        table = Table(show_header=True, header_style="bold")
        table.add_column("App")
        table.add_column("Config Path")
        table.add_column("Status")
        table.add_column("Current")

        for target in discovered:
            if target.error:
                status = "[red]Invalid config[/red]"
                current = "-"
            elif target.entry is None:
                status = "[yellow]Config found, no Skyvern entry[/yellow]"
                current = "-"
            else:
                status = f"[green]{_entry_kind(target.entry)}[/green]"
                location = _entry_location(target.entry)
                current = f"{_mask_key(_extract_entry_api_key(target.entry))} {location}".strip()
            table.add_row(target.name, str(target.config_path), status, current)

        console.print(table)

    if missing:
        missing_names = ", ".join(f"{name} ({path})" for name, path in missing)
        console.print(f"[dim]Config not found: {missing_names}[/dim]")


def _select_targets(discovered: list[SwitchTarget]) -> list[SwitchTarget]:
    selectable = [
        target for target in discovered if target.entry is not None and _entry_kind(target.entry) != "unsupported"
    ]
    if not selectable:
        console.print(
            "[red]No switchable Skyvern MCP entries were found. Add a Skyvern MCP entry for the client first, "
            "then re-run `skyvern mcp switch`.[/red]"
        )
        raise typer.Exit(code=1)

    if len(selectable) == 1:
        return selectable

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("App")
    table.add_column("Config Path")
    table.add_column("Transport")

    for index, target in enumerate(selectable, start=1):
        table.add_row(str(index), target.name, str(target.config_path), _entry_kind(target.entry))

    console.print("\n[bold]Switchable Skyvern configs[/bold]")
    console.print(table)

    raw_choice = _prompt_text(
        "Which apps should use the selected profile? Enter numbers separated by commas or 'all'",
        default="all",
    )
    if raw_choice.strip().lower() == "all":
        return selectable

    chosen_indexes: list[int] = []
    for item in raw_choice.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        try:
            index = int(stripped)
        except ValueError as exc:
            raise typer.BadParameter("Selections must be numbers separated by commas or 'all'.") from exc
        if index < 1 or index > len(selectable):
            raise typer.BadParameter(f"Selection {index} is out of range.")
        if index not in chosen_indexes:
            chosen_indexes.append(index)

    if not chosen_indexes:
        raise typer.BadParameter("Choose at least one app to update.")

    return [selectable[index - 1] for index in chosen_indexes]


def _patch_entry_with_profile(
    entry: dict,
    profile: MCPProfile,
    *,
    config_format: str = _CONFIG_FORMAT_JSON,
) -> dict:
    patched = copy.deepcopy(entry)
    kind = _entry_kind(entry)

    if kind == "local stdio":
        env = dict(patched.get("env") or {})
        env["SKYVERN_API_KEY"] = profile.api_key
        env["SKYVERN_BASE_URL"] = profile.base_url
        patched["env"] = env
        return patched

    if kind == "remote http":
        target_url = _profile_to_mcp_url(profile.base_url)
        if config_format == _CONFIG_FORMAT_CODEX or "http_headers" in patched:
            headers = dict(patched.get("http_headers") or {})
            headers["x-api-key"] = profile.api_key
            patched["http_headers"] = headers
            patched["url"] = target_url
            return patched

        headers = dict(patched.get("headers") or {})
        headers["x-api-key"] = profile.api_key
        patched["headers"] = headers
        if config_format != _CONFIG_FORMAT_YAML:
            patched["type"] = "http"
        patched["url"] = target_url
        return patched

    if kind == "mcp-remote bridge":
        args = list(patched.get("args") or [])
        target_url = _profile_to_mcp_url(profile.base_url)
        if not args or args[0] != "mcp-remote":
            raise ValueError("Unsupported mcp-remote entry format.")

        if len(args) == 1:
            args.append(target_url)
        else:
            args[1] = target_url

        cleaned_args: list[object] = []
        skip_next = False
        for index, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "--header" and index + 1 < len(args):
                next_arg = args[index + 1]
                if isinstance(next_arg, str) and next_arg.startswith("x-api-key:"):
                    skip_next = True
                    continue
            if isinstance(arg, str) and arg.startswith("x-api-key:"):
                continue
            cleaned_args.append(arg)

        cleaned_args.extend(["--header", f"x-api-key:{profile.api_key}"])
        patched["args"] = cleaned_args
        return patched

    raise ValueError(f"Unsupported Skyvern MCP entry format: {kind}")


def _render_patched_entry(target: SwitchTarget, patched: dict) -> Syntax:
    masked = _mask_secrets(patched)
    if target.config_format == _CONFIG_FORMAT_CODEX and target.entry_key:
        snippet = toml.dumps({_server_block_key(target.config_format): {target.entry_key: masked}})
        return Syntax(snippet, "toml")
    if target.config_format == _CONFIG_FORMAT_YAML and target.entry_key:
        snippet = yaml.dump(
            {_server_block_key(target.config_format): {target.entry_key: masked}},
            default_flow_style=False,
            sort_keys=False,
        )
        return Syntax(snippet, "yaml")
    return Syntax(json.dumps(masked, indent=2), "json")


def _apply_profile_to_target(
    target: SwitchTarget,
    profile: MCPProfile,
    *,
    dry_run: bool = False,
) -> tuple[bool, Path | None]:
    if target.entry is None or target.entry_key is None:
        raise ValueError(f"{target.name} does not have a switchable Skyvern MCP entry.")

    config, error = _load_switch_config(target.config_path, target.config_format)
    if error:
        raise ValueError(error)

    existing = config or {}
    server_block = _server_block_key(target.config_format)
    servers = existing.setdefault(server_block, {})
    current = servers.get(target.entry_key)
    if not isinstance(current, dict):
        raise ValueError(f"{target.name} does not have a valid Skyvern MCP entry.")

    patched = _patch_entry_with_profile(current, profile, config_format=target.config_format)
    if patched == current:
        return False, None

    if dry_run:
        console.print(f"\n[bold]{target.name}[/bold] -> {target.config_path}")
        console.print(_render_patched_entry(target, patched))
        return True, None

    servers[target.entry_key] = patched
    backup_path = _write_switch_config(target.config_path, existing, target.config_format)
    return True, backup_path


@profile_app.command("save")
def save_profile_command(
    name: str = typer.Argument(..., help="Profile name, for example 'work-prod'."),
    api_key: str | None = typer.Option(None, "--api-key", "-k", help="Skyvern API key (reads env/.env if omitted)"),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Skyvern API base URL (reads env/.env if omitted, default: https://api.skyvern.com)",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing profile with the same name."),
) -> None:
    env_key, env_base_url = _get_env_credentials()
    resolved_api_key = api_key or env_key
    if not resolved_api_key:
        resolved_api_key = _prompt_text("Skyvern API key", password=True)

    resolved_base_url = base_url or env_base_url or _DEFAULT_BASE_URL
    profile = _build_profile(name, resolved_api_key, resolved_base_url)
    path = _save_profile(profile, overwrite=overwrite)
    console.print(f"[green]Saved MCP profile '{profile.name}' at {path}[/green]")
    console.print(f"[dim]Saved MCP profiles store API keys in plaintext JSON under {path.parent}[/dim]")


@profile_app.command("list")
def list_profiles_command() -> None:
    profiles = _list_profiles()
    if not profiles:
        console.print(f"[yellow]No saved MCP profiles found in {_profile_store_dir()}[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Profile")
    table.add_column("Base URL")
    table.add_column("API Key")

    for profile in profiles:
        table.add_row(profile.name, profile.base_url, _mask_key(profile.api_key))

    console.print(table)
    console.print(f"[dim]Profile store: {_profile_store_dir()}[/dim]")


@mcp_app.command("switch")
def switch_command(
    profile_name: str | None = typer.Option(None, "--profile", help="Saved profile name to use without prompting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview updates without writing files."),
) -> None:
    console.print(
        Panel(
            "[bold]Skyvern MCP Switcher[/bold]\n\n"
            "This command finds supported local MCP configs, lets you choose which apps to update, "
            "then swaps in a Skyvern API key/base URL from env, saved profiles, existing configs, or manual entry.",
            border_style="blue",
        )
    )

    discovered, missing = _discover_switch_targets()
    if not discovered:
        console.print(
            "[red]No supported MCP client config files were found for Claude Code, Claude Desktop, Cursor, "
            "Windsurf, or Codex.[/red]"
        )
        raise typer.Exit(code=1)

    _print_discovery_results(discovered, missing)

    selected_targets = _select_targets(discovered)
    profile = _select_profile(profile_name, discovered)

    console.print(
        f"\n[bold]Selected source:[/bold] {profile.name}\n"
        f"[dim]API key:[/dim] {_mask_key(profile.api_key)}\n"
        f"[dim]Base URL:[/dim] {profile.base_url}\n"
        f"[dim]Remote MCP URL:[/dim] {_profile_to_mcp_url(profile.base_url)}\n"
        "[dim]Local stdio configs keep the base URL in SKYVERN_BASE_URL.[/dim]"
    )

    target_names = ", ".join(target.name for target in selected_targets)
    if not dry_run and not Confirm.ask(f"Update {target_names} to use '{profile.name}'?", default=True):
        raise typer.Abort()

    updated: list[str] = []
    unchanged: list[str] = []
    backups: list[Path] = []
    for target in selected_targets:
        changed, backup_path = _apply_profile_to_target(target, profile, dry_run=dry_run)
        if changed:
            updated.append(target.name)
        else:
            unchanged.append(target.name)
        if backup_path is not None:
            backups.append(backup_path)

    if dry_run:
        console.print("\n[yellow]Dry run only. No config files were modified.[/yellow]")
        return

    if updated:
        console.print(f"\n[green]Updated:[/green] {', '.join(updated)}")
    if unchanged:
        console.print(f"[dim]Already using that profile: {', '.join(unchanged)}[/dim]")
    if backups:
        console.print("[dim]Backups created:[/dim]")
        for backup in backups:
            console.print(f"[dim]- {backup}[/dim]")

    console.print(
        "\n[bold yellow]Restart Claude Code, Claude Desktop, Cursor, Windsurf, or Codex. "
        "If you updated a project `.mcp.json`, reopen that project too.[/bold yellow]"
    )
