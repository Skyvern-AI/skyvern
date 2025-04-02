import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Optional

import typer
import uvicorn
from click import Choice
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from skyvern.agent import SkyvernAgent
from skyvern.config import settings
from skyvern.schemas.runs import RunEngine
from skyvern.utils import detect_os, get_windows_appdata_roaming, migrate_db

mcp = FastMCP("Skyvern")
skyvern_agent = SkyvernAgent(
    base_url=settings.SKYVERN_BASE_URL,
    api_key=settings.SKYVERN_API_KEY,
)


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> str:
    """Browse the internet using a browser to achieve a user goal.

    Args:
        prompt: brief description of what the user wants to accomplish
        url: the target website for the user goal
    """
    res = await skyvern_agent.run_task(prompt=prompt, url=url, engine=RunEngine.skyvern_v1)
    return res.model_dump()["output"]


load_dotenv()

app = typer.Typer()
run_app = typer.Typer()
app.add_typer(run_app, name="run")


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_command(command: str, check: bool = True) -> tuple[Optional[str], Optional[int]]:
    try:
        result = subprocess.run(command, shell=True, check=check, capture_output=True, text=True)
        return result.stdout.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        return None, e.returncode


def is_postgres_running() -> bool:
    if command_exists("pg_isready"):
        result, _ = run_command("pg_isready")
        return result is not None and "accepting connections" in result
    return False


def database_exists(dbname: str, user: str) -> bool:
    check_db_command = f'psql {dbname} -U {user} -c "\\q"'
    output, _ = run_command(check_db_command, check=False)
    return output is not None


def create_database_and_user() -> None:
    print("Creating database user and database...")
    run_command("createuser skyvern")
    run_command("createdb skyvern -O skyvern")
    print("Database and user created successfully.")


def is_docker_running() -> bool:
    if not command_exists("docker"):
        return False
    _, code = run_command("docker info", check=False)
    return code == 0


def is_postgres_running_in_docker() -> bool:
    _, code = run_command("docker ps | grep -q postgresql-container", check=False)
    return code == 0


def is_postgres_container_exists() -> bool:
    _, code = run_command("docker ps -a | grep -q postgresql-container", check=False)
    return code == 0


def setup_postgresql() -> None:
    print("Setting up PostgreSQL...")

    if command_exists("psql") and is_postgres_running():
        print("PostgreSQL is already running locally.")
        if database_exists("skyvern", "skyvern"):
            print("Database and user exist.")
        else:
            create_database_and_user()
        return

    if not is_docker_running():
        print("Docker is not running or not installed. Please install or start Docker and try again.")
        exit(1)

    if is_postgres_running_in_docker():
        print("PostgreSQL is already running in a Docker container.")
    else:
        print("Attempting to install PostgreSQL via Docker...")
        if not is_postgres_container_exists():
            run_command(
                "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
            )
        else:
            run_command("docker start postgresql-container")
        print("PostgreSQL has been installed and started using Docker.")

        print("Waiting for PostgreSQL to start...")
        time.sleep(20)

    _, code = run_command('docker exec postgresql-container psql -U postgres -c "\\du" | grep -q skyvern', check=False)
    if code == 0:
        print("Database user exists.")
    else:
        print("Creating database user...")
        run_command("docker exec postgresql-container createuser -U postgres skyvern")

    _, code = run_command(
        "docker exec postgresql-container psql -U postgres -lqt | cut -d \\| -f 1 | grep -qw skyvern", check=False
    )
    if code == 0:
        print("Database exists.")
    else:
        print("Creating database...")
        run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
        print("Database and user created successfully.")


