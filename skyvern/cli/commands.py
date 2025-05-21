import sys
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

# Configure async for Windows
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Initialize Rich console for better formatting
console = Console()

# Main application
cli_app = typer.Typer(
    help="Skyvern - Browser automation powered by LLMs and Computer Vision",
    add_completion=False,
)

from .docs import docs_app
from .init_command import init, init_browser, init_mcp
from .run_commands import run_app
from .setup_commands import setup_mcp_command
from .tasks import tasks_app
from .workflow import workflow_app

cli_app = typer.Typer()
cli_app.add_typer(run_app, name="run")
cli_app.add_typer(workflow_app, name="workflow")
cli_app.add_typer(tasks_app, name="tasks")
cli_app.add_typer(docs_app, name="docs")
setup_app = typer.Typer()
cli_app.add_typer(setup_app, name="setup")
init_app = typer.Typer(invoke_without_command=True)
cli_app.add_typer(init_app, name="init")

setup_app.command(name="mcp")(setup_mcp_command)


@init_app.callback()
def init_callback(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
) -> None:
    """Run full initialization when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        init(no_postgres=no_postgres)


@init_app.command(name="browser")
def init_browser_command() -> None:
    """Initialize only the browser configuration."""
    init_browser()


@init_app.command(name="mcp")
def init_mcp_command() -> None:
    """Initialize only the MCP server configuration."""
    init_mcp()


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv()
    cli_app()
