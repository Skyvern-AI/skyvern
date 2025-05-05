import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import typer
import uvicorn
from dotenv import load_dotenv, set_key
from mcp.server.fastmcp import FastMCP

from skyvern.agent import SkyvernAgent
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.utils import detect_os, get_windows_appdata_roaming, migrate_db

load_dotenv()

cli_app = typer.Typer()
run_app = typer.Typer()
setup_app = typer.Typer()
cli_app.add_typer(run_app, name="run")
cli_app.add_typer(setup_app, name="setup")
mcp = FastMCP("Skyvern")


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> dict[str, str]:
    """Use Skyvern to execute anything in the browser. Useful for accomplishing tasks that require browser automation.

    This tool uses Skyvern's browser automation to navigate websites and perform actions to achieve
    the user's intended outcome. It can handle tasks like form filling, clicking buttons, data extraction,
    and multi-step workflows.

    It can even help you find updated data on the internet if your model information is outdated.

    Args:
        prompt: A natural language description of what needs to be accomplished (e.g. "Book a flight from
               NYC to LA", "Sign up for the newsletter", "Find the price of item X", "Apply to a job")
        url: The starting URL of the website where the task should be performed
    """
    skyvern_agent = SkyvernAgent(
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
        extra_headers={"X-User-Agent": "skyvern-mcp"},
    )
    res = await skyvern_agent.run_task(prompt=prompt, url=url)

    # TODO: It would be nice if we could return the task URL here
    output = res.model_dump()["output"]
    base_url = settings.SKYVERN_BASE_URL
    run_history_url = (
        "https://app.skyvern.com/history" if "skyvern.com" in base_url else "http://localhost:8080/history"
    )
    return {"output": output, "run_history_url": run_history_url}


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


