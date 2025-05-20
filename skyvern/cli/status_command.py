import os
from pathlib import Path

from dotenv import load_dotenv
from rich.panel import Panel
from rich.table import Table

from skyvern.utils import migrate_db

from .common import console


def status() -> None:
    """Check the status of Skyvern services."""
    console.print(Panel.fit("[bold blue]Skyvern Services Status[/]", subtitle="Checking all system components"))
    env_path = Path(".env")
    env_status = "✅ Found" if env_path.exists() else "❌ Not found"
    db_status = "⏳ Checking..."
    try:
        load_dotenv()
        migrate_db()
        db_status = "✅ Connected"
    except Exception:
        db_status = "❌ Not connected"
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
    api_key = os.getenv("SKYVERN_API_KEY", "")
    api_key_status = "✅ Configured" if api_key else "❌ Not configured"
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

