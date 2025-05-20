import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
import webbrowser
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, cast
from urllib.parse import urlparse

import requests  # type: ignore
import typer
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.library import Skyvern
from skyvern.utils import detect_os, get_windows_appdata_roaming, migrate_db

# Initialize Rich console for better formatting
console = Console()

# Main application
cli_app = typer.Typer(
    help="Skyvern - Browser automation powered by LLMs and Computer Vision",
    add_completion=False,
)

# Subcommands
run_app = typer.Typer(help="Run Skyvern components")
setup_app = typer.Typer(help="Set up Skyvern configurations")
tasks_app = typer.Typer(help="Manage Skyvern tasks")
workflows_app = typer.Typer(help="Manage Skyvern workflows")
docs_app = typer.Typer(help="Access Skyvern documentation")

# Add subcommands to main app
cli_app.add_typer(run_app, name="run")
cli_app.add_typer(setup_app, name="setup")
cli_app.add_typer(tasks_app, name="tasks")
cli_app.add_typer(workflows_app, name="workflows")
cli_app.add_typer(docs_app, name="docs")

# Documentation sections and their URLs

# Documentation sections and their URLs
DOCUMENTATION = {
    "quickstart": "https://docs.skyvern.com/introduction",
    "tasks": "https://docs.skyvern.com/running-tasks/introduction",
    "workflows": "https://docs.skyvern.com/workflows/introduction",
    "prompting": "https://docs.skyvern.com/getting-started/prompting-guide",
    "api": "https://docs.skyvern.com/integrations/api",
}


class DeploymentType(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class BrowserType(str, Enum):
    HEADLESS = "chromium-headless"
    HEADFUL = "chromium-headful"
    CDP = "cdp-connect"


# ----------------------------------------------------
# 1. Guided Onboarding Flow
# ----------------------------------------------------


@cli_app.command(name="init")
def init(
    deployment: Optional[DeploymentType] = typer.Option(None, help="Deployment type: local or cloud"),
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
) -> None:
    """
    Initialize Skyvern with a guided setup process.

    This wizard will help you configure Skyvern for either local development
    or connection to Skyvern Cloud. It will guide you through:

    - Choosing deployment type (local or cloud)
    - Setting up database (for local deployment)
    - Configuring LLM providers
    - Setting up browser automation
    - Configuring integrations
    """
    console.print(
        Panel.fit(
            "[bold blue]Welcome to Skyvern Setup Wizard[/]", subtitle="Let's get you started with browser automation"
        )
    )

    # Step 1: Choose deployment type
    if deployment is None:
        console.print(Markdown("## Step 1: Choose Deployment Type"))
        console.print("\n[yellow]Local deployment[/] - Run Skyvern on your machine")
        console.print(" • Requires local database and LLM API keys")
        console.print(" • Good for development and testing")
        console.print("\n[yellow]Cloud deployment[/] - Connect to Skyvern Cloud")
        console.print(" • Managed service with no local infrastructure")
        console.print(" • Production-ready with built-in scaling")

        deployment_choice = (
            console.input("\n[bold]Deploy locally or connect to cloud? [cloud/local] [/]").strip().lower()
        )
        run_local = deployment_choice == "local"
    else:
        run_local = deployment == DeploymentType.LOCAL

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        if run_local:
            # Step 2: Set up local infrastructure (for local deployment)
            setup_task = progress.add_task("[green]Setting up local infrastructure...", total=1)

            if not no_postgres:
                setup_postgresql(no_postgres)

            migrate_db()

            api_key_task = progress.add_task("[green]Generating API key...", total=1)
            api_key = asyncio.run(_setup_local_organization())
            progress.update(api_key_task, completed=1)

            # Step 3: Configure LLM providers
            progress.update(setup_task, completed=1)
            llm_task = progress.add_task("[green]Setting up LLM providers...", total=1)
            setup_llm_providers()
            progress.update(llm_task, completed=1)

            # Step 4: Configure browser settings
            browser_task = progress.add_task("[green]Setting up browser automation...", total=1)
            browser_type, browser_location, remote_debugging_url = setup_browser_config()
            update_or_add_env_var("BROWSER_TYPE", browser_type)
            if browser_location:
                update_or_add_env_var("CHROME_EXECUTABLE_PATH", browser_location)
            if remote_debugging_url:
                update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)
            progress.update(browser_task, completed=1)

            # Set defaults for local development
            update_or_add_env_var("SKYVERN_BASE_URL", "http://localhost:8000")
        else:
            # Configure for cloud deployment
            cloud_task = progress.add_task("[green]Setting up Skyvern Cloud connection...", total=1)

            base_url = console.input("\nEnter Skyvern base URL [https://api.skyvern.com]: ").strip()
            if not base_url:
                base_url = "https://api.skyvern.com"

            console.print("\nTo get your API key:")
            console.print("1. Create an account at [link=https://app.skyvern.com]https://app.skyvern.com[/link]")
            console.print("2. Go to Settings")
            console.print("3. Copy your API key")

            api_key = console.input("\nEnter your Skyvern API key: ").strip()
            while not api_key:
                console.print("[bold red]API key is required[/]")
                api_key = console.input("Enter your Skyvern API key: ").strip()

            update_or_add_env_var("SKYVERN_BASE_URL", base_url)
            progress.update(cloud_task, completed=1)

        # Common configuration
        analytics_task = progress.add_task("[green]Finalizing configuration...", total=1)

        # Ask for email or generate UUID for analytics
        analytics_id = console.input("\nPlease enter your email for analytics (press enter to skip): ")
        if not analytics_id:
            analytics_id = str(uuid.uuid4())

        update_or_add_env_var("ANALYTICS_ID", analytics_id)
        update_or_add_env_var("SKYVERN_API_KEY", api_key)
        progress.update(analytics_task, completed=1)

    # Step 5: Configure integrations
    console.print(Markdown("\n## Step 5: Configure Integrations"))
    configure_mcp = typer.confirm(
        "Would you like to configure AI integrations (Claude, Cursor, Windsurf)?", default=True
    )
    if configure_mcp:
        setup_mcp()
        console.print("\n[green]AI integrations configured successfully![/]")

    if run_local:
        # Install required components for local deployment
        console.print(Markdown("\n## Step 6: Installing Components"))
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            browser_install_task = progress.add_task("[green]Installing Chromium browser...", total=1)
            subprocess.run(["playwright", "install", "chromium"], check=True)
            progress.update(browser_install_task, completed=1)

    # Success message and next steps
    console.print(
        Panel.fit("[bold green]Skyvern setup complete![/]", subtitle="You're ready to start automating browsers")
    )

    if run_local:
        console.print("\n[bold]Next steps:[/]")
        console.print("1. Start the Skyvern server:         [yellow]skyvern run server[/]")
        console.print("2. Start the Skyvern UI:             [yellow]skyvern run ui[/]")
    else:
        console.print("\n[bold]Next steps:[/]")
        console.print(
            "1. Visit the Skyvern Cloud dashboard: [link=https://app.skyvern.com]https://app.skyvern.com[/link]"
        )
        console.print("2. Try using an AI integration:       [yellow]skyvern docs integrations[/]")


