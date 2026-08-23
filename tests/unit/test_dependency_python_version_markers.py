"""No dependency may silently disappear when the interpreter moves past a marker's upper bound.

Several dependencies are declared as a chain of version-pinned branches split by `python_version`.
That is a resolver-visible conflict only while some branch matches. The moment the interpreter passes
the last branch's upper bound, the dependency stops matching *any* branch and drops out of the
resolution with no error at all -- the build succeeds and the package is simply gone.

That is not hypothetical: resolving this project's dependencies under python 3.14 drops
`psycopg` entirely, so the server extra would ship without a Postgres driver and fail on its first
database connection instead of at build time.

This test pins the shape rather than the versions: whatever the branches are, the last one must be
open-ended upward.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

_UPPER_BOUND = re.compile(r"python_version\s*<\s*['\"](?P<version>[0-9.]+)['\"]")
_NAME = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)")


def _requirement_groups() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    groups = {"project.dependencies": project.get("dependencies", [])}
    for extra, requirements in (project.get("optional-dependencies") or {}).items():
        groups[f"optional-dependencies.{extra}"] = requirements
    for group, requirements in (data.get("dependency-groups") or {}).items():
        if isinstance(requirements, list) and all(isinstance(entry, str) for entry in requirements):
            groups[f"dependency-groups.{group}"] = requirements
    return groups


def _distribution(requirement: str) -> str:
    match = _NAME.match(requirement)
    return (match.group("name") if match else requirement).lower().replace("_", "-")


@pytest.mark.parametrize("group", sorted(_requirement_groups()))
def test_every_marker_split_dependency_has_an_open_ended_top_branch(group: str) -> None:
    requirements = _requirement_groups()[group]

    capped: dict[str, list[str]] = {}
    uncapped: set[str] = set()
    for requirement in requirements:
        name = _distribution(requirement)
        if "python_version" not in requirement:
            uncapped.add(name)
            continue
        bound = _UPPER_BOUND.search(requirement)
        if bound:
            capped.setdefault(name, []).append(requirement)
        else:
            uncapped.add(name)

    orphaned = {name: entries for name, entries in capped.items() if name not in uncapped}
    assert not orphaned, (
        "these dependencies are declared only under upper-bounded python_version markers, so they "
        "vanish silently from resolution on a newer interpreter rather than failing the build:\n"
        + "\n".join(f"  {name}: {entries}" for name, entries in sorted(orphaned.items()))
    )
