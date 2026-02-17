from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()


def output(
    data: Any,
    *,
    action: str = "",
    json_mode: bool = False,
) -> None:
    if json_mode:
        envelope: dict[str, Any] = {"ok": True, "action": action, "data": data, "error": None}
        json.dump(envelope, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        table = Table()
        for key in data[0]:
            table.add_column(key.replace("_", " ").title())
        for row in data:
            table.add_row(*[str(v) for v in row.values()])
        console.print(table)
    elif isinstance(data, dict):
        for key, value in data.items():
            console.print(f"[bold]{key}:[/bold] {value}")
    else:
        console.print(str(data))


def output_error(message: str, *, hint: str = "", json_mode: bool = False, exit_code: int = 1) -> None:
    if json_mode:
        envelope: dict[str, Any] = {
            "ok": False,
            "action": "",
            "data": None,
            "error": {"message": message, "hint": hint},
        }
        json.dump(envelope, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        raise SystemExit(exit_code)
    console.print(f"[red]Error: {message}[/red]")
    if hint:
        console.print(f"[yellow]Hint: {hint}[/yellow]")
    raise SystemExit(exit_code)