# ----------------------------------------------------
# 3. Improved Documentation Integration
# ----------------------------------------------------


@docs_app.command(name="open")
def open_docs(section: str = typer.Argument("quickstart", help="Documentation section to open")) -> None:
    """
    Open Skyvern documentation in your web browser.

    Available sections:
    - quickstart: Getting started guide
    - tasks: Task creation and running
    - workflows: Workflow creation and running
    - prompting: Best practices for writing prompts
    - api: API reference
    """
    if section not in DOCUMENTATION:
        console.print(f"[bold red]Error:[/] Documentation section '{section}' not found")
        console.print("\nAvailable sections:")
        for name, url in DOCUMENTATION.items():
            console.print(f"  • [bold]{name}[/] - {url}")
        return

    url = DOCUMENTATION[section]
    console.print(f"Opening documentation section: [bold]{section}[/]")
    console.print(f"URL: [link={url}]{url}[/link]")
    webbrowser.open(url)


@docs_app.command(name="prompting")
def prompting_guide() -> None:
    """
    Show prompting best practices for Skyvern.
    """
    console.print(
        Panel.fit("[bold blue]Skyvern Prompting Best Practices[/]", subtitle="Tips for writing effective prompts")
    )

    console.print(
        Markdown("""
## General Guidelines

1. **Be specific and detailed**
   - Specify exactly what actions should be taken
   - Include any data or criteria needed for decisions

2. **Define completion criteria**
   - Use COMPLETE/TERMINATE markers to indicate success/failure conditions
   - Specify what data to extract (if any)

3. **Break complex tasks into steps**
   - For multi-page flows, describe each step clearly
   - Use sequencing terms (first, then, after)

## Examples

✅ **Good prompt:**
```
Navigate to the products page. Find the product named "Wireless Headphones" 
and add it to the cart. Proceed to checkout and fill the form with:
Name: John Doe
Email: john@example.com
When complete, extract the order confirmation number.
COMPLETE when you see a "Thank you for your order" message.
```

❌ **Less effective prompt:**
```
Buy wireless headphones and check out.
```

## For More Information

Run `skyvern docs open prompting` to see the complete prompting guide online.
    """)
    )


# ----------------------------------------------------
# 4. User-Friendly Management Commands
# ----------------------------------------------------