async def _setup_local_organization() -> str:
    """
    Returns the API key for the local organization generated
    """
    from skyvern.forge import app
    from skyvern.forge.sdk.core import security
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME

    organization = await app.DATABASE.get_organization_by_domain("skyvern.local")
    if not organization:
        organization = await app.DATABASE.create_organization(
            organization_name="Skyvern-local",
            domain="skyvern.local",
            max_steps_per_run=10,
            max_retries_per_step=3,
        )
        api_key = security.create_access_token(
            organization.organization_id,
            expires_delta=API_KEY_LIFETIME,
        )
        # generate OrganizationAutoToken
        await app.DATABASE.create_org_auth_token(
            organization_id=organization.organization_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
    )
    return org_auth_token.token


@app.command(name="init")
def init(
    openai_api_key: str = typer.Option(..., help="The OpenAI API key"),
    log_level: str = typer.Option("INFO", help="The log level"),
) -> None:
    setup_postgresql()
    api_key = asyncio.run(_setup_local_organization())
    # Generate .env file
    with open(".env", "w") as env_file:
        env_file.write("ENABLE_OPENAI=true\n")
        env_file.write(f"OPENAI_API_KEY={openai_api_key}\n")
        env_file.write(f"LOG_LEVEL={log_level}\n")
        env_file.write("ARTIFACT_STORAGE_PATH=./artifacts\n")
        env_file.write(f"SKYVERN_API_KEY={api_key}\n")
    print(".env file created with the parameters provided.")


@app.command(name="migrate")
def migrate() -> None:
    migrate_db()


def get_claude_config_path(host_system: str) -> str:
    """Get the Claude Desktop config file path for the current OS."""
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
        base_path = os.path.expanduser(base_paths["darwin"][0])
        return os.path.join(base_path, "claude_desktop_config.json")

    if host_system == "linux":
        for path in base_paths["linux"]:
            full_path = os.path.expanduser(path)
            if os.path.exists(full_path):
                return os.path.join(full_path, "claude_desktop_config.json")

    raise Exception(f"Unsupported host system: {host_system}")


def get_claude_command_config(
    host_system: str, path_to_env: str, path_to_server: str, env_vars: str
) -> tuple[str, list]:
    """Get the command and arguments for Claude Desktop configuration."""
    base_env_vars = f"{env_vars} ENABLE_OPENAI=true LOG_LEVEL=CRITICAL"
    artifacts_path = os.path.join(os.path.abspath("./"), "artifacts")

    if host_system == "wsl":
        env_vars = f"{base_env_vars} ARTIFACT_STORAGE_PATH={artifacts_path} BROWSER_TYPE=chromium-headless"
        return "wsl.exe", ["bash", "-c", f"{env_vars} {path_to_env} {path_to_server}"]

    if host_system in ["linux", "darwin"]:
        env_vars = f"{base_env_vars} ARTIFACT_STORAGE_PATH={artifacts_path}"
        return path_to_env, [path_to_server]

    raise Exception(f"Unsupported host system: {host_system}")


def is_claude_desktop_installed(host_system: str) -> bool:
    """Check if Claude Desktop is installed by looking for its config directory."""
    try:
        config_path = os.path.dirname(get_claude_config_path(host_system))
        return os.path.exists(config_path)
    except Exception:
        return False


def get_cursor_config_path(host_system: str) -> str:
    """Get the Cursor config file path for the current OS."""
    if host_system == "wsl":
        roaming_path = get_windows_appdata_roaming()
        if roaming_path is None:
            raise RuntimeError("Could not locate Windows AppData\\Roaming path from WSL")
        return os.path.join(str(roaming_path), ".cursor", "mcp.json")

    # For both darwin and linux, use ~/.cursor/mcp.json
    return os.path.expanduser("~/.cursor/mcp.json")


def is_cursor_installed(host_system: str) -> bool:
    """Check if Cursor is installed by looking for its config directory."""
    try:
        config_dir = os.path.expanduser("~/.cursor")
        return os.path.exists(config_dir)
    except Exception:
        return False


