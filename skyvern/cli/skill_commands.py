"""Skill file management commands."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.markdown import Markdown
from rich.table import Table

from skyvern.cli.console import console

skill_app = typer.Typer(help="Manage bundled skill reference files.")

SKILLS_DIR = Path(__file__).parent / "skills"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)

_QA_REPORT_MARKER = "<!-- skyvern-qa-report -->"
_QA_REPORT_PATH = Path(".qa/latest-report.md")
_QA_COMMENT_FIELDS_JQ = '.[] | {id: .id, login: .user.login, head: (.body | split("\n")[0])}'


def _run_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, shell=False, check=False, capture_output=True, text=True)
    except OSError as exc:
        console.print(f"Unable to run {command[0]}: {exc}", style="red", markup=False)
        raise typer.Exit(code=1) from exc


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode == 0:
        return result.stdout.strip()
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    console.print(f"Unable to {action}: {detail}", style="red", markup=False)
    raise typer.Exit(code=1)


def _find_sticky_comment_id(comment_lines: str, login: str) -> str | None:
    """Return the oldest comment by `login` whose body opens with the QA marker."""
    for line in comment_lines.splitlines():
        if not line.strip():
            continue
        try:
            comment = json.loads(line)
        except json.JSONDecodeError:
            continue
        if comment.get("login") == login and str(comment.get("head", "")).strip() == _QA_REPORT_MARKER:
            return str(comment.get("id", ""))
    return None


def get_skill_dirs() -> list[Path]:
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
    dirs = get_skill_dirs()
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
        dirs = get_skill_dirs()
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


@skill_app.command("post-qa-report")
def post_qa_report() -> None:
    """Create or update a pull request QA report comment from .qa/latest-report.md."""
    report_file = _QA_REPORT_PATH
    try:
        report = report_file.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"Unable to read {report_file}: {exc}", style="red", markup=False)
        raise typer.Exit(code=1) from exc

    pr_result = _run_process(
        [
            "gh",
            "pr",
            "status",
            "--json",
            "number,state",
            "--jq",
            'if .currentBranch.state == "OPEN" then .currentBranch.number else empty end',
        ]
    )
    if pr_result.returncode != 0:
        detail = pr_result.stderr.strip() or pr_result.stdout.strip() or "unknown error"
        console.print(f"Unable to check for an open PR: {detail}", style="red", markup=False)
        console.print(f"Report remains at {report_file}.", style="yellow", markup=False)
        raise typer.Exit(code=1)
    if not pr_result.stdout.strip():
        console.print(f"No open PR found. Report remains at {report_file}.", style="yellow", markup=False)
        return
    pr_number = pr_result.stdout.strip()
    if not pr_number.isdecimal():
        console.print("[red]Unable to determine a valid PR number.[/red]")
        raise typer.Exit(code=1)

    commit = _require_success(
        _run_process(["git", "rev-parse", "--short", "HEAD"]),
        "read the current commit",
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_body = f"{_QA_REPORT_MARKER}\n## QA Report — {commit} — {timestamp}\n\n{report}\n"

    # Only ever edit a comment this user authored: any account can post a body containing
    # the marker, and editing on a bare marker match would delete someone else's comment.
    login_result = _run_process(["gh", "api", "user", "--jq", ".login"])
    login = login_result.stdout.strip() if login_result.returncode == 0 else ""
    existing_comment_id = None
    if login:
        comments_result = _run_process(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
                "--jq",
                _QA_COMMENT_FIELDS_JQ,
            ]
        )
        comment_lines = _require_success(comments_result, "look up the existing report comment")
        existing_comment_id = _find_sticky_comment_id(comment_lines, login)
    else:
        console.print(
            "Could not identify the authenticated gh user; posting a new comment instead of updating.",
            style="yellow",
            markup=False,
        )

    if existing_comment_id:
        if not existing_comment_id.isdecimal():
            console.print("[red]Unable to determine a valid report comment ID.[/red]")
            raise typer.Exit(code=1)
        post_command = [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/issues/comments/{existing_comment_id}",
            "-X",
            "PATCH",
            "-f",
            f"body={comment_body}",
        ]
    else:
        post_command = ["gh", "pr", "comment", pr_number, "--body", comment_body]

    _require_success(_run_process(post_command), "post the report comment")
    console.print("[green]Posted report to the pull request.[/green]")
