import asyncio
import subprocess
import uuid

import typer
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt

from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.utils import migrate_db
from skyvern.utils.env_paths import resolve_backend_env_path

from .browser import setup_browser_config
from .console import console
from .database import setup_postgresql
from .llm_setup import setup_llm_providers, update_or_add_env_var
from .mcp import setup_local_organization, setup_mcp


def init_env(
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
) -> bool:
    """Interactive initialization command for Skyvern."""
    console.print(
        Panel(
            "[bold green]Welcome to Skyvern CLI Initialization![/bold green]",
            border_style="green",
            expand=False,
        )
    )
    console.print("[italic]This wizard will help you set up Skyvern.[/italic]")

    infra_choice = Prompt.ask(
        "Would you like to run Skyvern [bold blue]local[/bold blue]ly or in the [bold purple]cloud[/bold purple]?",
        choices=["local", "cloud"],
    )

    run_local = infra_choice == "local"

    if run_local:
        setup_postgresql(no_postgres)
        console.print("📊 [bold blue]Running database migrations...[/bold blue]")
        migrate_db()
        console.print("✅ [green]Database migration complete.[/green]")

        console.print("🔑 [bold blue]Generating local organization API key...[/bold blue]")
        start_forge_app()
        api_key = asyncio.run(setup_local_organization())
        if api_key:
            console.print("✅ [green]Local organization API key generated.[/green]")
        else:
            console.print("[red]Failed to generate local organization API key. Please check server logs.[/red]")

        backend_env_path = resolve_backend_env_path()
        if backend_env_path.exists():
            console.print(f"💡 [{backend_env_path}] file already exists.", style="yellow", markup=False)
            redo_llm_setup = Confirm.ask(
                "Do you want to go through [bold yellow]LLM provider setup again[/bold yellow]?",
                default=False,
            )
            if not redo_llm_setup:
                console.print("[green]Skipping LLM setup.[/green]")
            else:
                console.print("\n[bold blue]Initializing .env file for LLM providers...[/bold blue]")
                setup_llm_providers()
        else:
            console.print("\n[bold blue]Initializing .env file...[/bold blue]")
            setup_llm_providers()

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

        console.print("\n[bold yellow]To run Skyvern you can either:[/bold yellow]")
        console.print("• [green]skyvern run server[/green]  (reuses the DB we just created)")
        console.print(
            "• [cyan]docker compose up -d[/cyan]  (starts a new Postgres inside Compose; you may stop the first container with: [magenta]docker rm -f postgresql-container[/magenta])"
        )
        console.print(
            "\n[italic]Only one Postgres container can run on the host's port 5432 at a time. If you switch to Docker Compose, remove the original with:[/italic] [magenta]docker rm -f postgresql-container[/magenta]"
        )
    else:
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
                return False
        update_or_add_env_var("SKYVERN_BASE_URL", base_url)

    analytics_id_input = Prompt.ask("Please enter your email for analytics (press enter to skip)", default="")
    analytics_id = analytics_id_input if analytics_id_input else str(uuid.uuid4())
    update_or_add_env_var("ANALYTICS_ID", analytics_id)
    update_or_add_env_var("SKYVERN_API_KEY", api_key)
    console.print(f"✅ [green]{resolve_backend_env_path()} file has been initialized.[/green]")

    if Confirm.ask("\nWould you like to [bold yellow]configure the MCP server[/bold yellow]?", default=True):
        setup_mcp()

        if not run_local:
            console.print(
                "\n🎉 [bold green]MCP configuration is complete! Your AI applications are now ready to use Skyvern Cloud.[/bold green]"
            )

    if run_local:
        console.print("\n⬇️ [bold blue]Installing Chromium browser...[/bold blue]")
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
        ) as progress:
            progress.add_task("[bold blue]Downloading Chromium, this may take a moment...", total=None)
            subprocess.run(["playwright", "install", "chromium"], check=True)
        console.print("✅ [green]Chromium installation complete.[/green]")

        console.print("\n🎉 [bold green]Skyvern setup complete![/bold green]")
        console.print("[bold]To start using Skyvern, run:[/bold]")
        console.print(Padding("skyvern run server", (1, 4), style="reverse green"))

    return run_local


def init_browser() -> None:
    """Initialize only the browser configuration and install Chromium."""
    console.print("\n[bold blue]Configuring browser settings...[/bold blue]")
    browser_type, browser_location, remote_debugging_url = setup_browser_config()
    update_or_add_env_var("BROWSER_TYPE", browser_type)
    if browser_location:
        update_or_add_env_var("CHROME_EXECUTABLE_PATH", browser_location)
    if remote_debugging_url:
        update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)
    console.print("✅ [green]Browser configuration complete.[/green]")

    console.print("\n⬇️ [bold blue]Installing Chromium browser...[/bold blue]")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
    ) as progress:
        progress.add_task("[bold blue]Downloading Chromium, this may take a moment...", total=None)
        subprocess.run(["playwright", "install", "chromium"], check=True)
    console.print("✅ [green]Chromium installation complete.[/green]")