@cli_app.command(name="status")
def status() -> None:
    """
    Check the status of Skyvern services.
    """
    console.print(Panel.fit("[bold blue]Skyvern Services Status[/]", subtitle="Checking all system components"))

    # Check for .env file
    env_path = Path(".env")
    env_status = "✅ Found" if env_path.exists() else "❌ Not found"

    # Check database connection
    db_status = "⏳ Checking..."
    try:
        load_dotenv()
        # Simple check - just see if we can run a migrate command without error
        migrate_db()
        db_status = "✅ Connected"
    except Exception:
        db_status = "❌ Not connected"

    # Check if server is running (port 8000)
    server_status = "⏳ Checking..."
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("localhost", 8000))
        s.close()
        server_status = "✅ Running"
    except Exception:
        server_status = "❌ Not running"

    # Check if UI is running (port 8080)
    ui_status = "⏳ Checking..."
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("localhost", 8080))
        s.close()
        ui_status = "✅ Running"
    except Exception:
        ui_status = "❌ Not running"

    # Check API key
    api_key = os.getenv("SKYVERN_API_KEY", "")
    api_key_status = "✅ Configured" if api_key else "❌ Not configured"

    # Display status table
    table = Table(title="Skyvern Services")
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Action to Fix", style="yellow")

    table.add_row("Configuration (.env)", env_status, "Run: skyvern init" if env_status.startswith("❌") else "")
    table.add_row("Database", db_status, "Check DATABASE_STRING in .env" if db_status.startswith("❌") else "")
    table.add_row("Server", server_status, "Run: skyvern run server" if server_status.startswith("❌") else "")
    table.add_row("UI", ui_status, "Run: skyvern run ui" if ui_status.startswith("❌") else "")
    table.add_row("API Key", api_key_status, "Run: skyvern init" if api_key_status.startswith("❌") else "")

    console.print(table)

    if "❌" in f"{env_status}{db_status}{server_status}{ui_status}{api_key_status}":
        console.print("\n[bold yellow]Some components need attention.[/] Fix the issues above to get started.")
    else:
        console.print("\n[bold green]All systems operational![/] Skyvern is ready to use.")


@tasks_app.command(name="list")
def list_tasks() -> None:
    """
    List recent Skyvern tasks.
    """
    console.print(Panel.fit("[bold blue]Recent Skyvern Tasks[/]", subtitle="Retrieving task history"))

    try:
        # Initialize Skyvern client
        load_dotenv()
        skyvern_agent = Skyvern(
            base_url=settings.SKYVERN_BASE_URL,
            api_key=settings.SKYVERN_API_KEY,
        )

        # Get tasks
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[green]Fetching recent tasks...", total=1)
            tasks = asyncio.run(skyvern_agent.get_tasks())
            progress.update(task, completed=1)

        if not tasks:
            console.print("[yellow]No tasks found[/]")
            return

        # Display tasks
        table = Table(title=f"Tasks ({len(tasks)} found)")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="blue")

        for task in tasks:
            table.add_row(
                str(task.id),
                task.title or "Untitled",
                task.status or "Unknown",
                task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else "Unknown",
            )

        console.print(table)

        # Show help for next steps
        console.print("\n[bold]Next steps:[/]")
        console.print("• View task details:    [yellow]skyvern tasks show <task_id>[/]")
        console.print("• Retry a task:         [yellow]skyvern tasks retry <task_id>[/]")

    except Exception as e:
        console.print(f"[bold red]Error listing tasks:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")


