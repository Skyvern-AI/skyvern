import asyncio
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, overload

import typer
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt

from skyvern.utils.env_paths import (
    BACKEND_ENV_FILE_ENV_VAR,
    EnvIntent,
    EnvScope,
    env_scope_label,
    parse_env_scope,
    resolve_backend_env_path,
)

from .browser import setup_browser_config
from .console import console
from .database import setup_postgresql
from .llm_setup import setup_llm_providers, update_or_add_env_var
from .masked_prompt import ask_secret

PLAYWRIGHT_CHROMIUM_BROWSER_TYPES = {"chromium-headful", "chromium-headless"}


@dataclass
class BrowserInstallStatus:
    """Status for the optional Playwright-managed Chromium install step."""

    required: bool
    ready: bool
    skipped: bool = False
    already_installed: bool = False
    attempted: bool = False
    reason: str | None = None
    error: str | None = None


@dataclass
class InitEnvResult:
    """Result of the interactive initialization flow."""

    run_local: bool
    browser_type: str | None = None
    browser_install: BrowserInstallStatus = field(
        default_factory=lambda: BrowserInstallStatus(required=False, ready=True)
    )

    def __bool__(self) -> bool:
        """Preserve bool-like behavior for existing callers."""
        return self.run_local


def capture_setup_event(
    event_name: str,
    success: bool = True,
    error_type: str | None = None,
    error_message: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> None:
    from skyvern.analytics import capture_setup_event as _capture_setup_event  # noqa: PLC0415

    _capture_setup_event(event_name, success, error_type, error_message, extra_data)


def _browser_type_requires_playwright_chromium(browser_type: str | None) -> bool:
    return browser_type in PLAYWRIGHT_CHROMIUM_BROWSER_TYPES


def _playwright_chromium_executable_path() -> Path | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path)
    except Exception:
        return None


def _is_playwright_chromium_installed() -> bool:
    executable_path = _playwright_chromium_executable_path()
    return executable_path is not None and executable_path.exists()


def _format_subprocess_error(error: subprocess.CalledProcessError) -> str:
    stderr = (error.stderr or "").strip()
    if stderr:
        return stderr
    stdout = (error.stdout or "").strip()
    if stdout:
        return stdout
    return str(error)


def _ensure_playwright_chromium(browser_type: str | None, skip_browser_install: bool) -> BrowserInstallStatus:
    """Install Playwright Chromium when the selected browser mode needs it."""
    if not _browser_type_requires_playwright_chromium(browser_type):
        reason = f"browser mode is {browser_type or 'not set'}"
        console.print(f"[green]Skipping Chromium installation because {reason}.[/green]")
        return BrowserInstallStatus(required=False, ready=True, skipped=True, reason=reason)

    if _is_playwright_chromium_installed():
        console.print("[green]Playwright Chromium is already installed. Skipping download.[/green]")
        return BrowserInstallStatus(required=True, ready=True, skipped=True, already_installed=True)

    if skip_browser_install:
        console.print("⏭️ [yellow]Skipping Chromium installation as requested.[/yellow]")
        return BrowserInstallStatus(
            required=True,
            ready=False,
            skipped=True,
            reason="--skip-browser-install was provided",
        )

    console.print("\n⬇️ [bold blue]Installing Chromium browser...[/bold blue]")
    capture_setup_event("playwright-install-start")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
    ) as progress:
        progress.add_task("[bold blue]Downloading Chromium, this may take a moment...", total=None)
        try:
            subprocess.run(["playwright", "install", "chromium"], check=True, capture_output=True, text=True)
            capture_setup_event("playwright-install-complete", success=True)
            console.print("✅ [green]Chromium installation complete.[/green]")
            return BrowserInstallStatus(required=True, ready=True, attempted=True)
        except subprocess.CalledProcessError as e:
            error_message = _format_subprocess_error(e)
            capture_setup_event(
                "playwright-install-fail",
                success=False,
                error_type="playwright_install_error",
                error_message=error_message,
            )
            console.print(
                Panel(
                    "[bold yellow]Chromium installation did not complete.[/bold yellow]\n\n"
                    "The rest of your Skyvern setup has been saved. Browser modes that launch "
                    "Playwright-managed Chromium will not be ready until this is fixed.\n\n"
                    "Run this command to finish the browser install:\n"
                    "[cyan]playwright install chromium[/cyan]",
                    border_style="yellow",
                )
            )
            return BrowserInstallStatus(
                required=True,
                ready=False,
                attempted=True,
                error=error_message,
            )


def _init_return_value(result: InitEnvResult, return_result: bool) -> bool | InitEnvResult:
    if return_result:
        return result
    return result.run_local


