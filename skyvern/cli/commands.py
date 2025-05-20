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

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.library import Skyvern
from skyvern.utils import detect_os, get_windows_appdata_roaming, migrate_db

# Import from rich
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.padding import Padding
from rich.prompt import Confirm, Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn

# Create a global console instance for consistent output
console = Console()

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
    skyvern_agent = Skyvern(
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
    )
    res = await skyvern_agent.run_task(prompt=prompt, url=url, user_agent="skyvern-mcp")

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
        console.print(f"[red]Error executing command: [bold]{command}[/bold][/red]", style="red")
        console.print(f"[red]Stderr: {e.stderr.strip()}[/red]", style="red")
        return None, e.returncode


def is_postgres_running() -> bool:
    if command_exists("pg_isready"):
        with console.status("[bold green]Checking PostgreSQL status...") as status:
            result, _ = run_command("pg_isready")
            if result is not None and "accepting connections" in result:
                status.stop()
                return True
            status.stop()
            return False
    return False


def database_exists(dbname: str, user: str) -> bool:
    check_db_command = f'psql {dbname} -U {user} -c "\\q"'
    output, _ = run_command(check_db_command, check=False)
    return output is not None


def create_database_and_user() -> None:
    console.print("🚀 [bold green]Creating database user and database...[/bold green]")
    run_command("createuser skyvern")
    run_command("createdb skyvern -O skyvern")
    console.print("✅ [bold green]Database and user created successfully.[/bold green]")


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

    console.print(Panel("[bold cyan]PostgreSQL Setup[/bold cyan]", border_style="blue"))

    if command_exists("psql") and is_postgres_running():
        console.print("✨ [green]PostgreSQL is already running locally.[/green]")
        if database_exists("skyvern", "skyvern"):
            console.print("✅ [green]Database and user exist.[/green]")
        else:
            create_database_and_user()
        return

    if no_postgres:
        console.print("[yellow]Skipping PostgreSQL container setup as requested.[/yellow]")
        console.print("[italic]If you plan to use Docker Compose, its Postgres service will start automatically.[/italic]")
        return

    if not is_docker_running():
        console.print("[red]Docker is not running or not installed. Please install or start Docker and try again.[/red]")
        exit(1)

    if is_postgres_running_in_docker():
        console.print("🐳 [green]PostgreSQL is already running in a Docker container.[/green]")
    else:
        if not no_postgres:
            start_postgres = Confirm.ask(
                '[yellow]No local Postgres detected. Start a disposable container now?[/yellow]\n'
                '[tip: choose "n" if you plan to run Skyvern via Docker Compose instead of `skyvern run server`]'
            )
            if not start_postgres:
                console.print("[yellow]Skipping PostgreSQL container setup.[/yellow]")
                console.print("[italic]If you plan to use Docker Compose, its Postgres service will start automatically.[/italic]")
                return

        console.print("🚀 [bold green]Attempting to install PostgreSQL via Docker...[/bold green]")
        if not is_postgres_container_exists():
            with console.status("[bold blue]Pulling and starting PostgreSQL container...[/bold blue]") as status:
                run_command(
                    "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
                )
            console.print("✅ [green]PostgreSQL has been installed and started using Docker.[/green]")
        else:
            with console.status("[bold blue]Starting existing PostgreSQL container...[/bold blue]") as status:
                run_command("docker start postgresql-container")
            console.print("✅ [green]Existing PostgreSQL container started.[/green]")


        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            console=console
        ) as progress:
            progress.add_task("[bold blue]Waiting for PostgreSQL to become ready...", total=None)
            time.sleep(20) # This sleep can be replaced with a more robust readiness check in a real app

        console.print("✅ [green]PostgreSQL container ready.[/green]")

    with console.status("[bold green]Checking database user...[/bold green]") as status:
        _, code = run_command('docker exec postgresql-container psql -U postgres -c "\\du" | grep -q skyvern', check=False)
        if code == 0:
            console.print("✅ [green]Database user exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database user...[/bold green]")
            run_command("docker exec postgresql-container createuser -U postgres skyvern")
            console.print("✅ [green]Database user created.[/green]")

    with console.status("[bold green]Checking database...[/bold green]") as status:
        _, code = run_command(
            "docker exec postgresql-container psql -U postgres -lqt | cut -d \\| -f 1 | grep -qw skyvern", check=False
        )
        if code == 0:
            console.print("✅ [green]Database exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database...[/bold green]")
            run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
            console.print("✅ [green]Database and user created successfully.[/green]")


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
    console.print(Panel("[bold magenta]LLM Provider Configuration[/bold magenta]", border_style="purple"))
    console.print("[italic]Note: All information provided here will be stored only on your local machine.[/italic]")
    model_options = []

    # OpenAI Configuration
    console.print("\n[bold blue]--- OpenAI Configuration ---[/bold blue]")
    console.print("To enable OpenAI, you must have an OpenAI API key.")
    enable_openai = Confirm.ask("Do you want to enable OpenAI?")
    if enable_openai:
        openai_api_key = Prompt.ask("Enter your OpenAI API key", password=True)
        if not openai_api_key:
            console.print("[red]Error: OpenAI API key is required. OpenAI will not be enabled.[/red]")
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
    console.print("\n[bold blue]--- Anthropic Configuration ---[/bold blue]")
    console.print("To enable Anthropic, you must have an Anthropic API key.")
    enable_anthropic = Confirm.ask("Do you want to enable Anthropic?")
    if enable_anthropic:
        anthropic_api_key = Prompt.ask("Enter your Anthropic API key", password=True)
        if not anthropic_api_key:
            console.print("[red]Error: Anthropic API key is required. Anthropic will not be enabled.[/red]")
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
    console.print("\n[bold blue]--- Azure Configuration ---[/bold blue]")
    console.print("To enable Azure, you must have an Azure deployment name, API key, base URL, and API version.")
    enable_azure = Confirm.ask("Do you want to enable Azure?")
    if enable_azure:
        azure_deployment = Prompt.ask("Enter your Azure deployment name")
        azure_api_key = Prompt.ask("Enter your Azure API key", password=True)
        azure_api_base = Prompt.ask("Enter your Azure API base URL")
        azure_api_version = Prompt.ask("Enter your Azure API version")
        if not all([azure_deployment, azure_api_key, azure_api_base, azure_api_version]):
            console.print("[red]Error: All Azure fields must be populated. Azure will not be enabled.[/red]")
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
    console.print("\n[bold blue]--- Gemini Configuration ---[/bold blue]")
    console.print("To enable Gemini, you must have a Gemini API key.")
    enable_gemini = Confirm.ask("Do you want to enable Gemini?")
    if enable_gemini:
        gemini_api_key = Prompt.ask("Enter your Gemini API key", password=True)
        if not gemini_api_key:
            console.print("[red]Error: Gemini API key is required. Gemini will not be enabled.[/red]")
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
    console.print("\n[bold blue]--- Novita AI Configuration ---[/bold blue]")
    console.print("To enable Novita AI, you must have a Novita AI API key.")
    enable_novita = Confirm.ask("Do you want to enable Novita AI?")
    if enable_novita:
        novita_api_key = Prompt.ask("Enter your Novita AI API key", password=True)
        if not novita_api_key:
            console.print("[red]Error: Novita AI API key is required. Novita AI will not be enabled.[/red]")
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
    console.print("\n[bold blue]--- OpenAI-Compatible Provider Configuration ---[/bold blue]")
    console.print("To enable an OpenAI-compatible provider, you must have a model name, API key, and API base URL.")
    enable_openai_compatible = Confirm.ask("Do you want to enable an OpenAI-compatible provider?")
    if enable_openai_compatible:
        openai_compatible_model_name = Prompt.ask("Enter the model name (e.g., 'yi-34b', 'mistral-large')")
        openai_compatible_api_key = Prompt.ask("Enter your API key", password=True)
        openai_compatible_api_base = Prompt.ask("Enter the API base URL (e.g., 'https://api.together.xyz/v1')")
        openai_compatible_vision = Confirm.ask("Does this model support vision?")

        if not all([openai_compatible_model_name, openai_compatible_api_key, openai_compatible_api_base]):
            console.print("[red]Error: All required fields must be populated. OpenAI-compatible provider will not be enabled.[/red]")
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
            openai_compatible_api_version = Prompt.ask("Enter API version (optional, press enter to skip)", default="")
            if openai_compatible_api_version:
                update_or_add_env_var("OPENAI_COMPATIBLE_API_VERSION", openai_compatible_api_version)

            update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "true")
            model_options.append("OPENAI_COMPATIBLE")
    else:
        update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "false")

    # Model Selection
    if not model_options:
        console.print(
            Panel(
                "[bold red]No LLM providers enabled.[/bold red]\n"
                "You won't be able to run Skyvern unless you enable at least one provider.\n"
                "You can re-run this script to enable providers or manually update the .env file.",
                border_style="red"
            )
        )
    else:
        console.print("\n[bold green]Available LLM models based on your selections:[/bold green]")
        for i, model in enumerate(model_options, 1):
            console.print(f"  [cyan]{i}.[/cyan] [green]{model}[/green]")

        chosen_model_idx = Prompt.ask(
            f"Choose a model by number (e.g., [cyan]1[/cyan] for [green]{model_options[0]}[/green])",
            choices=[str(i) for i in range(1, len(model_options) + 1)],
            default="1" # Default to the first option
        )
        chosen_model = model_options[int(chosen_model_idx) - 1]
        console.print(f"🎉 [bold green]Chosen LLM Model: {chosen_model}[/bold green]")
        update_or_add_env_var("LLM_KEY", chosen_model)

    console.print("✅ [green]LLM provider configurations updated in .env.[/green]")


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
    console.print(Panel("\n[bold blue]Configuring web browser for scraping...[/bold blue]", border_style="cyan"))
    browser_types = ["chromium-headless", "chromium-headful", "cdp-connect"]

    for i, browser_type in enumerate(browser_types, 1):
        console.print(f"[cyan]{i}.[/cyan] [bold]{browser_type}[/bold]")
        if browser_type == "chromium-headless":
            console.print("   - Runs Chrome in [italic]headless[/italic] mode (no visible window)")
        elif browser_type == "chromium-headful":
            console.print("   - Runs Chrome with [italic]visible window[/italic]")
        elif browser_type == "cdp-connect":
            console.print("   - Connects to an [italic]existing Chrome instance[/italic]")
            console.print("   - [yellow]Requires Chrome to be running with remote debugging enabled[/yellow]")

    selected_browser_idx = Prompt.ask("\nChoose browser type", choices=[str(i) for i in range(1, len(browser_types) + 1)])
    selected_browser = browser_types[int(selected_browser_idx) - 1]
    console.print(f"Selected browser: [bold green]{selected_browser}[/bold green]")

    browser_location = None
    remote_debugging_url = None

    if selected_browser == "cdp-connect":
        host_system = detect_os()
        default_location = get_default_chrome_location(host_system)
        console.print(f"\n[italic]Default Chrome location for your system:[/italic] [cyan]{default_location}[/cyan]")
        browser_location = Prompt.ask("Enter Chrome executable location (press Enter to use default)", default=default_location)
        if not browser_location:
            browser_location = default_location

        if not os.path.exists(browser_location):
            console.print(f"[yellow]Warning: Chrome not found at {browser_location}. Please verify the location is correct.[/yellow]")

        console.print("\n[bold]To use CDP connection, Chrome must be running with remote debugging enabled.[/bold]")
        console.print("Example: [code]chrome --remote-debugging-port=9222[/code]")
        console.print("[italic]Default debugging URL: [cyan]http://localhost:9222[/cyan][/italic]")

        default_port = "9222"
        if remote_debugging_url is None:
            remote_debugging_url = "http://localhost:9222"
        elif ":" in remote_debugging_url.split("/")[-1]:
            default_port = remote_debugging_url.split(":")[-1].split("/")[0]

        parsed_url = urlparse(remote_debugging_url)
        version_url = f"{parsed_url.scheme}://{parsed_url.netloc}/json/version"

        with console.status(f"[bold green]Checking if Chrome is already running with remote debugging on port {default_port}...") as status:
            try:
                response = requests.get(version_url, timeout=2)
                if response.status_code == 200:
                    try:
                        browser_info = response.json()
                        console.print("✅ [green]Chrome is already running with remote debugging![/green]")
                        if "Browser" in browser_info:
                            console.print(f"  Browser: [bold]{browser_info['Browser']}[/bold]")
                        if "webSocketDebuggerUrl" in browser_info:
                            console.print(f"  WebSocket URL: [link]{browser_info['webSocketDebuggerUrl']}[/link]")
                        console.print(f"  Connected to [link]{remote_debugging_url}[/link]")
                        return selected_browser, browser_location, remote_debugging_url
                    except json.JSONDecodeError:
                        console.print("[yellow]Port is in use, but doesn't appear to be Chrome with remote debugging.[/yellow]")
                else:
                    console.print(f"[yellow]Chrome responded with status code {response.status_code}.[/yellow]")
            except requests.RequestException:
                console.print(f"[red]No Chrome instance detected on {remote_debugging_url}[/red]")
        status.stop() # Ensure status stops if not already returned

        console.print("\n[bold]Executing Chrome with remote debugging enabled:[/bold]")

        if host_system == "darwin" or host_system == "linux":
            chrome_cmd = f'{browser_location} --remote-debugging-port={default_port} --user-data-dir="$HOME/chrome-cdp-profile" --no-first-run --no-default-browser-check'
            console.print(f"    [code]{chrome_cmd}[/code]")
        elif host_system == "windows" or host_system == "wsl":
            chrome_cmd = f'"{browser_location}" --remote-debugging-port={default_port} --user-data-dir="C:\\chrome-cdp-profile" --no-first-run --no-default-browser-check'
            console.print(f"    [code]{chrome_cmd}[/code]")
        else:
            console.print("[red]Unsupported OS for Chrome configuration. Please set it up manually.[/red]")

        execute_browser = Confirm.ask("\nWould you like to start Chrome with remote debugging now?")
        if execute_browser:
            console.print(f"🚀 [bold green]Starting Chrome with remote debugging on port {default_port}...\n[/bold green]")
            try:
                if host_system in ["darwin", "linux"]:
                    subprocess.Popen(f"nohup {chrome_cmd} > /dev/null 2>&1 &", shell=True)
                elif host_system == "windows":
                    subprocess.Popen(f"start {chrome_cmd}", shell=True)
                elif host_system == "wsl":
                    subprocess.Popen(f"cmd.exe /c start {chrome_cmd}", shell=True)

                console.print(f"✅ [green]Chrome started successfully. Connecting to [link]{remote_debugging_url}[/link][/green]")

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    transient=True,
                    console=console
                ) as progress:
                    progress.add_task("[bold blue]Waiting for Chrome to initialize...", total=None)
                    time.sleep(2)

                try:
                    verification_response = requests.get(version_url, timeout=5)
                    if verification_response.status_code == 200:
                        try:
                            browser_info = verification_response.json()
                            console.print("✅ [green]Connection verified! Chrome is running with remote debugging.[/green]")
                            if "Browser" in browser_info:
                                console.print(f"  Browser: [bold]{browser_info['Browser']}[/bold]")
                        except json.JSONDecodeError:
                            console.print("[yellow]Warning: Response from Chrome debugging port is not valid JSON.[/yellow]")
                    else:
                        console.print(f"[yellow]Warning: Chrome responded with status code {verification_response.status_code}[/yellow]")
                except requests.RequestException as e:
                    console.print(f"[red]Warning: Could not verify Chrome is running properly: {e}[/red]")
                    console.print("[italic]You may need to check Chrome manually or try a different port.[/italic]")
            except Exception as e:
                console.print(f"[red]Error starting Chrome: {e}[/red]")
                console.print("[italic]Please start Chrome manually using the command above.[/italic]")

        remote_debugging_url = Prompt.ask("Enter remote debugging URL (press Enter for default)", default="http://localhost:9222")
        if not remote_debugging_url:
            remote_debugging_url = "http://localhost:9222"

    return selected_browser, browser_location, remote_debugging_url


