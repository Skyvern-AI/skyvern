import typer

from .docs_commands import open_docs, prompting_guide
from .init_command import init
from .status_command import status
from .tasks_commands import create_task, list_tasks
from .workflows_commands import list_workflows

cli_app = typer.Typer(
    help="Skyvern - Browser automation powered by LLMs and Computer Vision",
    add_completion=False,
)

run_app = typer.Typer(help="Run Skyvern components")
setup_app = typer.Typer(help="Set up Skyvern configurations")
tasks_app = typer.Typer(help="Manage Skyvern tasks")
workflows_app = typer.Typer(help="Manage Skyvern workflows")
docs_app = typer.Typer(help="Access Skyvern documentation")

cli_app.add_typer(run_app, name="run")
cli_app.add_typer(setup_app, name="setup")
cli_app.add_typer(tasks_app, name="tasks")
cli_app.add_typer(workflows_app, name="workflows")
cli_app.add_typer(docs_app, name="docs")

cli_app.command(name="init")(init)
cli_app.command(name="status")(status)
docs_app.command(name="open")(open_docs)
docs_app.command(name="prompting")(prompting_guide)
tasks_app.command(name="list")(list_tasks)
tasks_app.command(name="create")(create_task)
workflows_app.command(name="list")(list_workflows)