@tasks_app.command(name="create")
def create_task(
    prompt: str = typer.Option(..., "--prompt", "-p", help="Task prompt"),
    url: str = typer.Option(..., "--url", "-u", help="Starting URL"),
    schema: Optional[str] = typer.Option(None, "--schema", "-s", help="Data extraction schema (JSON)"),
    output_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """
    Create and run a new Skyvern task.
    """
    console.print(Panel.fit("[bold blue]Creating New Skyvern Task[/]", subtitle="Running browser automation"))

    console.print(f"[bold]Prompt:[/] {prompt}")
    console.print(f"[bold]URL:[/] {url}")
    if schema:
        console.print(f"[bold]Schema:[/] {schema}")

    try:
        # Initialize Skyvern client
        load_dotenv()
        skyvern_agent = Skyvern(
            base_url=settings.SKYVERN_BASE_URL,
            api_key=settings.SKYVERN_API_KEY,
        )

        # Create and run task
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[green]Running task...", total=1)

            result = asyncio.run(
                skyvern_agent.run_task(prompt=prompt, url=url, data_extraction_schema=schema, user_agent="skyvern-cli")
            )

            progress.update(task, completed=1)

        # Display result
        if output_json:
            console.print_json(json.dumps(result.model_dump()))
        else:
            console.print("\n[bold green]Task completed successfully![/]")
            console.print(f"\n[bold]Output:[/] {result.model_dump()['output']}")

            # Display path to view results
            base_url = settings.SKYVERN_BASE_URL
            run_history_url = (
                "https://app.skyvern.com/history" if "skyvern.com" in base_url else "http://localhost:8080/history"
            )
            console.print(f"\nView details at: [link={run_history_url}]{run_history_url}[/link]")

    except Exception as e:
        console.print(f"[bold red]Error creating task:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")


@workflows_app.command(name="list")
def list_workflows() -> None:
    """
    List Skyvern workflows.
    """
    console.print(Panel.fit("[bold blue]Skyvern Workflows[/]", subtitle="Retrieving available workflows"))

    try:
        # Initialize Skyvern client
        load_dotenv()
        skyvern_agent = Skyvern(
            base_url=settings.SKYVERN_BASE_URL,
            api_key=settings.SKYVERN_API_KEY,
        )

        # Get workflows
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task("[green]Fetching workflows...", total=1)
            workflows = asyncio.run(skyvern_agent.get_workflows())
            progress.update(task, completed=1)

        if not workflows:
            console.print("[yellow]No workflows found[/]")
            return

        # Display workflows
        table = Table(title=f"Workflows ({len(workflows)} found)")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="blue")

        for workflow in workflows:
            table.add_row(
                str(workflow.id),
                workflow.title or "Untitled",
                workflow.status or "Unknown",
                workflow.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(workflow, "created_at") and workflow.created_at
                else "Unknown",
            )

        console.print(table)

        # Show help for next steps
        console.print("\n[bold]Next steps:[/]")
        console.print("• View workflow details:    [yellow]skyvern workflows show <workflow_id>[/]")
        console.print("• Run a workflow:           [yellow]skyvern workflows run <workflow_id>[/]")

    except Exception as e:
        console.print(f"[bold red]Error listing workflows:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")


# ----------------------------------------------------
# 5. Streamlined Configuration (Original functions enhanced)
# ----------------------------------------------------


def setup_postgresql(no_postgres: bool = False) -> None:
    """Set up PostgreSQL database for Skyvern with improved feedback."""
    console.print(Markdown("## Database Setup"))

    if command_exists("psql") and is_postgres_running():
        console.print("[green]✓[/] PostgreSQL is already running locally")
        if database_exists("skyvern", "skyvern"):
            console.print("[green]✓[/] Database and user exist")
        else:
            console.print("[yellow]![/] Creating database and user...")
            create_database_and_user()
            console.print("[green]✓[/] Database created successfully")
        return

    if no_postgres:
        console.print("[yellow]![/] Skipping PostgreSQL setup as requested")
        console.print("   If using Docker Compose, its Postgres service will start automatically")
        return

    if not is_docker_running():
        console.print("[bold red]×[/] Docker is not running or not installed")
        console.print("   Please install or start Docker and try again")
        exit(1)

    if is_postgres_running_in_docker():
        console.print("[green]✓[/] PostgreSQL is already running in Docker")
    else:
        if not no_postgres:
            console.print("[yellow]![/] No local Postgres detected")
            start_postgres = typer.confirm(
                "Start a disposable container now? (Choose 'n' if using Docker Compose)", default=True
            )

            if not start_postgres:
                console.print("[yellow]![/] Skipping PostgreSQL container setup")
                console.print("   If using Docker Compose, its Postgres service will start automatically")
                return

        console.print("[yellow]![/] Attempting to install PostgreSQL via Docker...")
        if not is_postgres_container_exists():
            run_command(
                "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
            )
        else:
            run_command("docker start postgresql-container")
        console.print("[green]✓[/] PostgreSQL has been installed and started using Docker")

        console.print("[yellow]![/] Waiting for PostgreSQL to start...")
        time.sleep(20)

    # Set up user and database in Docker postgres if needed
    _, code = run_command('docker exec postgresql-container psql -U postgres -c "\\du" | grep -q skyvern', check=False)
    if code == 0:
        console.print("[green]✓[/] Database user exists")
    else:
        console.print("[yellow]![/] Creating database user...")
        run_command("docker exec postgresql-container createuser -U postgres skyvern")

    _, code = run_command(
        "docker exec postgresql-container psql -U postgres -lqt | cut -d \\| -f 1 | grep -qw skyvern", check=False
    )
    if code == 0:
        console.print("[green]✓[/] Database exists")
    else:
        console.print("[yellow]![/] Creating database...")
        run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
        console.print("[green]✓[/] Database and user created successfully")


