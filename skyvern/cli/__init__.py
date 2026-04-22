"""Skyvern CLI package."""

__all__ = [
    "cli_app",
    "credentials_app",
    "init_app",
    "quickstart_app",
    "run_app",
    "workflow_app",
    "tasks_app",
    "docs_app",
    "status_app",
]

from .commands import cli_app

# Backward-compatible lazy re-exports: other symbols are only imported on access.
_LAZY_REEXPORTS = {
    "credentials_app": ("skyvern.cli.credentials", "credentials_app"),
    "docs_app": ("skyvern.cli.docs", "docs_app"),
    "init_app": ("skyvern.cli.init_command", "init_app_factory"),
    "quickstart_app": ("skyvern.cli.quickstart", "quickstart_app"),
    "run_app": ("skyvern.cli.run_commands", "run_app"),
    "status_app": ("skyvern.cli.status", "status_app"),
    "tasks_app": ("skyvern.cli.tasks", "tasks_app"),
    "workflow_app": ("skyvern.cli.workflow", "workflow_app"),
}


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    if name in _LAZY_REEXPORTS:
        import importlib  # noqa: PLC0415

        import typer as _typer  # noqa: PLC0415

        module_path, attr_name = _LAZY_REEXPORTS[name]
        mod = importlib.import_module(module_path)
        value = getattr(mod, attr_name)
        # If the export is a factory function (e.g. init_app_factory), call it
        # to return the Typer app instance for backward compatibility.
        if callable(value) and not isinstance(value, _typer.Typer):
            value = value()
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
