import typer
from dotenv import load_dotenv

from .docs import docs_app
from .init_command import init
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

setup_app.command(name="mcp")(setup_mcp_command)
cli_app.command(name="init")(init)

if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv()
    cli_app()