def setup_llm_providers() -> None:
    """Configure Large Language Model (LLM) Providers with improved UI."""
    console.print(Markdown("## LLM Provider Configuration"))
    console.print("All information provided here will be stored only on your local machine.\n")

    model_options = []

    # Create sections for each provider
    providers: list[dict[str, Any]] = [
        {
            "name": "OpenAI",
            "env_key": "ENABLE_OPENAI",
            "api_key_env": "OPENAI_API_KEY",
            "models": [
                "OPENAI_GPT4_1",
                "OPENAI_GPT4_1_MINI",
                "OPENAI_GPT4_1_NANO",
                "OPENAI_GPT4O",
                "OPENAI_O4_MINI",
                "OPENAI_O3",
            ],
            "setup_message": "To enable OpenAI, you need an API key from your OpenAI account.",
        },
        {
            "name": "Anthropic",
            "env_key": "ENABLE_ANTHROPIC",
            "api_key_env": "ANTHROPIC_API_KEY",
            "models": ["ANTHROPIC_CLAUDE3.5_SONNET", "ANTHROPIC_CLAUDE3.7_SONNET"],
            "setup_message": "To enable Anthropic, you need an API key from your Anthropic account.",
        },
        {
            "name": "Azure OpenAI",
            "env_key": "ENABLE_AZURE",
            "api_key_env": "AZURE_API_KEY",
            "models": ["AZURE_OPENAI_GPT4O"],
            "setup_message": "To enable Azure OpenAI, you need deployment details from your Azure account.",
            "extra_fields": {
                "AZURE_DEPLOYMENT": "Enter your Azure deployment name",
                "AZURE_API_BASE": "Enter your Azure API base URL",
                "AZURE_API_VERSION": "Enter your Azure API version",
            },
        },
        {
            "name": "Google Gemini",
            "env_key": "ENABLE_GEMINI",
            "api_key_env": "GEMINI_API_KEY",
            "models": [
                "GEMINI_FLASH_2_0",
                "GEMINI_FLASH_2_0_LITE",
                "GEMINI_2.5_PRO_PREVIEW_03_25",
                "GEMINI_2.5_PRO_EXP_03_25",
            ],
            "setup_message": "To enable Gemini, you need an API key from Google AI Studio.",
        },
        {
            "name": "Novita AI",
            "env_key": "ENABLE_NOVITA",
            "api_key_env": "NOVITA_API_KEY",
            "models": [
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
            ],
            "setup_message": "To enable Novita AI, you need an API key from Novita.",
        },
        {
            "name": "OpenAI-compatible",
            "env_key": "ENABLE_OPENAI_COMPATIBLE",
            "models": ["OPENAI_COMPATIBLE"],
            "setup_message": "To enable an OpenAI-compatible provider, you need provider-specific details.",
            "extra_fields": {
                "OPENAI_COMPATIBLE_MODEL_NAME": "Enter the model name (e.g., 'yi-34b', 'mistral-large')",
                "OPENAI_COMPATIBLE_API_KEY": "Enter your API key",
                "OPENAI_COMPATIBLE_API_BASE": "Enter the API base URL (e.g., 'https://api.together.xyz/v1')",
            },
            "extra_questions": [
                {
                    "question": "Does this model support vision?",
                    "env_key": "OPENAI_COMPATIBLE_SUPPORTS_VISION",
                    "value_if_yes": "true",
                    "value_if_no": "false",
                }
            ],
            "optional_fields": {"OPENAI_COMPATIBLE_API_VERSION": "Enter API version (optional, press enter to skip)"},
        },
    ]

    # Process each provider
    for provider in providers:
        console.print(f"\n[bold yellow]{provider['name']}[/]")
        console.print(provider["setup_message"])

        enable = typer.confirm(f"Enable {provider['name']}?", default=False)
        update_or_add_env_var(provider["env_key"], "true" if enable else "false")

        if enable:
            # Handle API key (most providers)
            if "api_key_env" in provider:
                api_key = typer.prompt(f"Enter your {provider['name']} API key", hide_input=True)
                if not api_key:
                    console.print(f"[bold red]Error:[/] {provider['name']} API key is required.")
                    console.print(f"{provider['name']} will not be enabled.")
                    update_or_add_env_var(provider["env_key"], "false")
                    continue
                update_or_add_env_var(provider["api_key_env"], api_key)

            # Handle extra fields (Azure, OpenAI-compatible)
            if "extra_fields" in provider:
                field_values = {}
                for env_key, prompt_text in provider["extra_fields"].items():
                    value = typer.prompt(prompt_text)
                    field_values[env_key] = value
                    update_or_add_env_var(env_key, value)

                # Check if all required fields are provided
                if any(not v for v in field_values.values()):
                    console.print(f"[bold red]Error:[/] All {provider['name']} fields must be populated.")
                    console.print(f"{provider['name']} will not be enabled.")
                    update_or_add_env_var(provider["env_key"], "false")
                    continue

            # Handle extra yes/no questions
            if "extra_questions" in provider:
                for question in provider["extra_questions"]:
                    answer = typer.confirm(question["question"], default=False)
                    value = question["value_if_yes"] if answer else question["value_if_no"]
                    update_or_add_env_var(question["env_key"], value)

            # Handle optional fields
            if "optional_fields" in provider:
                for env_key, prompt_text in provider["optional_fields"].items():
                    value = typer.prompt(prompt_text, default="")
                    if value:
                        update_or_add_env_var(env_key, value)

            # Add models to options
            model_options.extend(provider["models"])
            console.print(f"[green]✓[/] {provider['name']} configured successfully")

    # Model Selection
    if not model_options:
        console.print(
            "\n[bold red]Warning:[/] No LLM providers enabled. You won't be able to run Skyvern without a provider."
        )
    else:
        console.print("\n[bold]Available LLM models based on your selections:[/]")
        for i, model in enumerate(model_options, 1):
            console.print(f"  {i}. [cyan]{model}[/]")

        while True:
            try:
                model_choice = typer.prompt(f"Choose a model by number (1-{len(model_options)})", type=int)
                if 1 <= model_choice <= len(model_options):
                    break
                console.print(f"[red]Please enter a number between 1 and {len(model_options)}[/]")
            except ValueError:
                console.print("[red]Please enter a valid number[/]")

        chosen_model = model_options[model_choice - 1]
        console.print(f"[green]✓[/] Model selected: [bold]{chosen_model}[/]")
        update_or_add_env_var("LLM_KEY", chosen_model)

    console.print("[green]✓[/] LLM provider configuration updated in .env")


