import json
import os
import shutil

from dotenv import load_dotenv
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.library import Skyvern
from skyvern.utils import detect_os, get_windows_appdata_roaming

from .console import console


async def setup_local_organization() -> str:
    skyvern_agent = Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)
    organization = await skyvern_agent.get_organization()
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
    )
    return org_auth_token.token if org_auth_token else ""


# ----- Helper paths and checks -----


def get_claude_config_path(host_system: str) -> str:
    if host_system == "wsl":
        roaming_path = get_windows_appdata_roaming()
        if roaming_path is None:
            raise RuntimeError("Could not locate Windows AppData\\Roaming path from WSL")
        return os.path.join(str(roaming_path), "Claude", "claude_desktop_config.json")
    base_paths = {
        "darwin": ["~/Library/Application Support/Claude"],
        "linux": ["~/.config/Claude", "~/.local/share/Claude", "~/Claude"],
    }
    if host_system == "darwin":
        return os.path.join(os.path.expanduser(base_paths["darwin"][0]), "claude_desktop_config.json")
    if host_system == "linux":
        for path in base_paths["linux"]:
            full = os.path.expanduser(path)
            if os.path.exists(full):
                return os.path.join(full, "claude_desktop_config.json")
    raise Exception(f"Unsupported host system: {host_system}")


def get_cursor_config_path(host_system: str) -> str:
    if host_system == "wsl":
        roaming_path = get_windows_appdata_roaming()
        if roaming_path is None:
            raise RuntimeError("Could not locate Windows AppData\\Roaming path from WSL")
        return os.path.join(str(roaming_path), ".cursor", "mcp.json")
    return os.path.expanduser("~/.cursor/mcp.json")


def get_windsurf_config_path(host_system: str) -> str:
    return os.path.expanduser("~/.codeium/windsurf/mcp_config.json")


# ----- Setup Helpers -----


def setup_mcp_config() -> str:
    console.print(Panel("[bold yellow]Setting up MCP Python Environment[/bold yellow]", border_style="yellow"))
    python_paths: list[tuple[str, str]] = []
    for python_cmd in ["python", "python3.11"]:
        python_path = shutil.which(python_cmd)
        if python_path:
            python_paths.append((python_cmd, python_path))

    if not python_paths:
        console.print("[red]Error: Could not find any Python installation. Please install Python 3.11 first.[/red]")
        path_to_env = Prompt.ask(
            "Enter the full path to your python 3.11 environment. For example in MacOS if you installed it using Homebrew, it would be [cyan]/opt/homebrew/bin/python3.11[/cyan]"
        )
    else:
        _, default_path = python_paths[0]
        console.print(f"💡 [italic]Detected Python environment:[/italic] [green]{default_path}[/green]")
        path_to_env = default_path
    return path_to_env


def is_cursor_installed(host_system: str) -> bool:
    try:
        config_dir = os.path.expanduser("~/.cursor")
        return os.path.exists(config_dir)
    except Exception:
        return False


def is_claude_desktop_installed(host_system: str) -> bool:
    try:
        config_path = os.path.dirname(get_claude_config_path(host_system))
        return os.path.exists(config_path)
    except Exception:
        return False


def is_windsurf_installed(host_system: str) -> bool:
    try:
        config_dir = os.path.expanduser("~/.codeium/windsurf")
        return os.path.exists(config_dir)
    except Exception:
        return False


