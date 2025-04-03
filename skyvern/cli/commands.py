import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from dotenv import load_dotenv, set_key
from mcp.server.fastmcp import FastMCP

from skyvern.agent import SkyvernAgent
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.utils import detect_os, get_windows_appdata_roaming, migrate_db

mcp = FastMCP("Skyvern")


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> str:
    """Browse the internet using a browser to achieve a user goal.

    Args:
        prompt: brief description of what the user wants to accomplish
        url: the target website for the user goal
    """
    skyvern_agent = SkyvernAgent(
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
        extra_headers={"X-User-Agent": "skyvern-mcp"},
    )
    res = await skyvern_agent.run_task(prompt=prompt, url=url)
    return res.model_dump()["output"]


load_dotenv()

cli_app = typer.Typer()
run_app = typer.Typer()
cli_app.add_typer(run_app, name="run")


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


def update_or_add_env_var(key: str, value: str) -> None:
    """Update or add environment variable in .env file."""

    env_path = Path(".env")
    if not env_path.exists():
        env_path.touch()
        # Write default environment variables using dotenv
        defaults = {
            "ENV": "local",
            "ENABLE_OPENAI": "false",
            "OPENAI_API_KEY": "",
            "ENABLE_ANTHROPIC": "false",
            "ANTHROPIC_API_KEY": "",
            "ENABLE_AZURE": "false",
            "AZURE_DEPLOYMENT": "",
            "AZURE_API_KEY": "",
            "AZURE_API_BASE": "",
            "AZURE_API_VERSION": "",
            "ENABLE_AZURE_GPT4O_MINI": "false",
            "AZURE_GPT4O_MINI_DEPLOYMENT": "",
            "AZURE_GPT4O_MINI_API_KEY": "",
            "AZURE_GPT4O_MINI_API_BASE": "",
            "AZURE_GPT4O_MINI_API_VERSION": "",
            "ENABLE_GEMINI": "false",
            "GEMINI_API_KEY": "",
            "ENABLE_NOVITA": "false",
            "NOVITA_API_KEY": "",
            "LLM_KEY": "",
            "SECONDARY_LLM_KEY": "",
            "BROWSER_TYPE": "chromium-headful",
            "MAX_SCRAPING_RETRIES": "0",
            "VIDEO_PATH": "./videos",
            "BROWSER_ACTION_TIMEOUT_MS": "5000",
            "MAX_STEPS_PER_RUN": "50",
            "LOG_LEVEL": "INFO",
            "DATABASE_STRING": "postgresql+psycopg://skyvern@localhost/skyvern",
            "PORT": "8000",
            "ANALYTICS_ID": "anonymous",
            "ENABLE_LOG_ARTIFACTS": "false",
        }
        for key, value in defaults.items():
            set_key(env_path, key, value)

    load_dotenv(env_path)
    set_key(env_path, key, value)


