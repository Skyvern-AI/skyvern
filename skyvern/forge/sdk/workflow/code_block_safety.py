"""Shared static safety checks for CodeBlock Python snippets."""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable

from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected

# Keep this policy aligned with codeblock/codeblock_safety.py; the runner image carries a local copy.
BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "system",
        "popen",
        "Popen",
        "exec",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "check_call",
        "check_output",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execlp",
        "execlpe",
        "fork",
        "open_connection",
        "start_server",
        "create_connection",
        "create_server",
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_code",
        "co_code",
        "co_consts",
        "co_names",
        "co_varnames",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "tb_frame",
        "tb_next",
        "mro",
        "listdir",
        "makedirs",
        "rmdir",
        "codecs",
        "modules",
        "builtins",
        "stdout",
        "stderr",
        "stdin",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "eval",
        "vars",
        "format",
        "format_map",
    }
)


def is_safe_code(
    code: str,
    *,
    error_factory: Callable[[str], Exception] = InsecureCodeDetected,
) -> None:
    """Reject imports, private members, and known escape hatches."""
    tree = ast.parse(textwrap.dedent(code))
    for node in ast.walk(tree):
        if hasattr(node, "attr") and str(node.attr).startswith("__"):
            raise error_factory("Not allowed to access private methods or attributes")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise error_factory("Not allowed to access private methods or attributes")
        if isinstance(node, ast.Import | ast.ImportFrom):
            raise error_factory("Not allowed to import modules")
        if hasattr(node, "attr") and node.attr in BLOCKED_ATTRS:
            raise error_factory(f"Not allowed to access '{node.attr}'")
