from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable, Coroutine, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from skyvern.cli.core.telemetry import capture_cli_tool_call

console = Console()

ENVELOPE_SCHEMA_VERSION = "1.0"


def output(
    data: Any,
    *,
    action: str = "",
    json_mode: bool = False,
) -> None:
    if json_mode:
        envelope: dict[str, Any] = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "ok": True,
            "action": action,
            "data": data,
            "error": None,
            "warnings": [],
            "browser_context": None,
            "artifacts": None,
            "timing_ms": None,
        }
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


def output_error(
    message: str,
    *,
    hint: str = "",
    action: str = "",
    json_mode: bool = False,
    exit_code: int = 1,
) -> NoReturn:
    if json_mode:
        envelope: dict[str, Any] = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "ok": False,
            "action": action,
            "data": None,
            "error": {"message": message, "hint": hint},
            "warnings": [],
            "browser_context": None,
            "artifacts": None,
            "timing_ms": None,
        }
        json.dump(envelope, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        raise SystemExit(exit_code)
    console.print(f"[red]Error: {message}[/red]")
    if hint:
        console.print(f"[yellow]Hint: {hint}[/yellow]")
    raise SystemExit(exit_code)


def emit_tool_result(
    result: dict[str, Any],
    *,
    json_output: bool,
    action: str | None = None,
    telemetry_tool_name: str | None = None,
) -> None:
    """Emit an MCP tool result, preserving the full MCP envelope shape in JSON mode."""
    if telemetry_tool_name is not None:
        capture_cli_tool_call(telemetry_tool_name, ok=bool(result.get("ok", False)))

    if json_output:
        envelope = {**result}
        envelope.setdefault("schema_version", ENVELOPE_SCHEMA_VERSION)
        envelope.setdefault("warnings", [])
        envelope.setdefault("browser_context", None)
        envelope.setdefault("artifacts", None)
        envelope.setdefault("timing_ms", None)
        json.dump(envelope, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        if not result.get("ok", False):
            raise SystemExit(1)
        return

    if result.get("ok", False):
        output(result.get("data"), action=action or str(result.get("action", "")), json_mode=False)
        return

    err = result.get("error") or {}
    output_error(
        str(err.get("message") or "Unknown error"),
        hint=str(err.get("hint") or ""),
        action=action or str(result.get("action") or ""),
        json_mode=False,
    )


def run_tool(
    runner: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    json_output: bool,
    hint_on_exception: str,
    action: str = "",
    telemetry_tool_name: str | None = None,
) -> None:
    """Run an async MCP tool and emit the result."""
    try:
        result: dict[str, Any] = asyncio.run(runner())
        emit_tool_result(result, json_output=json_output, telemetry_tool_name=telemetry_tool_name)
    except typer.BadParameter:
        raise
    except Exception as e:
        if telemetry_tool_name is not None:
            capture_cli_tool_call(telemetry_tool_name, ok=False, error=e)
        output_error(str(e), hint=hint_on_exception, action=action, json_mode=json_output)


def resolve_inline_or_file(value: str | None, *, param_name: str) -> str | None:
    """Resolve a value that may be a literal string or an @file reference."""
    if value is None or not value.startswith("@"):
        return value

    file_path = value[1:]
    if not file_path:
        raise typer.BadParameter(f"{param_name} file path cannot be empty after '@'.")

    path = Path(file_path).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise typer.BadParameter(f"Unable to read {param_name} file '{path}': {e}") from e