def setup_postgresql(no_postgres: bool = False) -> None:
    """Set up PostgreSQL database for Skyvern.

    This function checks if a PostgreSQL server is running locally or in Docker.
    If no PostgreSQL server is found, it offers to start a Docker container
    running PostgreSQL (unless explicitly opted out).

    Args:
        no_postgres: When True, skips starting a PostgreSQL container even if no
                    local PostgreSQL server is detected. Useful when planning to
                    use Docker Compose, which provides its own PostgreSQL service.
    """

    if command_exists("psql") and is_postgres_running():
        print("PostgreSQL is already running locally.")
        if database_exists("skyvern", "skyvern"):
            print("Database and user exist.")
        else:
            create_database_and_user()
        return

    if no_postgres:
        print("Skipping PostgreSQL container setup as requested.")
        print("If you plan to use Docker Compose, its Postgres service will start automatically.")
        return

    if not is_docker_running():
        print("Docker is not running or not installed. Please install or start Docker and try again.")
        exit(1)

    if is_postgres_running_in_docker():
        print("PostgreSQL is already running in a Docker container.")
    else:
        if not no_postgres:
            start_postgres = (
                input(
                    'No local Postgres detected. Start a disposable container now? (Y/n) [Y]\n[Tip: choose "n" if you plan to run Skyvern via Docker Compose instead of `skyvern run server`] '
                )
                .strip()
                .lower()
            )
            if start_postgres in ["n", "no"]:
                print("Skipping PostgreSQL container setup.")
                print("If you plan to use Docker Compose, its Postgres service will start automatically.")
                return

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
        for k, v in defaults.items():
            set_key(env_path, k, v)

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
            model_options.extend(
                [
                    "OPENAI_GPT4_1",
                    "OPENAI_GPT4_1_MINI",
                    "OPENAI_GPT4_1_NANO",
                    "OPENAI_GPT4O",
                    "OPENAI_O4_MINI",
                    "OPENAI_O3",
                ]
            )
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
            model_options.extend(
                [
                    "GEMINI_FLASH_2_0",
                    "GEMINI_FLASH_2_0_LITE",
                    "GEMINI_2.5_PRO_PREVIEW_03_25",
                    "GEMINI_2.5_PRO_EXP_03_25",
                ]
            )
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

    # OpenAI Compatible Configuration
    print("To enable an OpenAI-compatible provider, you must have a model name, API key, and API base URL.")
    enable_openai_compatible = input("Do you want to enable an OpenAI-compatible provider (y/n)? ").lower() == "y"
    if enable_openai_compatible:
        openai_compatible_model_name = input("Enter the model name (e.g., 'yi-34b', 'mistral-large'): ")
        openai_compatible_api_key = input("Enter your API key: ")
        openai_compatible_api_base = input("Enter the API base URL (e.g., 'https://api.together.xyz/v1'): ")
        openai_compatible_vision = input("Does this model support vision (y/n)? ").lower() == "y"

        if not all([openai_compatible_model_name, openai_compatible_api_key, openai_compatible_api_base]):
            print("Error: All required fields must be populated.")
            print("OpenAI-compatible provider will not be enabled.")
        else:
            update_or_add_env_var("OPENAI_COMPATIBLE_MODEL_NAME", openai_compatible_model_name)
            update_or_add_env_var("OPENAI_COMPATIBLE_API_KEY", openai_compatible_api_key)
            update_or_add_env_var("OPENAI_COMPATIBLE_API_BASE", openai_compatible_api_base)

            # Set vision support
            if openai_compatible_vision:
                update_or_add_env_var("OPENAI_COMPATIBLE_SUPPORTS_VISION", "true")
            else:
                update_or_add_env_var("OPENAI_COMPATIBLE_SUPPORTS_VISION", "false")

            # Optional: Ask for API version
            openai_compatible_api_version = input("Enter API version (optional, press enter to skip): ")
            if openai_compatible_api_version:
                update_or_add_env_var("OPENAI_COMPATIBLE_API_VERSION", openai_compatible_api_version)

            update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "true")
            model_options.append("OPENAI_COMPATIBLE")
    else:
        update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "false")

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

        default_port = "9222"
        if remote_debugging_url is None:
            remote_debugging_url = "http://localhost:9222"
        elif ":" in remote_debugging_url.split("/")[-1]:
            default_port = remote_debugging_url.split(":")[-1].split("/")[0]

        parsed_url = urlparse(remote_debugging_url)
        version_url = f"{parsed_url.scheme}://{parsed_url.netloc}/json/version"

        print(f"\nChecking if Chrome is already running with remote debugging on port {default_port}...")
        try:
            response = requests.get(version_url, timeout=2)
            if response.status_code == 200:
                try:
                    browser_info = response.json()
                    print("Chrome is already running with remote debugging!")
                    if "Browser" in browser_info:
                        print(f"Browser: {browser_info['Browser']}")
                    if "webSocketDebuggerUrl" in browser_info:
                        print(f"WebSocket URL: {browser_info['webSocketDebuggerUrl']}")
                    print(f"Connected to {remote_debugging_url}")
                    return selected_browser, browser_location, remote_debugging_url
                except json.JSONDecodeError:
                    print("Port is in use, but doesn't appear to be Chrome with remote debugging.")
        except requests.RequestException:
            print(f"No Chrome instance detected on {remote_debugging_url}")

        print("\nExecuting Chrome with remote debugging enabled:")

        if host_system == "darwin" or host_system == "linux":
            chrome_cmd = f'{browser_location} --remote-debugging-port={default_port} --user-data-dir="$HOME/chrome-cdp-profile" --no-first-run --no-default-browser-check'
            print(f"    {chrome_cmd}")
        elif host_system == "windows" or host_system == "wsl":
            chrome_cmd = f'"{browser_location}" --remote-debugging-port={default_port} --user-data-dir="C:\\chrome-cdp-profile" --no-first-run --no-default-browser-check'
            print(f"    {chrome_cmd}")
        else:
            print("Unsupported OS for Chrome configuration. Please set it up manually.")

        # Ask user if they want to execute the command
        execute_browser = (
            input("\nWould you like to start Chrome with remote debugging now? (y/n) [y]: ").strip().lower()
        )
        if not execute_browser or execute_browser == "y":
            print(f"Starting Chrome with remote debugging on port {default_port}...")
            try:
                # Execute in background - different approach per OS
                if host_system in ["darwin", "linux"]:
                    subprocess.Popen(f"nohup {chrome_cmd} > /dev/null 2>&1 &", shell=True)
                elif host_system == "windows":
                    subprocess.Popen(f"start {chrome_cmd}", shell=True)
                elif host_system == "wsl":
                    subprocess.Popen(f"cmd.exe /c start {chrome_cmd}", shell=True)

                print(f"Chrome started successfully. Connecting to {remote_debugging_url}")

                print("Waiting for Chrome to initialize...")
                time.sleep(2)

                try:
                    verification_response = requests.get(version_url, timeout=5)
                    if verification_response.status_code == 200:
                        try:
                            browser_info = verification_response.json()
                            print("Connection verified! Chrome is running with remote debugging.")
                            if "Browser" in browser_info:
                                print(f"Browser: {browser_info['Browser']}")
                        except json.JSONDecodeError:
                            print("Warning: Response from Chrome debugging port is not valid JSON.")
                    else:
                        print(f"Warning: Chrome responded with status code {verification_response.status_code}")
                except requests.RequestException as e:
                    print(f"Warning: Could not verify Chrome is running properly: {e}")
                    print("You may need to check Chrome manually or try a different port.")
            except Exception as e:
                print(f"Error starting Chrome: {e}")
                print("Please start Chrome manually using the command above.")

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