def setup_browser_config() -> tuple[str, Optional[str], Optional[str]]:
    """Configure browser settings for Skyvern with improved UI."""
    console.print(Markdown("## Browser Configuration"))

    browser_types = [
        {
            "id": "chromium-headless",
            "name": "Headless Chrome",
            "description": "Runs Chrome in the background (no visible window)",
        },
        {"id": "chromium-headful", "name": "Visible Chrome", "description": "Runs Chrome with a visible window"},
        {
            "id": "cdp-connect",
            "name": "Connect to Chrome",
            "description": "Connects to an existing Chrome instance with remote debugging",
        },
    ]

    console.print("Select browser mode:")
    for i, browser in enumerate(browser_types, 1):
        console.print(f"  {i}. [bold]{browser['name']}[/] - {browser['description']}")

    # Get browser choice
    while True:
        try:
            choice = typer.prompt("Enter your choice (1-3)", type=int)
            if 1 <= choice <= len(browser_types):
                selected_browser = browser_types[choice - 1]["id"]
                break
            console.print(f"[red]Please enter a number between 1 and {len(browser_types)}[/]")
        except ValueError:
            console.print("[red]Please enter a valid number[/]")

    browser_location = None
    remote_debugging_url = None

    # Additional configuration for CDP connection
    if selected_browser == "cdp-connect":
        host_system = detect_os()
        default_location = get_default_chrome_location(host_system)

        console.print(f"\n[yellow]Default Chrome location:[/] {default_location}")
        browser_location = typer.prompt("Enter Chrome executable location", default=default_location)

        if not os.path.exists(browser_location):
            console.print(f"[bold yellow]Warning:[/] Chrome not found at {browser_location}")
            console.print("Please verify the location is correct")
            if not typer.confirm("Continue with this path anyway?", default=False):
                return setup_browser_config()  # Start over

        console.print("\n[bold]Chrome Remote Debugging Setup:[/]")
        console.print("Chrome must be running with remote debugging enabled.")
        console.print("Example command: [italic]chrome --remote-debugging-port=9222[/]")

        default_port = "9222"
        remote_debugging_url = f"http://localhost:{default_port}"

        # Check if Chrome is already running with remote debugging
        parsed_url = urlparse(remote_debugging_url)
        version_url = f"{parsed_url.scheme}://{parsed_url.netloc}/json/version"

        console.print(f"\n[yellow]Checking for Chrome on port {default_port}...[/]")
        chrome_running = False

        try:
            response = requests.get(version_url, timeout=2)
            if response.status_code == 200:
                try:
                    browser_info = response.json()
                    console.print("[green]✓[/] Chrome is already running with remote debugging!")
                    if "Browser" in browser_info:
                        console.print(f"   Browser: {browser_info['Browser']}")
                    if "webSocketDebuggerUrl" in browser_info:
                        console.print(f"   WebSocket URL: {browser_info['webSocketDebuggerUrl']}")
                    chrome_running = True
                except json.JSONDecodeError:
                    console.print("[red]Port is in use, but doesn't appear to be Chrome[/]")
        except requests.RequestException:
            console.print(f"[yellow]No Chrome instance detected on {remote_debugging_url}[/]")

        # If Chrome isn't running, offer to start it
        if not chrome_running:
            if host_system == "darwin" or host_system == "linux":
                chrome_cmd = f'{browser_location} --remote-debugging-port={default_port} --user-data-dir="$HOME/chrome-cdp-profile" --no-first-run --no-default-browser-check'
            elif host_system == "windows" or host_system == "wsl":
                chrome_cmd = f'"{browser_location}" --remote-debugging-port={default_port} --user-data-dir="C:\\chrome-cdp-profile" --no-first-run --no-default-browser-check'
            else:
                console.print("[red]Unsupported OS for Chrome configuration[/]")
                chrome_cmd = ""

            if chrome_cmd:
                console.print(f"\nCommand to start Chrome: [yellow]{chrome_cmd}[/]")

                if typer.confirm("Start Chrome with remote debugging now?", default=True):
                    console.print(f"[yellow]Starting Chrome with remote debugging on port {default_port}...[/]")
                    try:
                        if host_system in ["darwin", "linux"]:
                            subprocess.Popen(f"nohup {chrome_cmd} > /dev/null 2>&1 &", shell=True)
                        elif host_system == "windows":
                            subprocess.Popen(f"start {chrome_cmd}", shell=True)
                        elif host_system == "wsl":
                            subprocess.Popen(f"cmd.exe /c start {chrome_cmd}", shell=True)

                        console.print("Chrome starting...")
                        console.print(f"Connecting to {remote_debugging_url}")

                        # Wait for Chrome to start and verify connection
                        with Progress(
                            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
                        ) as progress:
                            wait_task = progress.add_task("[green]Waiting for Chrome to initialize...", total=1)
                            time.sleep(2)
                            progress.update(wait_task, completed=1)

                        try:
                            verification_response = requests.get(version_url, timeout=5)
                            if verification_response.status_code == 200:
                                try:
                                    browser_info = verification_response.json()
                                    console.print(
                                        "[green]✓[/] Connection verified! Chrome is running with remote debugging"
                                    )
                                    if "Browser" in browser_info:
                                        console.print(f"   Browser: {browser_info['Browser']}")
                                except json.JSONDecodeError:
                                    console.print(
                                        "[yellow]Warning:[/] Response from Chrome debugging port is not valid JSON"
                                    )
                            else:
                                console.print(
                                    f"[yellow]Warning:[/] Chrome responded with status code {verification_response.status_code}"
                                )
                        except requests.RequestException as e:
                            console.print(f"[yellow]Warning:[/] Could not verify Chrome is running: {e}")
                            console.print("You may need to check Chrome manually or try a different port")
                    except Exception as e:
                        console.print(f"[red]Error starting Chrome:[/] {e}")
                        console.print("Please start Chrome manually using the command above")

        # Get the debugging URL
        custom_url = typer.prompt("Enter remote debugging URL", default=remote_debugging_url)
        if custom_url:
            remote_debugging_url = custom_url

    console.print(f"\n[green]✓[/] Browser configuration complete: [bold]{selected_browser}[/]")
    return selected_browser, browser_location, remote_debugging_url


