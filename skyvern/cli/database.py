import asyncio
from pathlib import Path

import aiosqlite
from rich.panel import Panel

from .console import console


async def _initialize_sqlite(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.commit()


def setup_sqlite() -> None:
    """Create the SQLite database and apply recommended PRAGMA settings."""
    console.print(Panel("[bold cyan]SQLite Setup[/bold cyan]", border_style="blue"))
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    db_file = data_dir / "skyvern.db"
    if db_file.exists():
        console.print("✅ [green]SQLite database already exists.[/green]")
    else:
        console.print("🚀 [bold green]Creating SQLite database...[/bold green]")
    asyncio.run(_initialize_sqlite(db_file))
    console.print(f"✅ [green]Database ready at {db_file}[/green]")