async def _setup_local_organization() -> str:
    """
    Returns the API key for the local organization generated
    """
    skyvern_agent = Skyvern(
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
    console.print(Panel("[bold green]Running Database Migrations...[/bold green]", border_style="green"))
    migrate_db()
    console.print("✅ [green]Database migration complete.[/green]")


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
        console.print(
            f"[red]Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file to set up Windsurf MCP. Please open {path_windsurf_config} and set these variables manually.[/red]"
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
                console.print(
                    f"[red]JSONDecodeError when reading Error configuring Windsurf. Please open {path_windsurf_config} and fix the json config first.[/red]"
                )
                return False

        with open(path_windsurf_config, "w") as f:
            json.dump(windsurf_config, f, indent=2)
    except Exception as e:
        console.print(f"[red]Error configuring Windsurf: {e}[/red]")
        return False

    console.print(f"✅ [green]Windsurf MCP configuration updated successfully at [link]{path_windsurf_config}[/link].[/green]")
    return True


def setup_mcp_config() -> str:
    """
    return the path to the python environment
    """
    console.print(Panel("[bold yellow]Setting up MCP Python Environment[/bold yellow]", border_style="yellow"))
    # Try to find Python in this order: python, python3, python3.12, python3.11, python3.10, python3.9
    python_paths = []
    for python_cmd in ["python", "python3.11"]: # Added python3.11 as primary check
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


def setup_claude_desktop_config(host_system: str, path_to_env: str) -> bool:
    """Set up Claude Desktop configuration with given command and args."""
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

        # Read environment variables from .env file
        load_dotenv(".env")
        skyvern_base_url = os.environ.get("SKYVERN_BASE_URL", "")
        skyvern_api_key = os.environ.get("SKYVERN_API_KEY", "")

        if not skyvern_base_url or not skyvern_api_key:
            console.print("[red]Error: SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file[/red]")
            return False # Indicate failure if critical env vars are missing

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

        console.print(f"✅ [green]Claude Desktop MCP configuration updated successfully at [link]{path_claude_config}[/link].[/green]")
        return True

    except Exception as e:
        console.print(f"[red]Error configuring Claude Desktop: {e}[/red]")
        return False


def setup_cursor_config(host_system: str, path_to_env: str) -> bool:
    """Set up Cursor configuration with given command and args."""
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
                        "env": {
                            "SKYVERN_BASE_URL": skyvern_base_url,
                            "SKYVERN_API_KEY": skyvern_api_key,
                        },
                        "command": path_to_env,
                        "args": ["-m", "skyvern", "run", "mcp"],
                    }
            except json.JSONDecodeError:
                console.print(
                    f"[red]JSONDecodeError when reading Error configuring Cursor. Please open [link]{path_cursor_config}[/link] and fix the json config first.[/red]"
                )
                return False

        with open(path_cursor_config, "w") as f:
            json.dump(cursor_config, f, indent=2)

        console.print(f"✅ [green]Cursor MCP configuration updated successfully at [link]{path_cursor_config}[/link][/green]")
        return True

    except Exception as e:
        console.print(f"[red]Error configuring Cursor: {e}[/red]")
        return False


@setup_app.command(name="mcp")
def setup_mcp() -> None:
    """Configure MCP for different Skyvern deployments."""
    console.print(Panel("[bold green]MCP Server Setup[/bold green]", border_style="green"))
    host_system = detect_os()

    path_to_env = setup_mcp_config()

    claude_response = Confirm.ask("Would you like to set up MCP integration for Claude Desktop?", default=True)
    if claude_response:
        setup_claude_desktop_config(host_system, path_to_env)

    cursor_response = Confirm.ask("Would you like to set up MCP integration for Cursor?", default=True)
    if cursor_response:
        setup_cursor_config(host_system, path_to_env)

    windsurf_response = Confirm.ask("Would you like to set up MCP integration for Windsurf?", default=True)
    if windsurf_response:
        setup_windsurf_config(host_system, path_to_env)
    
    console.print("\n🎉 [bold green]MCP server configuration completed.[/bold green]")


@run_app.command(name="server")
def run_server() -> None:
    load_dotenv()
    load_dotenv(".env")
    from skyvern.config import settings

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Skyvern API Server on port {port}...", border_style="green"))
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
    console.print(Panel("[bold blue]Starting Skyvern UI Server...[/bold blue]", border_style="blue"))
    # Check for and handle any existing process on port 8080
    try:
        with console.status("[bold green]Checking for existing process on port 8080...") as status:
            result = subprocess.run("lsof -t -i :8080", shell=True, capture_output=True, text=True, check=False)
            if result.stdout.strip():
                status.stop()
                response = Confirm.ask("Process already running on port 8080. [yellow]Kill it?[/yellow]")
                if response:
                    subprocess.run("lsof -t -i :8080 | xargs kill", shell=True, check=False)
                    console.print("✅ [green]Process killed.[/green]")
                else:
                    console.print("[yellow]UI server not started. Process already running on port 8080.[/yellow]")
                    return
            status.stop()
    except Exception as e:
        console.print(f"[red]Error checking for process: {e}[/red]")
        pass

    # Get the frontend directory path relative to this file
    current_dir = Path(__file__).parent.parent.parent
    frontend_dir = current_dir / "skyvern-frontend"
    if not frontend_dir.exists():
        console.print(f"[bold red]ERROR: Skyvern Frontend directory not found at [path]{frontend_dir}[/path]. Are you in the right repo?[/bold red]")
        return

    if not (frontend_dir / ".env").exists():
        console.print("[bold blue]Setting up frontend .env file...[/bold blue]")
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
                console.print("[red]ERROR: SKYVERN_API_KEY not found in .env file[/red]")
        else:
            console.print("[red]ERROR: .env file not found[/red]")

        console.print("✅ [green]Successfully set up frontend .env file[/green]")

    # Change to frontend directory
    os.chdir(frontend_dir)

    # Run npm install and start
    try:
        console.print("📦 [bold blue]Running npm install...[/bold blue]")
        subprocess.run("npm install --silent", shell=True, check=True)
        console.print("✅ [green]npm install complete.[/green]")
        console.print("🚀 [bold blue]Starting npm UI server...[/bold blue]")
        subprocess.run("npm run start", shell=True, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error running UI server: {e}[/bold red]")
        return


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Run the MCP server."""
    console.print(Panel("[bold green]Starting MCP Server...[/bold green]", border_style="green"))
    mcp.run(transport="stdio")


@cli_app.command(name="init")
def init(no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container")) -> None:
    console.print(Panel("[bold green]Welcome to Skyvern CLI Initialization![/bold green]", border_style="green", expand=False))
    console.print("[italic]This wizard will help you set up Skyvern.[/italic]")

    run_local = Confirm.ask("Would you like to run Skyvern [bold blue]locally[/bold blue] or in the [bold purple]cloud[/bold purple]?", default=False, choices=["local", "cloud"])
    
    if run_local:
        setup_postgresql(no_postgres)
        console.print("📊 [bold blue]Running database migrations...[/bold blue]")
        migrate_db()
        console.print("✅ [green]Database migration complete.[/green]")
        
        console.print("🔑 [bold blue]Generating local organization API key...[/bold blue]")
        api_key = asyncio.run(_setup_local_organization())
        if api_key:
            console.print("✅ [green]Local organization API key generated.[/green]")
        else:
            console.print("[red]Failed to generate local organization API key. Please check server logs.[/red]")

        if os.path.exists(".env"):
            console.print("💡 [.env] file already exists.", style="yellow")
            redo_llm_setup = Confirm.ask("Do you want to go through [bold yellow]LLM provider setup again[/bold yellow]?", default=False)
            if not redo_llm_setup:
                console.print("[green]Skipping LLM setup.[/green]")
            else:
                console.print("\n[bold blue]Initializing .env file for LLM providers...[/bold blue]")
                setup_llm_providers()
        else:
            console.print("\n[bold blue]Initializing .env file...[/bold blue]")
            setup_llm_providers()


        # Configure browser settings
        console.print("\n[bold blue]Configuring browser settings...[/bold blue]")
        browser_type, browser_location, remote_debugging_url = setup_browser_config()
        update_or_add_env_var("BROWSER_TYPE", browser_type)
        if browser_location:
            update_or_add_env_var("CHROME_EXECUTABLE_PATH", browser_location)
        if remote_debugging_url:
            update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)
        console.print("✅ [green]Browser configuration complete.[/green]")

        console.print("🌐 [bold blue]Setting Skyvern Base URL to: http://localhost:8000[/bold blue]")
        update_or_add_env_var("SKYVERN_BASE_URL", "http://localhost:8000")

    else: # Cloud setup
        console.print(Panel("[bold purple]Cloud Deployment Setup[/bold purple]", border_style="purple"))
        base_url = Prompt.ask("Enter Skyvern base URL", default="https://api.skyvern.com", show_default=True)
        if not base_url:
            base_url = "https://api.skyvern.com"

        console.print("\n[bold]To get your API key:[/bold]")
        console.print("1. Create an account at [link]https://app.skyvern.com[/link]")
        console.print("2. Go to [bold cyan]Settings[/bold cyan]")
        console.print("3. [bold green]Copy your API key[/bold green]")
        api_key = Prompt.ask("Enter your Skyvern API key", password=True)
        if not api_key:
            console.print("[red]API key is required.[/red]")
            api_key = Prompt.ask("Please re-enter your Skyvern API key", password=True)
            if not api_key:
                console.print("[bold red]Error: API key cannot be empty. Aborting initialization.[/bold red]")
                return # Exit if API key is not provided even after re-prompt

        update_or_add_env_var("SKYVERN_BASE_URL", base_url)


    # Ask for email or generate UUID
    analytics_id_input = Prompt.ask("Please enter your email for analytics (press enter to skip)", default="")
    analytics_id = analytics_id_input if analytics_id_input else str(uuid.uuid4())
    update_or_add_env_var("ANALYTICS_ID", analytics_id)
    update_or_add_env_var("SKYVERN_API_KEY", api_key) # This might overwrite local API key if init is re-run for LLM setup.
                                                        # Consider moving this to just after API key determination.
    console.print("✅ [green].env file has been initialized.[/green]")

    # Ask if user wants to configure MCP server
    configure_mcp = Confirm.ask("\nWould you like to [bold yellow]configure the MCP server[/bold yellow]?", default=True)
    if configure_mcp:
        setup_mcp()
        
        if not run_local:
            console.print("\n🎉 [bold green]MCP configuration is complete! Your AI applications are now ready to use Skyvern Cloud.[/bold green]")

    if run_local:
        console.print("\n⬇️ [bold blue]Installing Chromium browser...[/bold blue]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            console=console
        ) as progress:
            progress.add_task("[bold blue]Downloading Chromium, this may take a moment...", total=None)
            subprocess.run(["playwright", "install", "chromium"], check=True)
        console.print("✅ [green]Chromium installation complete.[/green]")

        console.print("\n🎉 [bold green]Skyvern setup complete![/bold green]")
        console.print("[bold]To start using Skyvern, run:[/bold]")
        console.print(Padding("skyvern run server", (1, 4), style="reverse green"))


if __name__ == "__main__":
    cli_app()