def setup_windsurf_config(host_system: str, path_to_env: str) -> bool:
    """Set up Windsurf configuration for Skyvern MCP."""
    if not is_windsurf_installed(host_system):
        return False

    load_dotenv(".env")
    skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
    skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")
    if not skyvern_base_url or not skyvern_api_key:
        print(
            "Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Windsurf MCP. Please open {path_windsurf_config} and set these variables manually."
        )

    try:
        path_windsurf_config = get_windsurf_config_path(host_system)
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
                        "env": {
                            "SKYVERN_BASE_URL": skyvern_base_url,
                            "SKYVERN_API_KEY": skyvern_api_key,
                        },
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                print(
                    f"JSONDecodeError when reading Error configuring Windsurf. Please open {path_windsurf_config} and fix the json config first."
                )
                return False

        with open(path_windsurf_config, "w") as f:
            json.dump(windsurf_config, f, indent=2)
    except Exception as e:
        print(f"Error configuring Windsurf: {e}")
        return False

    print(f"Windsurf MCP configuration updated successfully at {path_windsurf_config}.")
    return True


def setup_mcp_config() -> str:
    """
    return the path to the python environment
    """
    # Try to find Python in this order: python, python3, python3.12, python3.11, python3.10, python3.9
    python_paths = []
    for python_cmd in ["python", "python3.11"]:
        python_path = shutil.which(python_cmd)
        if python_path:
            python_paths.append((python_cmd, python_path))

    if not python_paths:
        print("Error: Could not find any Python installation. Please install Python 3.11 first.")
        path_to_env = typer.prompt(
            "Enter the full path to your python 3.11 environment. For example in MacOS if you installed it using Homebrew, it would be /opt/homebrew/bin/python3.11"
        )
    else:
        # Show the first found Python as default
        _, default_path = python_paths[0]
        path_to_env = default_path
    return path_to_env


