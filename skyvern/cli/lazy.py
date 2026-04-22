"""Lazy-loading infrastructure for the Skyvern CLI.

Provides ``LazyTyperGroup`` which defers sub-command module imports until a
command is actually *invoked*, keeping ``skyvern --help`` fast even when
heavy dependencies are installed.
"""

from __future__ import annotations

import importlib
from typing import Any

import click
import typer
import typer.core
import typer.main
from rich.panel import Panel

from skyvern.cli.console import console

# ---------------------------------------------------------------------------
# Module-level lazy command registry
# ---------------------------------------------------------------------------

_LAZY_COMMANDS: dict[str, tuple[str, str, str]] = {}
"""Mapping of command_name -> (module_path, attr_name, help_text)."""


def register_lazy_command(name: str, module_path: str, attr_name: str, help_text: str) -> None:
    """Register a command/sub-app for deferred import.

    Parameters
    ----------
    name:
        The CLI sub-command name (e.g. ``"run"``).
    module_path:
        Dotted Python module path (e.g. ``"skyvern.cli.run_commands"``).
    attr_name:
        Attribute to import from *module_path* (e.g. ``"run_app"``).
    help_text:
        Short help shown in ``--help`` without importing the module.
    """
    _LAZY_COMMANDS[name] = (module_path, attr_name, help_text)


def _resolve_lazy_command(name: str) -> click.BaseCommand:
    """Import the module and resolve the Typer app (or Click command) for *name*."""
    module_path, attr_name, _help = _LAZY_COMMANDS[name]
    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr_name)

    if isinstance(obj, typer.Typer):
        group = typer.main.get_group(obj)
        group.name = name
        return group

    if callable(obj):
        # For factory functions that return a Typer app
        result = obj()
        if isinstance(result, typer.Typer):
            group = typer.main.get_group(result)
            group.name = name
            return group
        if isinstance(result, click.BaseCommand):
            return result
        raise TypeError(f"Factory '{name}' returned unsupported type: {type(result)}")

    if isinstance(obj, click.BaseCommand):
        return obj

    raise TypeError(f"Lazy command '{name}' resolved to unsupported type: {type(obj)}")


# ---------------------------------------------------------------------------
# Placeholder click.Command used for --help rendering
# ---------------------------------------------------------------------------


class _LazyPlaceholder(click.Command):
    """A lightweight stand-in that renders help text without importing anything."""

    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name=name, help=help_text)

    def get_short_help_str(self, limit: int = 150) -> str:
        return self.help or ""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            real = _resolve_lazy_command(self.name or "")
        except ImportError as exc:
            _handle_missing_dep(exc)
            raise  # unreachable — _handle_missing_dep raises typer.Exit, but satisfies linters
        return real.invoke(ctx)


# ---------------------------------------------------------------------------
# LazyTyperGroup
# ---------------------------------------------------------------------------


class LazyTyperGroup(typer.core.TyperGroup):
    """A TyperGroup subclass that includes lazily-registered commands.

    Commands registered via :func:`register_lazy_command` appear in
    ``list_commands`` and ``--help`` output but are not imported until a
    user actually *invokes* them.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        # Eagerly-added commands first (preserves insertion order), then lazy.
        eager = list(self.commands)
        lazy = [n for n in _LAZY_COMMANDS if n not in self.commands]
        return eager + lazy

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.BaseCommand | None:
        # Prefer eagerly-registered commands.
        if cmd_name in self.commands:
            return self.commands[cmd_name]

        if cmd_name in _LAZY_COMMANDS:
            try:
                resolved = _resolve_lazy_command(cmd_name)
                # Cache so subsequent calls are fast.
                self.commands[cmd_name] = resolved
                return resolved
            except ImportError:
                # Missing dep — return placeholder so --help doesn't crash.
                _, _, help_text = _LAZY_COMMANDS[cmd_name]
                return _LazyPlaceholder(cmd_name, help_text)

        return None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Render all commands (eager + lazy) using pre-registered help text."""
        commands: list[tuple[str, click.BaseCommand]] = []

        for name in self.list_commands(ctx):
            if name in self.commands:
                cmd = self.commands[name]
                if cmd.hidden:
                    continue
                commands.append((name, cmd))
            elif name in _LAZY_COMMANDS:
                _, _, help_text = _LAZY_COMMANDS[name]
                commands.append((name, _LazyPlaceholder(name, help_text)))

        if not commands:
            return

        limit = formatter.width - 6 - max(len(n) for n, _ in commands)
        rows: list[tuple[str, str]] = []
        for name, cmd in commands:
            short_help = cmd.get_short_help_str(limit=limit)
            rows.append((name, short_help))

        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


# ---------------------------------------------------------------------------
# Missing-dep helper
# ---------------------------------------------------------------------------


def _handle_missing_dep(exc: ImportError) -> None:
    """Show a user-friendly error when a required dependency is missing."""
    dep_name = exc.name or str(exc)
    console.print(
        Panel(
            f"[bold red]This command requires a dependency that is not installed.[/bold red]\n\n"
            f"Missing: [yellow]{dep_name}[/yellow]\n"
            f"Run: [green]pip install {dep_name}[/green]",
            title="Missing Dependency",
            border_style="red",
        )
    )
    raise typer.Exit(code=1)