def setup_cursor_mcp(host_system: str, path_to_env: str, path_to_server: str, env_vars: str) -> None:
    """Set up Cursor MCP configuration."""
    if not is_cursor_installed(host_system):
        print("Cursor is not installed. Skipping Cursor MCP setup.")
        return

    try:
        path_cursor_config = get_cursor_config_path(host_system)
    except Exception as e:
        print(f"Error setting up Cursor: {e}")
        return

    # Get command configuration
    try:
        command, args = get_claude_command_config(host_system, path_to_env, path_to_server, env_vars)
    except Exception as e:
        print(f"Error configuring Cursor command: {e}")
        return

    # Create or update Cursor config file
    os.makedirs(os.path.dirname(path_cursor_config), exist_ok=True)
    config = {"Skyvern": {"command": command, "args": args}}

    if os.path.exists(path_cursor_config):
        try:
            with open(path_cursor_config, "r") as f:
                existing_config = json.load(f)
                existing_config.update(config)
                config = existing_config
        except json.JSONDecodeError:
            pass  # Use default config if file is corrupted

    with open(path_cursor_config, "w") as f:
        json.dump(config, f, indent=2)

    print("Cursor MCP configuration updated successfully.")


def setup_claude_desktop(host_system: str, path_to_env: str, path_to_server: str) -> None:
    """Set up Claude Desktop configuration for Skyvern MCP."""
    if not is_claude_desktop_installed(host_system):
        print("Claude Desktop is not installed. Skipping MCP setup.")
        return

    # Get config file path
    try:
        path_claude_config = get_claude_config_path(host_system)
    except Exception as e:
        print(f"Error setting up Claude Desktop: {e}")
        return

    # Setup environment variables
    env_vars = ""
    for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        value = os.getenv(key)
        if value is None:
            value = typer.prompt(f"Enter your {key}")
        env_vars += f"{key}={value} "

    # Get command configuration
    try:
        claude_command, claude_args = get_claude_command_config(host_system, path_to_env, path_to_server, env_vars)
    except Exception as e:
        print(f"Error configuring Claude Desktop command: {e}")
        return

    # Create or update Claude config file
    os.makedirs(os.path.dirname(path_claude_config), exist_ok=True)
    if not os.path.exists(path_claude_config):
        with open(path_claude_config, "w") as f:
            json.dump({"mcpServers": {}}, f, indent=2)

    with open(path_claude_config, "r") as f:
        claude_config = json.load(f)
        claude_config["mcpServers"].pop("Skyvern", None)
        claude_config["mcpServers"]["Skyvern"] = {"command": claude_command, "args": claude_args}

    with open(path_claude_config, "w") as f:
        json.dump(claude_config, f, indent=2)

    print("Claude Desktop configuration updated successfully.")


def get_mcp_server_url(deployment_type: str, host: str = "") -> str:
    """Get the MCP server URL based on deployment type."""
    if deployment_type in ["local", "cloud"]:
        return os.path.join(os.path.abspath("./skyvern/mcp"), "server.py")
    else:
        raise ValueError(f"Invalid deployment type: {deployment_type}")


def setup_mcp_config(host_system: str, deployment_type: str, host: str = "") -> tuple[str, str]:
    """Set up MCP configuration based on deployment type."""
    if deployment_type in ["local", "cloud"]:
        # For local deployment, we need Python environment
        python_path = shutil.which("python")
        if python_path:
            path_to_env = python_path
        else:
            path_to_env = typer.prompt("Enter the full path to your configured python environment")
        return path_to_env, get_mcp_server_url(deployment_type)
    else:
        raise NotImplementedError()