def setup_claude_desktop_config(host_system: str, path_to_env: str) -> bool:
    console.print(Panel("[bold blue]Configuring Claude Desktop MCP[/bold blue]", border_style="blue"))
    if not is_claude_desktop_installed(host_system):
        console.print("[yellow]Claude Desktop is not installed. Please install it first.[/yellow]")
        return False

    try:
        path_claude_config = get_claude_config_path(host_system)
        os.makedirs(os.path.dirname(path_claude_config), exist_ok=True)
        if not os.path.exists(path_claude_config):
            with open(path_claude_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        load_dotenv(".env")
        skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")
        if not skyvern_base_url or not skyvern_api_key:
            console.print(
                f"[red]Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Claude MCP. Please open {path_claude_config} and set these variables manually.[/red]"
            )
            return False

        claude_config: dict = {"mcpServers": {}}
        if os.path.exists(path_claude_config):
            try:
                with open(path_claude_config, "r") as f:
                    claude_config = json.load(f)
                    claude_config["mcpServers"].pop("Skyvern", None)
                    claude_config["mcpServers"]["Skyvern"] = {
                        "env": {"SKYVERN_BASE_URL": skyvern_base_url, "SKYVERN_API_KEY": skyvern_api_key},
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                console.print(
                    f"[red]JSONDecodeError encountered while reading the Claude Desktop configuration. Please open {path_claude_config} and fix the JSON config.[/red]"
                )
                return False

        with open(path_claude_config, "w") as f:
            json.dump(claude_config, f, indent=2)

        console.print(
            f"✅ [green]Claude Desktop MCP configuration updated successfully at [link]{path_claude_config}[/link].[/green]"
        )
        return True

    except Exception as e:
        console.print(f"[red]Error configuring Claude Desktop: {e}[/red]")
        return False


def setup_cursor_config(host_system: str, path_to_env: str) -> bool:
    console.print(Panel("[bold blue]Configuring Cursor MCP[/bold blue]", border_style="blue"))
    if not is_cursor_installed(host_system):
        console.print("[yellow]Cursor is not installed. Skipping Cursor MCP setup.[/yellow]")
        return False

    try:
        path_cursor_config = get_cursor_config_path(host_system)
        os.makedirs(os.path.dirname(path_cursor_config), exist_ok=True)
        if not os.path.exists(path_cursor_config):
            with open(path_cursor_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        load_dotenv(".env")
        skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")
        if not skyvern_base_url or not skyvern_api_key:
            console.print(
                f"[red]Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Cursor MCP. Please open [link]{path_cursor_config}[/link] and set these variables manually.[/red]"
            )
            return False

        cursor_config: dict = {"mcpServers": {}}
        if os.path.exists(path_cursor_config):
            try:
                with open(path_cursor_config, "r") as f:
                    cursor_config = json.load(f)
                    cursor_config["mcpServers"].pop("Skyvern", None)
                    cursor_config["mcpServers"]["Skyvern"] = {
                        "env": {"SKYVERN_BASE_URL": skyvern_base_url, "SKYVERN_API_KEY": skyvern_api_key},
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                console.print(
                    f"[red]JSONDecodeError encountered while reading the Cursor configuration. Please open [link]{path_cursor_config}[/link] and fix the JSON config.[/red]"
                )
                return False

        with open(path_cursor_config, "w") as f:
            json.dump(cursor_config, f, indent=2)

        console.print(
            f"✅ [green]Cursor MCP configuration updated successfully at [link]{path_cursor_config}[/link][/green]"
        )
        return True

    except Exception as e:
        console.print(f"[red]Error configuring Cursor: {e}[/red]")
        return False


def setup_windsurf_config(host_system: str, path_to_env: str) -> bool:
    if not is_windsurf_installed(host_system):
        return False

    path_windsurf_config = get_windsurf_config_path(host_system)
    load_dotenv(".env")
    skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
    skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")
    if not skyvern_base_url or not skyvern_api_key:
        console.print(
            f"[red]Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Windsurf MCP. Please open {path_windsurf_config} and set these variables manually.[/red]"
        )

    try:
        os.makedirs(os.path.dirname(path_windsurf_config), exist_ok=True)
        if not os.path.exists(path_windsurf_config):
            with open(path_windsurf_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        windsurf_config: dict = {"mcpServers": {}}
        if os.path.exists(path_windsurf_config):
            try:
                with open(path_windsurf_config, "r") as f:
                    windsurf_config = json.load(f)
                    windsurf_config["mcpServers"].pop("Skyvern", None)
                    windsurf_config["mcpServers"]["Skyvern"] = {
                        "env": {"SKYVERN_BASE_URL": skyvern_base_url, "SKYVERN_API_KEY": skyvern_api_key},
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                console.print(
                    f"[red]JSONDecodeError when reading Error configuring Windsurf. Please open {path_windsurf_config} and fix the json config first.[/red]"
                )
                return False

        with open(path_windsurf_config, "w") as f:
            json.dump(windsurf_config, f, indent=2)
    except Exception as e:
        console.print(f"[red]Error configuring Windsurf: {e}[/red]")
        return False

    console.print(
        f"✅ [green]Windsurf MCP configuration updated successfully at [link]{path_windsurf_config}[/link].[/green]"
    )
    return True


def setup_mcp() -> None:
    console.print(Panel("[bold green]MCP Server Setup[/bold green]", border_style="green"))
    host_system = detect_os()
    path_to_env = setup_mcp_config()

    if Confirm.ask("Would you like to set up MCP integration for Claude Desktop?", default=True):
        setup_claude_desktop_config(host_system, path_to_env)

    if Confirm.ask("Would you like to set up MCP integration for Cursor?", default=True):
        setup_cursor_config(host_system, path_to_env)

    if Confirm.ask("Would you like to set up MCP integration for Windsurf?", default=True):
        setup_windsurf_config(host_system, path_to_env)

    console.print("\n🎉 [bold green]MCP server configuration completed.[/bold green]")