def _parse_env_scope(value: str) -> EnvScope:
    try:
        return parse_env_scope(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _select_backend_env_scope(*, run_local: bool, env_scope: str | None) -> EnvScope:
    if run_local:
        if env_scope is None:
            return EnvScope.LEGACY
        selected_scope = _parse_env_scope(env_scope)
        if selected_scope is not EnvScope.LEGACY:
            raise typer.BadParameter(
                "Self-hosted local server setup writes ./.env. Project/global scopes are for cloud/API config."
            )
        return selected_scope

    if env_scope is not None:
        return _parse_env_scope(env_scope)

    default_scope = EnvScope.GLOBAL
    console.print("\n[bold blue]Backend config location[/bold blue]")
    console.print("  [cyan]1.[/cyan] Current directory (./.env)")
    console.print("  [cyan]2.[/cyan] Project directory (./.skyvern/.env)")
    console.print("  [cyan]3.[/cyan] Global user directory (~/.skyvern/.env)")
    while True:
        try:
            selected = Prompt.ask(
                "Where should Skyvern store backend config?",
                default="3",
            )
        except EOFError:
            return default_scope
        try:
            return _parse_env_scope(selected)
        except typer.BadParameter as exc:
            console.print(f"[red]{exc.message}[/red]")


@overload
def init_env(
    no_postgres: bool = False,
    database_string: str = "",
    skip_browser_install: bool = False,
    env_scope: str | None = None,
    env_path: Path | str | None = None,
    return_result: Literal[False] = False,
) -> bool: ...


@overload
def init_env(
    no_postgres: bool = False,
    database_string: str = "",
    skip_browser_install: bool = False,
    env_scope: str | None = None,
    env_path: Path | str | None = None,
    return_result: Literal[True] = True,
) -> InitEnvResult: ...


def init_env(
    no_postgres: bool = False,
    database_string: str = "",
    skip_browser_install: bool = False,
    env_scope: str | None = None,
    env_path: Path | str | None = None,
    return_result: bool = False,
) -> bool | InitEnvResult:
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
    result = InitEnvResult(run_local=run_local)
    selected_env_scope = _select_backend_env_scope(run_local=run_local, env_scope=env_scope)
    backend_env_path = (
        Path(env_path).expanduser()
        if env_path is not None
        else resolve_backend_env_path(
            intent=EnvIntent.SERVER if run_local else EnvIntent.CLOUD,
            scope=selected_env_scope,
            for_write=True,
        )
    )
    os.environ[BACKEND_ENV_FILE_ENV_VAR] = str(backend_env_path)
    console.print(f"[dim]Backend config: {env_scope_label(selected_env_scope)} -> {backend_env_path}[/dim]")

    def set_env_var(key: str, value: str) -> None:
        update_or_add_env_var(key, value, env_path=backend_env_path)

    if run_local:
        if database_string:
            console.print("🔗 [bold blue]Using custom database connection...[/bold blue]")
            set_env_var("DATABASE_STRING", database_string)
            console.print(f"✅ [green]Database connection string set in {backend_env_path}.[/green]")
        else:
            setup_postgresql(no_postgres, env_path=backend_env_path)
        console.print("📊 [bold blue]Running database migrations...[/bold blue]")
        from skyvern.utils import migrate_db  # noqa: PLC0415

        migrate_db()
        console.print("✅ [green]Database migration complete.[/green]")

        console.print("🔑 [bold blue]Generating local organization API key...[/bold blue]")
        from skyvern.forge.forge_app_initializer import start_forge_app  # noqa: PLC0415

        from .mcp import setup_local_organization  # noqa: PLC0415

        start_forge_app()
        api_key = asyncio.run(setup_local_organization())
        if api_key:
            console.print("✅ [green]Local organization API key generated.[/green]")
        else:
            console.print("[red]Failed to generate local organization API key. Please check server logs.[/red]")

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
                setup_llm_providers(env_path=backend_env_path)
        else:
            console.print("\n[bold blue]Initializing .env file...[/bold blue]")
            setup_llm_providers(env_path=backend_env_path)

        console.print("\n[bold blue]Configuring browser settings...[/bold blue]")
        browser_type, browser_location, remote_debugging_url = setup_browser_config()
        result.browser_type = browser_type
        set_env_var("BROWSER_TYPE", browser_type)
        if browser_location:
            set_env_var("CHROME_EXECUTABLE_PATH", browser_location)
        if remote_debugging_url:
            set_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)
        set_env_var("BROWSER_STREAMING_MODE", "cdp")
        console.print("✅ [green]Browser configuration complete.[/green]")

        console.print("🌐 [bold blue]Setting Skyvern Base URL to: http://localhost:8000[/bold blue]")
        set_env_var("SKYVERN_BASE_URL", "http://localhost:8000")

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
        api_key = None

        auth_method = Prompt.ask(
            "Authenticate via [bold blue]browser[/bold blue] (recommended) or paste an [bold yellow]api-key[/bold yellow] manually?",
            choices=["browser", "api-key"],
            default="browser",
        )

        if auth_method == "browser":
            from .auth_command import run_signup

            frontend_url = Prompt.ask(
                "Frontend URL",
                default="https://app.skyvern.com",
                show_default=True,
            )
            run_signup(base_url=frontend_url, env_path=backend_env_path)
            api_key = None  # already saved by browser_auth
        else:
            base_url = Prompt.ask("Enter Skyvern base URL", default="https://api.skyvern.com", show_default=True)
            if not base_url:
                base_url = "https://api.skyvern.com"

            console.print("\n[bold]To get your API key:[/bold]")
            console.print("1. Create an account at [link]https://app.skyvern.com[/link]")
            console.print("2. Go to [bold cyan]Settings[/bold cyan]")
            console.print("3. [bold green]Copy your API key[/bold green]")
            api_key = ask_secret("Enter your Skyvern API key")
            if not api_key:
                console.print("[red]API key is required.[/red]")
                api_key = ask_secret("Please re-enter your Skyvern API key")
                if not api_key:
                    console.print("[bold red]Error: API key cannot be empty. Aborting initialization.[/bold red]")
                    return _init_return_value(result, return_result)
            set_env_var("SKYVERN_BASE_URL", base_url)

        console.print(
            "\n[bold yellow]Tip:[/bold yellow] Want Skyvern Cloud to use your local browser "
            "(with your existing cookies, logins, and extensions)?"
        )
        console.print("  Run: [reverse green] skyvern browser serve --tunnel [/reverse green]")
        console.print("  This starts Chrome on your machine and creates a tunnel so Skyvern Cloud can control it.")
        console.print("  Learn more: [link]https://www.skyvern.com/docs/optimization/browser-tunneling[/link]")

    analytics_id_input = Prompt.ask("Please enter your email for analytics (press enter to skip)", default="")
    analytics_id = analytics_id_input if analytics_id_input else str(uuid.uuid4())
    set_env_var("ANALYTICS_ID", analytics_id)
    if api_key:
        set_env_var("SKYVERN_API_KEY", api_key)
    console.print(f"✅ [green]{backend_env_path} file has been initialized.[/green]")

    # Retrieve browser config for MCP setup (set during local init)
    _mcp_browser_type = os.environ.get("BROWSER_TYPE") if run_local else None
    _mcp_browser_url = os.environ.get("BROWSER_REMOTE_DEBUGGING_URL") if run_local else None

    if Confirm.ask("\nWould you like to [bold yellow]configure the MCP server[/bold yellow]?", default=True):
        from .mcp import setup_mcp  # noqa: PLC0415

        setup_mcp(
            local=run_local,
            browser_type=_mcp_browser_type,
            browser_remote_debugging_url=_mcp_browser_url,
        )

        if not run_local:
            console.print(
                "\n🎉 [bold green]MCP configuration is complete! Your AI applications are now ready to use Skyvern Cloud.[/bold green]"
            )

    if run_local:
        result.browser_install = _ensure_playwright_chromium(result.browser_type, skip_browser_install)

        console.print("\n🎉 [bold green]Skyvern setup complete![/bold green]")
        capture_setup_event("init-complete", success=True, extra_data={"mode": "local"})
        console.print("[bold]To start using Skyvern, run:[/bold]")
        console.print(Padding("skyvern run server", (1, 4), style="reverse green"))

    return _init_return_value(result, return_result)


