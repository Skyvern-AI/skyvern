"""Skill file management commands."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import typer
from rich.markdown import Markdown
from rich.table import Table

from skyvern.cli.console import console

skill_app = typer.Typer(help="Manage bundled skill reference files.")

SKILLS_DIR = Path(__file__).parent / "skills"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _get_skill_dirs() -> list[Path]:
    """Return sorted list of skill directories (those containing SKILL.md)."""
    if not SKILLS_DIR.exists():
        return []
    return sorted(
        d for d in SKILLS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_") and (d / "SKILL.md").exists()
    )


def _resolve_skill(name: str) -> Path:
    """Resolve a skill name to its SKILL.md path with path containment check."""
    skill_md = (SKILLS_DIR / name / "SKILL.md").resolve()
    if not skill_md.is_relative_to(SKILLS_DIR.resolve()):
        console.print(f"[red]Invalid skill name: {name}[/red]")
        raise typer.Exit(code=1)
    if not skill_md.exists():
        console.print(f"[red]Skill '{name}' not found. Run 'skyvern skill list' to see available skills.[/red]")
        raise typer.Exit(code=1)
    return skill_md


def _extract_description(skill_md: Path) -> str:
    """Extract the description field from SKILL.md frontmatter."""
    content = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return ""
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("description:"):
            desc = line[len("description:") :].strip().strip('"').strip("'")
            # Truncate long descriptions for table display
            if len(desc) > 80:
                return desc[:77] + "..."
            return desc
    return ""


@skill_app.command("list")
def skill_list() -> None:
    """List all bundled skills."""
    dirs = _get_skill_dirs()
    if not dirs:
        console.print("[red]No skills found in package. Re-install skyvern.[/red]")
        raise typer.Exit(code=1)

    table = Table(title="Bundled Skills")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    for d in dirs:
        desc = _extract_description(d / "SKILL.md")
        table.add_row(d.name, desc)
    console.print(table)


@skill_app.command("path")
def skill_path(
    name: str = typer.Argument(None, help="Skill name (omit to show skills directory)"),
) -> None:
    """Print the absolute path to a bundled skill or the skills directory."""
    if name is None:
        if not SKILLS_DIR.exists():
            console.print("[red]Skills directory not found in package. Re-install skyvern.[/red]")
            raise typer.Exit(code=1)
        typer.echo(str(SKILLS_DIR))
        return

    skill_md = _resolve_skill(name)
    typer.echo(str(skill_md))


@skill_app.command("show")
def skill_show(
    name: str = typer.Argument(..., help="Skill name to display"),
) -> None:
    """Display a skill's SKILL.md rendered in the terminal."""
    skill_md = _resolve_skill(name)
    content = skill_md.read_text(encoding="utf-8")
    console.print(Markdown(content))


@skill_app.command("copy")
def skill_copy(
    output: str = typer.Option(".", "--output", "-o", help="Destination directory"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files"),
    name: str = typer.Argument(None, help="Skill name (omit to copy all skills)"),
) -> None:
    """Copy skill(s) to a local path for customization or agent installation."""
    dst = Path(output)
    _ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    dst.mkdir(parents=True, exist_ok=True)
    if name is not None:
        skill_md = _resolve_skill(name)
        src = skill_md.parent
        target = dst / name
        if target.exists() and not overwrite:
            console.print(f"[yellow]Destination {target} already exists. Use --overwrite to replace.[/yellow]")
            raise typer.Exit(code=1)
        shutil.copytree(src, target, dirs_exist_ok=overwrite, ignore=_ignore)
        console.print(f"[green]Copied skill '{name}' to {target.resolve()}[/green]")
    else:
        dirs = _get_skill_dirs()
        if not dirs:
            console.print("[red]No skills found in package. Re-install skyvern.[/red]")
            raise typer.Exit(code=1)
        for d in dirs:
            target = dst / d.name
            if target.exists() and not overwrite:
                console.print(f"[yellow]Destination {target} already exists. Use --overwrite to replace.[/yellow]")
                raise typer.Exit(code=1)
        for d in dirs:
            target = dst / d.name
            shutil.copytree(d, target, dirs_exist_ok=overwrite, ignore=_ignore)
        console.print(f"[green]Copied {len(dirs)} skills to {dst.resolve()}[/green]")