def setup_claude_desktop_config(host_system: str, path_to_env: str) -> bool:
    """Set up Claude Desktop configuration with given command and args."""
    if not is_claude_desktop_installed(host_system):
        print("Claude Desktop is not installed. Please install it first.")
        return False

    try:
        path_claude_config = get_claude_config_path(host_system)

        os.makedirs(os.path.dirname(path_claude_config), exist_ok=True)
        if not os.path.exists(path_claude_config):
            with open(path_claude_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        # Read environment variables from .env file
        load_dotenv(".env")
        skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")

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

        print(f"Claude Desktop MCP configuration updated successfully at {path_claude_config}.")
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
        if not os.path.exists(path_cursor_config):
            with open(path_cursor_config, "w") as f:
                json.dump({"mcpServers": {}}, f, indent=2)

        load_dotenv(".env")
        skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")

        if not skyvern_base_url or not skyvern_api_key:
            print(
                f"Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Cursor MCP. Please open {path_cursor_config} and set the these variables manually."
            )

        cursor_config: dict = {"mcpServers": {}}

        if os.path.exists(path_cursor_config):
            try:
                with open(path_cursor_config, "r") as f:
                    cursor_config = json.load(f)
                    cursor_config["mcpServers"].pop("Skyvern", None)
                    cursor_config["mcpServers"]["Skyvern"] = {
                        "env": {
                            "SKYVERN_BASE_URL": skyvern_base_url,
                            "SKYVERN_API_KEY": skyvern_api_key,
                        },
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                print(
                    f"JSONDecodeError when reading Error configuring Cursor. Please open {path_cursor_config} and fix the json config first."
                )
                return False

        with open(path_cursor_config, "w") as f:
            json.dump(cursor_config, f, indent=2)

        print(f"Cursor MCP configuration updated successfully at {path_cursor_config}")
        return True

    except Exception as e:
        print(f"Error configuring Cursor: {e}")
        return False


@setup_app.command(name="mcp")
def setup_mcp() -> None:
    """Configure MCP for different Skyvern deployments."""
    host_system = detect_os()

    path_to_env = setup_mcp_config()

    # Configure both Claude Desktop and Cursor
    claude_response = input("Would you like to set up MCP integration for Claude Desktop? (y/n) [y]: ").strip().lower()
    if not claude_response or claude_response == "y":
        setup_claude_desktop_config(host_system, path_to_env)

    cursor_response = input("Would you like to set up MCP integration for Cursor? (y/n) [y]: ").strip().lower()
    if not cursor_response or cursor_response == "y":
        setup_cursor_config(host_system, path_to_env)

    windsurf_response = input("Would you like to set up MCP integration for Windsurf? (y/n) [y]: ").strip().lower()
    if not windsurf_response or windsurf_response == "y":
        setup_windsurf_config(host_system, path_to_env)


@run_app.command(name="server")
def run_server() -> None:
    load_dotenv()
    load_dotenv(".env")
    from skyvern.config import settings

    port = settings.PORT
    uvicorn.run(
        "skyvern.forge.api_app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


@run_app.command(name="ui")
def run_ui() -> None:
    # FIXME: This is untested and may not work
    """Run the Skyvern UI server."""
    # Check for and handle any existing process on port 8080
    try:
        result = subprocess.run("lsof -t -i :8080", shell=True, capture_output=True, text=True, check=False)
        if result.stdout.strip():
            response = input("Process already running on port 8080. Kill it? (y/n) [y]: ").strip().lower()
            if not response or response == "y":
                subprocess.run("lsof -t -i :8080 | xargs kill", shell=True, check=False)
            else:
                print("UI server not started. Process already running on port 8080.")
                return
    except Exception:
        pass

    # Get the frontend directory path relative to this file
    current_dir = Path(__file__).parent.parent.parent
    frontend_dir = current_dir / "skyvern-frontend"
    if not frontend_dir.exists():
        print(f"[ERROR] Skyvern Frontend directory not found at {frontend_dir}. Are you in the right repo?")
        return

    if not (frontend_dir / ".env").exists():
        shutil.copy(frontend_dir / ".env.example", frontend_dir / ".env")
        # Update VITE_SKYVERN_API_KEY in frontend .env with SKYVERN_API_KEY from main .env
        main_env_path = current_dir / ".env"
        if main_env_path.exists():
            load_dotenv(main_env_path)
            skyvern_api_key = os.getenv("SKYVERN_API_KEY")
            if skyvern_api_key:
                frontend_env_path = frontend_dir / ".env"
                set_key(str(frontend_env_path), "VITE_SKYVERN_API_KEY", skyvern_api_key)
            else:
                print("[ERROR] SKYVERN_API_KEY not found in .env file")
        else:
            print("[ERROR] .env file not found")

        print("Successfully set up frontend .env file")

    # Change to frontend directory
    os.chdir(frontend_dir)

    # Run npm install and start
    try:
        subprocess.run("npm install --silent", shell=True, check=True)
        subprocess.run("npm run start", shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running UI server: {e}")
        return


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


@cli_app.command(name="init")
def init(no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container")) -> None:
    run_local_str = (
        input("Would you like to run Skyvern locally or in the cloud? (local/cloud) [cloud]: ").strip().lower()
    )
    run_local = run_local_str == "local" if run_local_str else False

    if run_local:
        setup_postgresql(no_postgres)
        migrate_db()
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
        base_url = input("Enter Skyvern base URL (press Enter for https://api.skyvern.com): ").strip()
        if not base_url:
            base_url = "https://api.skyvern.com"

        print("To get your API key:")
        print("1. Create an account at https://app.skyvern.com")
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

        if not run_local:
            print("\nMCP configuration is complete! Your AI applications are now ready to use Skyvern Cloud.")

    if run_local:
        print("\nInstalling Chromium browser...")
        subprocess.run(["playwright", "install", "chromium"], check=True)
        print("Chromium installation complete.")

        print("\nTo start using Skyvern, run:")
        print("    skyvern run server")