def setup_llm_providers() -> None:
    """Configure Large Language Model (LLM) Providers."""
    print("Configuring Large Language Model (LLM) Providers...")
    print("Note: All information provided here will be stored only on your local machine.")
    model_options = []

    # OpenAI Configuration
    print("To enable OpenAI, you must have an OpenAI API key.")
    enable_openai = input("Do you want to enable OpenAI (y/n)? ").lower() == "y"
    if enable_openai:
        openai_api_key = input("Enter your OpenAI API key: ")
        if not openai_api_key:
            print("Error: OpenAI API key is required.")
            print("OpenAI will not be enabled.")
        else:
            update_or_add_env_var("OPENAI_API_KEY", openai_api_key)
            update_or_add_env_var("ENABLE_OPENAI", "true")
            model_options.extend(["OPENAI_GPT4O"])
    else:
        update_or_add_env_var("ENABLE_OPENAI", "false")

    # Anthropic Configuration
    print("To enable Anthropic, you must have an Anthropic API key.")
    enable_anthropic = input("Do you want to enable Anthropic (y/n)? ").lower() == "y"
    if enable_anthropic:
        anthropic_api_key = input("Enter your Anthropic API key: ")
        if not anthropic_api_key:
            print("Error: Anthropic API key is required.")
            print("Anthropic will not be enabled.")
        else:
            update_or_add_env_var("ANTHROPIC_API_KEY", anthropic_api_key)
            update_or_add_env_var("ENABLE_ANTHROPIC", "true")
            model_options.extend(
                [
                    "ANTHROPIC_CLAUDE3_OPUS",
                    "ANTHROPIC_CLAUDE3_HAIKU",
                    "ANTHROPIC_CLAUDE3.5_SONNET",
                    "ANTHROPIC_CLAUDE3.7_SONNET",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_ANTHROPIC", "false")

    # Azure Configuration
    print("To enable Azure, you must have an Azure deployment name, API key, base URL, and API version.")
    enable_azure = input("Do you want to enable Azure (y/n)? ").lower() == "y"
    if enable_azure:
        azure_deployment = input("Enter your Azure deployment name: ")
        azure_api_key = input("Enter your Azure API key: ")
        azure_api_base = input("Enter your Azure API base URL: ")
        azure_api_version = input("Enter your Azure API version: ")
        if not all([azure_deployment, azure_api_key, azure_api_base, azure_api_version]):
            print("Error: All Azure fields must be populated.")
            print("Azure will not be enabled.")
        else:
            update_or_add_env_var("AZURE_DEPLOYMENT", azure_deployment)
            update_or_add_env_var("AZURE_API_KEY", azure_api_key)
            update_or_add_env_var("AZURE_API_BASE", azure_api_base)
            update_or_add_env_var("AZURE_API_VERSION", azure_api_version)
            update_or_add_env_var("ENABLE_AZURE", "true")
            model_options.append("AZURE_OPENAI_GPT4O")
    else:
        update_or_add_env_var("ENABLE_AZURE", "false")

    # Gemini Configuration
    print("To enable Gemini, you must have an Gemini API key.")
    enable_gemini = input("Do you want to enable Gemini (y/n)? ").lower() == "y"
    if enable_gemini:
        gemini_api_key = input("Enter your Gemini API key: ")
        if not gemini_api_key:
            print("Error: Gemini API key is required.")
            print("Gemini will not be enabled.")
        else:
            update_or_add_env_var("GEMINI_API_KEY", gemini_api_key)
            update_or_add_env_var("ENABLE_GEMINI", "true")
            model_options.extend(["GEMINI_FLASH_2_0", "GEMINI_FLASH_2_0_LITE", "GEMINI_PRO"])
    else:
        update_or_add_env_var("ENABLE_GEMINI", "false")

    # Novita AI Configuration
    print("To enable Novita AI, you must have an Novita AI API key.")
    enable_novita = input("Do you want to enable Novita AI (y/n)? ").lower() == "y"
    if enable_novita:
        novita_api_key = input("Enter your Novita AI API key: ")
        if not novita_api_key:
            print("Error: Novita AI API key is required.")
            print("Novita AI will not be enabled.")
        else:
            update_or_add_env_var("NOVITA_API_KEY", novita_api_key)
            update_or_add_env_var("ENABLE_NOVITA", "true")
            model_options.extend(
                [
                    "NOVITA_DEEPSEEK_R1",
                    "NOVITA_DEEPSEEK_V3",
                    "NOVITA_LLAMA_3_3_70B",
                    "NOVITA_LLAMA_3_2_1B",
                    "NOVITA_LLAMA_3_2_3B",
                    "NOVITA_LLAMA_3_2_11B_VISION",
                    "NOVITA_LLAMA_3_1_8B",
                    "NOVITA_LLAMA_3_1_70B",
                    "NOVITA_LLAMA_3_1_405B",
                    "NOVITA_LLAMA_3_8B",
                    "NOVITA_LLAMA_3_70B",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_NOVITA", "false")

    # Model Selection
    if not model_options:
        print(
            "No LLM providers enabled. You won't be able to run Skyvern unless you enable at least one provider. You can re-run this script to enable providers or manually update the .env file."
        )
    else:
        print("Available LLM models based on your selections:")
        for i, model in enumerate(model_options, 1):
            print(f"{i}. {model}")

        while True:
            try:
                model_choice = int(input(f"Choose a model by number (e.g., 1 for {model_options[0]}): "))
                if 1 <= model_choice <= len(model_options):
                    break
                print(f"Please enter a number between 1 and {len(model_options)}")
            except ValueError:
                print("Please enter a valid number")

        chosen_model = model_options[model_choice - 1]
        print(f"Chosen LLM Model: {chosen_model}")
        update_or_add_env_var("LLM_KEY", chosen_model)

    print("LLM provider configurations updated in .env.")


def get_default_chrome_location(host_system: str) -> str:
    """Get the default Chrome/Chromium location based on OS."""
    if host_system == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif host_system == "linux":
        # Common Linux locations
        chrome_paths = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
        for path in chrome_paths:
            if os.path.exists(path):
                return path
        return "/usr/bin/google-chrome"  # default if not found
    elif host_system == "wsl":
        return "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
    else:
        return "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"


def setup_browser_config() -> tuple[str, Optional[str], Optional[str]]:
    """Configure browser settings for Skyvern."""
    print("\nConfiguring web browser for scraping...")
    browser_types = ["chromium-headless", "chromium-headful", "cdp-connect"]

    for i, browser_type in enumerate(browser_types, 1):
        print(f"{i}. {browser_type}")
        if browser_type == "chromium-headless":
            print("   - Runs Chrome in headless mode (no visible window)")
        elif browser_type == "chromium-headful":
            print("   - Runs Chrome with visible window")
        elif browser_type == "cdp-connect":
            print("   - Connects to an existing Chrome instance")
            print("   - Requires Chrome to be running with remote debugging enabled")

    while True:
        try:
            choice = int(input("\nChoose browser type (1-3): "))
            if 1 <= choice <= len(browser_types):
                selected_browser = browser_types[choice - 1]
                break
            print(f"Please enter a number between 1 and {len(browser_types)}")
        except ValueError:
            print("Please enter a valid number")

    browser_location = None
    remote_debugging_url = None

    if selected_browser == "cdp-connect":
        host_system = detect_os()
        default_location = get_default_chrome_location(host_system)
        print(f"\nDefault Chrome location for your system: {default_location}")
        browser_location = input("Enter Chrome executable location (press Enter to use default): ").strip()
        if not browser_location:
            browser_location = default_location

        if not os.path.exists(browser_location):
            print(f"Warning: Chrome not found at {browser_location}. Please verify the location is correct.")

        print("\nTo use CDP connection, Chrome must be running with remote debugging enabled.")
        print("Example: chrome --remote-debugging-port=9222")
        print("Default debugging URL: http://localhost:9222")
        remote_debugging_url = input("Enter remote debugging URL (press Enter for default): ").strip()
        if not remote_debugging_url:
            remote_debugging_url = "http://localhost:9222"

    return selected_browser, browser_location, remote_debugging_url


async def _setup_local_organization() -> str:
    """
    Returns the API key for the local organization generated
    """
    skyvern_agent = SkyvernAgent(
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
    )
    organization = await skyvern_agent.get_organization()

    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
    )
    return org_auth_token.token if org_auth_token else ""


@cli_app.command(name="init")
def init() -> None:
    run_local_str = (
        input("Would you like to run Skyvern locally or in the cloud? (local/cloud) [cloud]: ").strip().lower()
    )
    run_local = run_local_str == "local" if run_local_str else False

    if run_local:
        setup_postgresql()
        api_key = asyncio.run(_setup_local_organization())

        if os.path.exists(".env"):
            print(".env file already exists, skipping initialization.")
            redo_llm_setup = input("Do you want to go through LLM provider setup again (y/n)? ")
            if redo_llm_setup.lower() != "y":
                return

        print("Initializing .env file...")
        setup_llm_providers()

        # Configure browser settings
        browser_type, browser_location, remote_debugging_url = setup_browser_config()
        update_or_add_env_var("BROWSER_TYPE", browser_type)
        if browser_location:
            update_or_add_env_var("CHROME_EXECUTABLE_PATH", browser_location)
        if remote_debugging_url:
            update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)

        print("Defaulting Skyvern Base URL to: http://localhost:8000")
        update_or_add_env_var("SKYVERN_BASE_URL", "http://localhost:8000")

    else:
        base_url = input("Enter Skyvern base URL (press Enter for api.skyvern.com): ").strip()
        if not base_url:
            base_url = "https://api.skyvern.com"

        print("To get your API key:")
        print("1. Create an account at app.skyvern.com")
        print("2. Go to Settings")
        print("3. Copy your API key")
        api_key = input("Enter your Skyvern API key: ").strip()
        if not api_key:
            print("API key is required")
            api_key = input("Enter your Skyvern API key: ").strip()

        update_or_add_env_var("SKYVERN_BASE_URL", base_url)

    # Ask for email or generate UUID
    analytics_id = input("Please enter your email for analytics (press enter to skip): ")
    if not analytics_id:
        analytics_id = str(uuid.uuid4())

    update_or_add_env_var("ANALYTICS_ID", analytics_id)
    update_or_add_env_var("SKYVERN_API_KEY", api_key)
    print(".env file has been initialized.")

    # Ask if user wants to configure MCP server
    configure_mcp = input("\nWould you like to configure the MCP server (y/n)? ").lower() == "y"
    if configure_mcp:
        setup_mcp()
        print("\nMCP server configuration completed.")


@cli_app.command(name="migrate")
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


def is_windsurf_installed(host_system: str) -> bool:
    """Check if Windsurf is installed by looking for its config directory."""
    try:
        config_dir = os.path.expanduser("~/.codeium/windsurf")
        return os.path.exists(config_dir)
    except Exception:
        return False


def get_windsurf_config_path(host_system: str) -> str:
    """Get the Windsurf config file path for the current OS."""
    return os.path.expanduser("~/.codeium/windsurf/mcp_config.json")


def setup_windsurf_config(host_system: str, path_to_env: str) -> None:
    """Set up Windsurf configuration for Skyvern MCP."""
    if not is_windsurf_installed(host_system):
        return

    load_dotenv()
    skyvern_base_url = os.getenv("SKYVERN_BASE_URL", "")
    skyvern_api_key = os.getenv("SKYVERN_API_KEY", "")
    if not skyvern_base_url or not skyvern_api_key:
        print(
            "Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Windsurf MCP. Please open {path_windsurf_config} and set the these variables manually."
        )

    path_windsurf_config = get_windsurf_config_path(host_system)
    os.makedirs(os.path.dirname(path_windsurf_config), exist_ok=True)
    config = {
        "Skyvern": {
            "env": {"SKYVERN_BASE_URL": skyvern_base_url, "SKYVERN_API_KEY": skyvern_api_key},
            "command": path_to_env,
            "args": ["-m", "skyvern", "run", "mcp"],
        }
    }
    if os.path.exists(path_windsurf_config):
        try:
            with open(path_windsurf_config, "r") as f:
                existing_config = json.load(f)
                existing_config.update(config)
                config = existing_config
        except json.JSONDecodeError:
            pass
    with open(path_windsurf_config, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Windsurf configuration updated successfully at {path_windsurf_config}.")


def setup_mcp_config() -> str:
    """
    return the path to the python environment
    """
    python_path = shutil.which("python")
    if python_path:
        use_default = typer.prompt(f"Found Python at {python_path}. Use this path? (y/n)").lower() == "y"
        if use_default:
            path_to_env = python_path
        else:
            path_to_env = typer.prompt("Enter the full path to your configured python environment")
    return path_to_env


def setup_mcp() -> None:
    """Configure MCP for different Skyvern deployments."""
    host_system = detect_os()

    path_to_env = setup_mcp_config()

    # Configure both Claude Desktop and Cursor

    setup_claude_desktop_config(host_system, path_to_env)
    setup_cursor_config(host_system, path_to_env)
    setup_windsurf_config(host_system, path_to_env)


def setup_claude_desktop_config(host_system: str, path_to_env: str) -> bool:
    """Set up Claude Desktop configuration with given command and args."""
    if not is_claude_desktop_installed(host_system):
        return False

    try:
        path_claude_config = get_claude_config_path(host_system)

        os.makedirs(os.path.dirname(path_claude_config), exist_ok=True)
        if not os.path.exists(path_claude_config):
            with open(path_claude_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        # Read environment variables from .env file
        load_dotenv()
        skyvern_base_url = os.getenv("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.getenv("SKYVERN_API_KEY", "")

        if not skyvern_base_url or not skyvern_api_key:
            print("Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file")

        with open(path_claude_config, "r") as f:
            claude_config = json.load(f)
            claude_config["mcpServers"].pop("Skyvern", None)
            claude_config["mcpServers"]["Skyvern"] = {
                "env": {
                    "SKYVERN_BASE_URL": skyvern_base_url,
                    "SKYVERN_API_KEY": skyvern_api_key,
                },
                "command": path_to_env,
                "args": ["-m", "skyvern", "run", "mcp"],
            }

        with open(path_claude_config, "w") as f:
            json.dump(claude_config, f, indent=2)

        print(f"Claude Desktop configuration updated successfully at {path_claude_config}.")
        return True

    except Exception as e:
        print(f"Error configuring Claude Desktop: {e}")
        return False


def setup_cursor_config(host_system: str, path_to_env: str) -> bool:
    """Set up Cursor configuration with given command and args."""
    if not is_cursor_installed(host_system):
        return False

    try:
        path_cursor_config = get_cursor_config_path(host_system)

        os.makedirs(os.path.dirname(path_cursor_config), exist_ok=True)
        load_dotenv()
        skyvern_base_url = os.getenv("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.getenv("SKYVERN_API_KEY", "")

        if not skyvern_base_url or not skyvern_api_key:
            print(
                f"Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Cursor MCP. Please open {path_cursor_config} and set the these variables manually."
            )

        config = {
            "Skyvern": {
                "env": {
                    "SKYVERN_BASE_URL": skyvern_base_url,
                    "SKYVERN_API_KEY": skyvern_api_key,
                },
                "command": path_to_env,
                "args": ["-m", "skyvern", "run", "mcp"],
            }
        }
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
    load_dotenv()
    from skyvern.config import settings

    port = settings.PORT
    browser_type = settings.BROWSER_TYPE
    browser_path = settings.CHROME_EXECUTABLE_PATH

    if browser_type == "cdp-connect" and browser_path:
        browser_process = subprocess.Popen(
            [browser_path, "--remote-debugging-port=9222"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if browser_process.poll() is not None:
            raise Exception(f"Failed to open browser. browser_path: {browser_path}")

    uvicorn.run(
        "skyvern.forge.api_app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")