def command_exists(command: str) -> bool:
    """Check if a command exists on the system."""
    return shutil.which(command) is not None


def run_command(command: str, check: bool = True) -> tuple[Optional[str], Optional[int]]:
    """Run a shell command and return the output and return code."""
    try:
        result = subprocess.run(command, shell=True, check=check, capture_output=True, text=True)
        return result.stdout.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        return None, e.returncode


def is_postgres_running() -> bool:
    """Check if PostgreSQL is running locally."""
    if command_exists("pg_isready"):
        result, _ = run_command("pg_isready")
        return result is not None and "accepting connections" in result
    return False


def database_exists(dbname: str, user: str) -> bool:
    """Check if a PostgreSQL database exists."""
    check_db_command = f'psql {dbname} -U {user} -c "\\q"'
    output, _ = run_command(check_db_command, check=False)
    return output is not None


def create_database_and_user() -> None:
    """Create PostgreSQL database and user for Skyvern."""
    run_command("createuser skyvern")
    run_command("createdb skyvern -O skyvern")


def is_docker_running() -> bool:
    """Check if Docker is running."""
    if not command_exists("docker"):
        return False
    _, code = run_command("docker info", check=False)
    return code == 0


def is_postgres_running_in_docker() -> bool:
    """Check if PostgreSQL is running in Docker."""
    _, code = run_command("docker ps | grep -q postgresql-container", check=False)
    return code == 0


def is_postgres_container_exists() -> bool:
    """Check if PostgreSQL Docker container exists."""
    _, code = run_command("docker ps -a | grep -q postgresql-container", check=False)
    return code == 0


def update_or_add_env_var(key: str, value: str) -> None:
    """Update or add environment variable in .env file with better handling."""
    env_path = Path(".env")
    if not env_path.exists():
        env_path.touch()
        # Write default environment variables
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

    # Load environment to get current values
    load_dotenv(env_path)
    current_value = os.getenv(key)

    # Only update if value is different
    if current_value != value:
        set_key(env_path, key, value)
        # Also update in current environment
        os.environ[key] = value


async def _setup_local_organization() -> str:
    """Set up and return the API key for the local organization."""
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


