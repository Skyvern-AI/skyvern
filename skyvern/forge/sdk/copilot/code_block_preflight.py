"""Static preflight checks for generated Workflow Copilot code blocks."""

from __future__ import annotations

import ast
import keyword
import re
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CodeBlockPreflightDiagnostic:
    code: str
    message: str


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\([AB]")
_MYPY_ERROR_RE = re.compile(r"^(?P<path>.*?):(?P<line>\d+): (?P<severity>error): (?P<message>.*)$")
_LOCATOR_NOT_CALLABLE_RE = re.compile(r'"Locator" not callable\s+\[operator\]')


def preflight_code_block(
    code: str,
    *,
    parameter_keys: Iterable[str] = (),
) -> list[CodeBlockPreflightDiagnostic]:
    """Run typed snippet checks for code-block Python.

    This is intentionally best-effort while the checker dependency remains a
    dev/local dependency. Security/sandbox validation still runs separately and
    must not depend on this helper being available.
    """

    diagnostics = _static_ast_diagnostics(code)
    if diagnostics:
        return diagnostics

    try:
        from mypy import api as mypy_api
    except ImportError:
        return []

    source = _build_typed_module(code, parameter_keys=parameter_keys)
    # mypy's API raises the interpreter recursion limit and never restores it,
    # which leaks into the rest of the process; snapshot and restore it ourselves.
    recursion_limit = sys.getrecursionlimit()
    with tempfile.TemporaryDirectory(prefix="skyvern-code-block-preflight-") as tmpdir:
        path = Path(tmpdir) / "code_block.py"
        path.write_text(source, encoding="utf-8")
        try:
            stdout, stderr, status = mypy_api.run(
                [
                    str(path),
                    "--config-file=/dev/null",
                    "--no-error-summary",
                    "--show-error-codes",
                    "--ignore-missing-imports",
                    "--no-incremental",
                    "--cache-dir=/dev/null",
                ]
            )
        finally:
            sys.setrecursionlimit(recursion_limit)
    if status == 0:
        return []

    return _parse_mypy_output(stdout)


def _static_ast_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    try:
        tree = ast.parse(_build_typed_module(code, parameter_keys=()))
    except SyntaxError as exc:
        # The wrapper scaffolding is static and valid, so any SyntaxError is in the supplied code —
        # e.g. an attacker-page string with a raw line-boundary codepoint that splits a literal. Surface
        # it at authoring time instead of letting the block fail silently at run time.
        return [
            CodeBlockPreflightDiagnostic(
                code="SYNTAX_ERROR",
                message=f"Code block does not parse as Python: {exc.msg}. Fix the snippet before persisting it.",
            )
        ]

    diagnostics: list[CodeBlockPreflightDiagnostic] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        wizard_step_diagnostic = _wizard_step_selector_diagnostic(node)
        if wizard_step_diagnostic is not None:
            diagnostics.append(wizard_step_diagnostic)
            continue
        evaluate_diagnostic = _page_evaluate_diagnostic(node)
        if evaluate_diagnostic is not None:
            diagnostics.append(evaluate_diagnostic)
            continue
        regex_diagnostic = _regex_literal_diagnostic(node)
        if regex_diagnostic is not None:
            diagnostics.append(regex_diagnostic)
    return diagnostics


def _wizard_step_selector_diagnostic(node: ast.Call) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "locator" or not node.args:
        return None

    selector = node.args[0]
    if not isinstance(selector, ast.Constant) or not isinstance(selector.value, str):
        return None
    normalized_selector = selector.value.lower()
    if "data-next-step" not in normalized_selector and "data-step" not in normalized_selector:
        return None
    if "button" not in normalized_selector:
        return None

    return CodeBlockPreflightDiagnostic(
        code="AMBIGUOUS_WIZARD_STEP_SELECTOR",
        message=(
            "Code block targets a wizard step button by metadata selector only. Step metadata can match "
            "both forward and back controls under Playwright strict mode. Target the visible semantic control "
            "instead, such as `page.get_by_role('button', name='Continue')`, or narrow the locator to visible "
            "button text before clicking."
        ),
    )


def _page_evaluate_diagnostic(node: ast.Call) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "evaluate":
        return None
    if len(node.args) <= 2:
        return None
    return CodeBlockPreflightDiagnostic(
        code="PLAYWRIGHT_API_MISMATCH",
        message=(
            "Code block calls Playwright `evaluate` with too many positional arguments. "
            "In Playwright Python, pass at most the JavaScript expression and one serialized arg; "
            "pack multiple values into a dict or list before calling evaluate."
        ),
    )


_RE_LITERAL_FUNCTIONS = frozenset(
    {
        "compile",
        "search",
        "match",
        "fullmatch",
        "findall",
        "finditer",
        "split",
        "sub",
    }
)


def _regex_literal_diagnostic(node: ast.Call) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if (
        not isinstance(func, ast.Attribute)
        or func.attr not in _RE_LITERAL_FUNCTIONS
        or not isinstance(func.value, ast.Name)
        or func.value.id != "re"
        or not node.args
    ):
        return None

    pattern = node.args[0]
    if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
        return None

    try:
        re.compile(pattern.value)
    except re.error as exc:
        return CodeBlockPreflightDiagnostic(
            code="INVALID_REGEX_LITERAL",
            message=(
                f"Code block contains an invalid regex literal for `re.{func.attr}`: {exc}. "
                "Fix the pattern or avoid regex when simple string checks are enough."
            ),
        )
    return None


def _build_typed_module(code: str, *, parameter_keys: Iterable[str]) -> str:
    parameter_declarations = "\n".join(
        f"{key}: Any" for key in dict.fromkeys(parameter_keys) if _valid_python_identifier(key)
    )
    indented_code = textwrap.indent(textwrap.dedent(code).strip() or "pass", "    ")
    if parameter_declarations:
        parameter_declarations += "\n"
    return (
        "from __future__ import annotations\n"
        "from typing import Any\n"
        "from types import SimpleNamespace\n"
        "import asyncio\n"
        "import html\n"
        "import json\n"
        "import re\n"
        "from asyncio import sleep\n"
        "from playwright.async_api import Page\n\n"
        "page: Page\n"
        f"{parameter_declarations}"
        "\n"
        "async def __code_block__() -> object:\n"
        f"{indented_code}\n"
        "    return {}\n"
    )


def _valid_python_identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value) and not value.startswith("__")


def _parse_mypy_output(output: str) -> list[CodeBlockPreflightDiagnostic]:
    diagnostics: list[CodeBlockPreflightDiagnostic] = []
    for line in output.splitlines():
        match = _MYPY_ERROR_RE.match(_ANSI_ESCAPE_RE.sub("", line))
        if not match:
            continue
        message = match.group("message")
        diagnostic = _diagnostic_from_mypy_message(message)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def _diagnostic_from_mypy_message(message: str) -> CodeBlockPreflightDiagnostic | None:
    if _LOCATOR_NOT_CALLABLE_RE.search(message):
        return CodeBlockPreflightDiagnostic(
            code="PLAYWRIGHT_API_MISMATCH",
            message=(
                "Code block calls a Playwright Locator as a function. In Playwright Python, locator properties "
                "such as `.first` and `.last` are not methods; use the property value before waiting, filling, "
                "or clicking."
            ),
        )
    return None
