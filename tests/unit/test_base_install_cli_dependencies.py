"""CLI commands documented for a plain `pip install skyvern` must only need base dependencies.

The MCP docs tell users to run `pip install skyvern` and then `skyvern setup <client>` or
`skyvern mcp switch`. The heavy server stack lives in the `server` extra, so any third-party module
these commands import at module level has to be declared in `project.dependencies`; otherwise the
command fails with a "Missing Dependency" error on a base install.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

BASE_INSTALL_CLI_MODULES = ["skyvern.cli.setup_commands", "skyvern.cli.mcp_commands"]

# Import name -> distribution name, where they differ.
_DISTRIBUTION_FOR_MODULE = {"yaml": "pyyaml", "dotenv": "python-dotenv"}
_NAME = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)")


def _base_distributions() -> set[str]:
    dependencies = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return {_NAME.match(req).group("name").lower().replace("_", "-") for req in dependencies}


def _module_path(module: str) -> Path | None:
    base = REPO_ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _module_level_imports(path: Path) -> set[str]:
    """Absolute imports executed at import time (top level, not under `if TYPE_CHECKING`)."""
    imports: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def _third_party_imports(entry_module: str) -> set[str]:
    """Top-level third-party packages reachable from `entry_module` through module-level imports."""
    seen: set[str] = set()
    third_party: set[str] = set()
    pending = [entry_module]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is None:
            continue
        for imported in _module_level_imports(path):
            top = imported.split(".")[0]
            if top == "skyvern":
                pending.append(imported)
            elif top not in sys.stdlib_module_names and top != "__future__":
                third_party.add(top)
    return third_party


@pytest.mark.parametrize("module", BASE_INSTALL_CLI_MODULES)
def test_base_install_cli_imports_are_base_dependencies(module: str) -> None:
    base = _base_distributions()
    missing = sorted(
        top
        for top in _third_party_imports(module)
        if _DISTRIBUTION_FOR_MODULE.get(top, top).lower().replace("_", "-") not in base
    )
    assert not missing, f"{module} imports {missing}, which are not in project.dependencies"