def init_app_factory() -> typer.Typer:
    """Build and return the ``init`` sub-app with its callback and browser sub-command.

    This factory is called lazily by :class:`LazyTyperGroup` so that the heavy
    imports in this module are deferred until the user actually runs
    ``skyvern init``.
    """
    app = typer.Typer(
        invoke_without_command=True,
        help="Interactively configure Skyvern and its dependencies.",
    )

    @app.callback()
    def _init_callback(
        ctx: typer.Context,
        no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
        database_string: str = typer.Option(
            "",
            "--database-string",
            help="Custom database connection string (e.g., postgresql+psycopg://user:password@host:port/dbname). When provided, skips Docker PostgreSQL setup.",
        ),
        env_scope: str | None = typer.Option(
            None,
            "--env-scope",
            help="Backend env location: legacy/current, project, or global.",
        ),
    ) -> None:
        """Run full initialization when no subcommand is provided."""
        if ctx.invoked_subcommand is None:
            init_env(no_postgres=no_postgres, database_string=database_string, env_scope=env_scope)

    @app.command(name="browser")
    def _init_browser_command() -> None:
        """Initialize only the browser configuration."""
        init_browser()

    return app


def init_browser() -> None:
    """Initialize only the browser configuration and install Chromium."""
    console.print("\n[bold blue]Configuring browser settings...[/bold blue]")
    capture_setup_event("browser-config-start")
    browser_type, browser_location, remote_debugging_url = setup_browser_config()
    update_or_add_env_var("BROWSER_TYPE", browser_type)
    if browser_location:
        update_or_add_env_var("CHROME_EXECUTABLE_PATH", browser_location)
    if remote_debugging_url:
        update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", remote_debugging_url)
    update_or_add_env_var("BROWSER_STREAMING_MODE", "cdp")
    capture_setup_event(
        "browser-config-complete",
        success=True,
        extra_data={"browser_type": browser_type, "has_custom_path": browser_location is not None},
    )
    console.print("✅ [green]Browser configuration complete.[/green]")

    _ensure_playwright_chromium(browser_type, skip_browser_install=False)