def get_command_config(host_system: str, command: str, target: str, env_vars: str) -> tuple[str, list]:
    """Get the command and arguments for MCP configuration."""
    base_env_vars = f"{env_vars} ENABLE_OPENAI=true LOG_LEVEL=CRITICAL"
    artifacts_path = os.path.join(os.path.abspath("./"), "artifacts")

    if host_system == "wsl":
        env_vars = f"{base_env_vars} ARTIFACT_STORAGE_PATH={artifacts_path} BROWSER_TYPE=chromium-headless"
        return "wsl.exe", ["bash", "-c", f"{env_vars} {command} {target}"]

    if host_system in ["linux", "darwin"]:
        env_vars = f"{base_env_vars} ARTIFACT_STORAGE_PATH={artifacts_path}"
        if target.startswith("http"):
            return command, ["-X", "POST", target]
        return command, [target]

    raise Exception(f"Unsupported host system: {host_system}")


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Configure MCP for different Skyvern deployments."""
    host_system = detect_os()

    # Prompt for deployment type
    deployment_types = ["local", "cloud"]
    deployment_type = typer.prompt("Select Skyvern deployment type", type=Choice(deployment_types), default="local")

    try:
        command, target = setup_mcp_config(host_system, deployment_type)
    except Exception as e:
        print(f"Error setting up MCP configuration: {e}")
        return

    # Cloud deployment variables
    env_vars = ""
    if deployment_type == "cloud":
        for key in ["SKYVERN_MCP_CLOUD_URL", "SKYVERN_MCP_API_KEY"]:
            value = os.getenv(key)
            if value is None:
                value = typer.prompt(f"Enter your {key}")
            env_vars += f"{key}={value} "

    # Setup environment variables
    for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        value = os.getenv(key)
        if value is None:
            value = typer.prompt(f"Enter your {key}")
        env_vars += f"{key}={value} "

    # Configure both Claude Desktop and Cursor
    success = False
    success |= setup_claude_desktop_config(host_system, command, target, env_vars)
    success |= setup_cursor_config(host_system, command, target, env_vars)

    if not success:
        print("Neither Claude Desktop nor Cursor is installed. Please install at least one of them.")


def setup_claude_desktop_config(host_system: str, command: str, target: str, env_vars: str) -> bool:
    """Set up Claude Desktop configuration with given command and args."""
    if not is_claude_desktop_installed(host_system):
        return False

    try:
        claude_command, claude_args = get_command_config(host_system, command, target, env_vars)
        path_claude_config = get_claude_config_path(host_system)

        os.makedirs(os.path.dirname(path_claude_config), exist_ok=True)
        if not os.path.exists(path_claude_config):
            with open(path_claude_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        with open(path_claude_config, "r") as f:
            claude_config = json.load(f)
            claude_config["mcpServers"].pop("Skyvern", None)
            claude_config["mcpServers"]["Skyvern"] = {"command": claude_command, "args": claude_args}

        with open(path_claude_config, "w") as f:
            json.dump(claude_config, f, indent=2)

        print("Claude Desktop configuration updated successfully.")
        return True

    except Exception as e:
        print(f"Error configuring Claude Desktop: {e}")
        return False


def setup_cursor_config(host_system: str, command: str, target: str, env_vars: str) -> bool:
    """Set up Cursor configuration with given command and args."""
    if not is_cursor_installed(host_system):
        return False

    try:
        cursor_command, cursor_args = get_command_config(host_system, command, target, env_vars)
        path_cursor_config = get_cursor_config_path(host_system)

        os.makedirs(os.path.dirname(path_cursor_config), exist_ok=True)
        config = {"Skyvern": {"command": cursor_command, "args": cursor_args}}

        if os.path.exists(path_cursor_config):
            try:
                with open(path_cursor_config, "r") as f:
                    existing_config = json.load(f)
                    existing_config.update(config)
                    config = existing_config
            except json.JSONDecodeError:
                pass

        with open(path_cursor_config, "w") as f:
            json.dump(config, f, indent=2)

        print(f"Cursor configuration updated successfully at {path_cursor_config}")
        return True

    except Exception as e:
        print(f"Error configuring Cursor: {e}")
        return False


@run_app.command(name="server")
def run_server() -> None:
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "skyvern.forge.api_app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