def setup_mcp() -> None:
    """Configure MCP for different Skyvern deployments."""
    host_system = detect_os()
    path_to_env = setup_mcp_config()

    # Configure integrations
    integrations = [
        {
            "name": "Claude Desktop",
            "check_fn": lambda: is_claude_desktop_installed(host_system),
            "setup_fn": lambda: setup_claude_desktop_config(host_system, path_to_env),
            "not_installed_msg": "Claude Desktop is not installed. Please install it first.",
        },
        {
            "name": "Cursor Editor",
            "check_fn": lambda: is_cursor_installed(host_system),
            "setup_fn": lambda: setup_cursor_config(host_system, path_to_env),
            "not_installed_msg": "Cursor Editor is not installed. Please install it first.",
        },
        {
            "name": "Windsurf",
            "check_fn": lambda: is_windsurf_installed(host_system),
            "setup_fn": lambda: setup_windsurf_config(host_system, path_to_env),
            "not_installed_msg": "Windsurf is not installed. Please install it first.",
        },
    ]

    # Set up each integration
    for integration in integrations:
        console.print(f"\n[bold]Setting up {integration['name']}[/]")

        # Check if installed
        check_fn = cast(Callable[[], bool], integration["check_fn"])
        if not check_fn():
            console.print(f"[yellow]![/] {integration['not_installed_msg']}")
            console.print(f"Skipping {integration['name']} integration setup.")
            continue

        # Ask user if they want to set up this integration
        if typer.confirm(f"Configure {integration['name']} integration?", default=True):
            # Set up the integration
            setup_fn = cast(Callable[[], bool], integration["setup_fn"])
            if setup_fn():
                console.print(f"[green]✓[/] {integration['name']} integration configured successfully")
            else:
                console.print(f"[red]×[/] Error configuring {integration['name']} integration")
        else:
            console.print(f"Skipping {integration['name']} integration setup")

    console.print("\n[green]✓[/] MCP integration setup complete")


def setup_mcp_config() -> str:
    """Find or prompt for the Python executable path and return it."""
    # Try to find Python
    python_paths = []
    for python_cmd in ["python", "python3.11"]:
        python_path = shutil.which(python_cmd)
        if python_path:
            python_paths.append((python_cmd, python_path))

    if not python_paths:
        console.print("[yellow]![/] Could not find Python 3.11 installation")
        path_to_env = typer.prompt(
            "Enter the full path to your Python 3.11 environment", default="/opt/homebrew/bin/python3.11"
        )
    else:
        # Show found Python installations
        console.print("[green]✓[/] Found Python installations:")
        for i, (cmd, path) in enumerate(python_paths, 1):
            console.print(f"  {i}. {cmd}: {path}")

        # Use the first one as default
        _, default_path = python_paths[0]
        path_to_env = default_path

        if len(python_paths) > 1:
            # Let user choose if multiple were found
            choice = typer.prompt("Which Python installation do you want to use? (Enter number)", default="1")
            try:
                index = int(choice) - 1
                if 0 <= index < len(python_paths):
                    _, path_to_env = python_paths[index]
            except ValueError:
                console.print(f"[yellow]![/] Invalid choice, using default: {path_to_env}")

    return path_to_env


def is_claude_desktop_installed(host_system: str) -> bool:
    """Check if Claude Desktop is installed."""
    try:
        config_path = os.path.dirname(get_claude_config_path(host_system))
        return os.path.exists(config_path)
    except Exception:
        return False


def get_claude_config_path(host_system: str) -> str:
    """Get the Claude Desktop config file path."""
    if host_system == "wsl":
        roaming_path = get_windows_appdata_roaming()
        if roaming_path is None:
            raise RuntimeError("Could not locate Windows AppData\\Roaming path from WSL")
        return os.path.join(str(roaming_path), ".cursor", "mcp.json")

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


def setup_claude_desktop_config(host_system: str, path_to_env: str) -> bool:
    """Set up Claude Desktop configuration."""
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
            console.print("[red]×[/] SKYVERN_BASE_URL and SKYVERN_API_KEY must be set in .env file")
            return False

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

        return True
    except Exception as e:
        console.print(f"[red]×[/] Error configuring Claude Desktop: {e}")
        return False


def is_cursor_installed(host_system: str) -> bool:
    """Check if Cursor is installed."""
    try:
        config_dir = os.path.expanduser("~/.cursor")
        return os.path.exists(config_dir)
    except Exception:
        return False


def setup_cursor_config(host_system: str, path_to_env: str) -> bool:
    """Placeholder setup for Cursor integration."""
    console.print("[yellow]![/] Cursor integration setup is not implemented yet")
    return False


def is_windsurf_installed(host_system: str) -> bool:
    """Check if Windsurf is installed."""
    # TODO: Implement actual detection logic
    return False


def setup_windsurf_config(host_system: str, path_to_env: str) -> bool:
    """Placeholder setup for Windsurf integration."""
    console.print("[yellow]![/] Windsurf integration setup is not implemented yet")
    return False
