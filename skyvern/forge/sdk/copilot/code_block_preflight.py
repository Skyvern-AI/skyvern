"""Static preflight checks for generated Workflow Copilot code blocks."""

from __future__ import annotations

import ast
import asyncio
import keyword
import re
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Iterator, Mapping

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError, author_time_code_security_errors
from skyvern.forge.sdk.copilot.code_block_synthesis import is_root_locator_selector
from skyvern.forge.sdk.workflow.models._jinja import _json_finalize, _json_type_filter
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.utils.templating import get_missing_variables

RENDER_TEMPLATE_SYNTAX_REASON_CODE = "RENDER_TEMPLATE_SYNTAX"
RENDER_UNDEFINED_NAME_REASON_CODE = "RENDER_UNDEFINED_NAME"
SCANNER_ADVISORY_REASON_CODE = "SCANNER_ADVISORY"
SCANNER_ADVISORY_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class CodeBlockPreflightDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class CodeBlockScanFinding:
    """One advisory finding from the deployment's code scanner.

    ``message`` must come from scanner rule metadata only — never from the matched
    snippet, which can carry user PII/secrets and attacker-authored text that would
    otherwise flow into a copilot context.
    """

    rule_id: str
    line: int
    message: str = ""


@dataclass(frozen=True)
class CodeBlockRenderDiagnostic:
    code: str
    message: str
    failing_expression: str


# Mirrors the runtime's strict-mode template formatter (jinja_json_finalize_strict_env)
# regardless of the WORKFLOW_TEMPLATING_STRICTNESS deployment setting.
_render_check_env = SandboxedEnvironment(undefined=StrictUndefined, finalize=_json_finalize)
_render_check_env.filters["json"] = _json_type_filter

_RENDER_SYSTEM_BINDING_NAMES = (
    "workflow_title",
    "workflow_id",
    "workflow_permanent_id",
    "workflow_run_id",
    "current_date",
    "browser_session_id",
    "workflow_run_outputs",
    "workflow_run_summary",
)

# The runtime injects these only inside a for-loop iteration, so bind them only
# when the source actually opens a loop. ponytail: loop-presence heuristic, not
# true per-scope tracking — a reference after the loop closes still passes.
_RENDER_LOOP_BINDING_NAMES = (
    "current_index",
    "current_item",
    "current_value",
)

_JINJA_EXPRESSION_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STATEMENT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_FOR_STATEMENT_RE = re.compile(r"\{%-?\s*for\s", re.DOTALL)


class _PermissiveRenderBinding:
    def __getattr__(self, name: str) -> _PermissiveRenderBinding:
        return self

    def __getitem__(self, key: object) -> _PermissiveRenderBinding:
        return self

    # Without __iter__, __getitem__ triggers Python's legacy iteration protocol,
    # which never raises IndexError here and spins {% for %} loops forever.
    def __iter__(self) -> Iterator[_PermissiveRenderBinding]:
        return iter((self,))

    def __str__(self) -> str:
        return "value"


def _first_template_expression_for(code: str, root: str) -> str:
    root_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(root)}(?![A-Za-z0-9_])")
    for pattern in (_JINJA_EXPRESSION_RE, _JINJA_STATEMENT_RE):
        for match in pattern.finditer(code):
            if root_re.search(match.group(0)):
                return match.group(0)
    return f"{{{{ {root} }}}}"


def _top_level_form_suggestion(expression: str, root: str) -> str:
    inner = expression.strip().strip("{}%").strip()
    if not inner.startswith(f"{root}."):
        return ""
    remainder = inner[len(root) + 1 :]
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", remainder)
    if match is None:
        return ""
    return f"{{{{ {match.group(0)} }}}}"


def code_block_render_diagnostic(code: str, bound_names: Iterable[str]) -> CodeBlockRenderDiagnostic | None:
    """Dry-render the code block through the runtime's strict Jinja semantics with every
    runtime-provided name bound to a permissive sentinel; only genuinely unrenderable
    templates (undefined names, syntax errors, sandbox violations) produce a diagnostic."""
    if "{{" not in code and "{%" not in code:
        return None
    try:
        template = _render_check_env.from_string(code)
    except TemplateSyntaxError as exc:
        source_lines = code.splitlines()
        line = source_lines[exc.lineno - 1].strip() if exc.lineno and exc.lineno <= len(source_lines) else ""
        detail = f" Offending line: `{line}`." if line else ""
        return CodeBlockRenderDiagnostic(
            code=RENDER_TEMPLATE_SYNTAX_REASON_CODE,
            message=f"Jinja template syntax error on line {exc.lineno}: {exc.message}.{detail}",
            failing_expression=line,
        )
    bindings: dict[str, object] = {name: _PermissiveRenderBinding() for name in bound_names}
    system_names: tuple[str, ...] = _RENDER_SYSTEM_BINDING_NAMES
    if _JINJA_FOR_STATEMENT_RE.search(code):
        system_names = system_names + _RENDER_LOOP_BINDING_NAMES
    for name in system_names:
        bindings.setdefault(name, _PermissiveRenderBinding())
    missing: set[str] = set()
    try:
        missing = get_missing_variables(code, bindings)
    except (UndefinedError, SecurityError) as exc:
        return CodeBlockRenderDiagnostic(
            code=RENDER_UNDEFINED_NAME_REASON_CODE,
            message=f"A Jinja expression in this code block cannot render at runtime: {exc}.",
            failing_expression="",
        )
    except Exception:
        missing = set()
    if missing:
        root = sorted(missing)[0].split("[")[0].split(".")[0]
        expression = _first_template_expression_for(code, root)
        suggestion = _top_level_form_suggestion(expression, root)
        guidance = (
            f" Declared inputs are injected as top-level names; write `{suggestion}` instead."
            if suggestion
            else (
                " Only declared parameter keys, block labels, `<label>_output` values, and workflow "
                "system names (e.g. `current_date`) are available as top-level template names."
            )
        )
        return CodeBlockRenderDiagnostic(
            code=RENDER_UNDEFINED_NAME_REASON_CODE,
            message=(
                f"The expression `{expression}` cannot render at runtime: "
                f"`{root}` is not a defined template name.{guidance}"
            ),
            failing_expression=expression,
        )
    try:
        template.render(bindings)
    except (UndefinedError, SecurityError) as exc:
        return CodeBlockRenderDiagnostic(
            code=RENDER_UNDEFINED_NAME_REASON_CODE,
            message=f"A Jinja expression in this code block cannot render at runtime: {exc}.",
            failing_expression="",
        )
    except Exception:
        return None
    return None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\([AB]")
_MYPY_ERROR_RE = re.compile(r"^(?P<path>.*?):(?P<line>\d+): (?P<severity>error): (?P<message>.*)$")
_LOCATOR_NOT_CALLABLE_RE = re.compile(r'"Locator" not callable\s+\[operator\]')
# Locator properties that return a Locator; calling one (``.first()`` / ``.last()``)
# is the ``"Locator" not callable`` misuse the mypy pass exists to surface.
_LOCATOR_NONCALLABLE_PROPERTIES = frozenset({"first", "last"})
_BROAD_BODY_TEXT_WAIT_NEEDLES = (
    "document.body.innertext",
    "document.body.textcontent",
    "document.documentelement.innertext",
    "document.documentelement.textcontent",
)
_BROAD_TABLE_RECORD_KEYS = frozenset(("items", "locations", "records", "rows"))
_BROAD_TABLE_SCAN_SELECTORS = frozenset({"article", "section", ".card", "li"})
_BROAD_TABLE_SELECTOR_METHODS = frozenset(("locator", "query_selector", "query_selector_all"))
_LONE_LIST_ITEM_SELECTOR_EXEMPTION = frozenset({"li"})
_GET_BY_TEXT_NARROWING_ATTRIBUTES = frozenset({"first", "last"})
_GET_BY_TEXT_NARROWING_METHODS = frozenset({"filter", "first", "last", "nth"})
_LOCATOR_NARROWING_METHODS = frozenset(
    {
        "filter",
        "get_by_alt_text",
        "get_by_label",
        "get_by_placeholder",
        "get_by_role",
        "get_by_test_id",
        "get_by_text",
        "get_by_title",
        "locator",
        "nth",
    }
)
# `nth` cannot narrow a root container to anything smaller, so it does not count as narrowing on a
# root chain even though it does on an ordinary multi-match locator.
_ROOT_NARROWING_METHODS = _LOCATOR_NARROWING_METHODS - {"nth"}
_READINESS_WAIT_STATES = frozenset({"visible", "attached"})
_READINESS_EXPECTATION_METHODS = frozenset({"to_be_attached", "to_be_visible"})
# Advisory diagnostics reach the model as guidance only; keeping them out of the preflight
# gate is what stops `skyvern_code_block_lint` from turning advice into a rejection.
_ADVISORY_DIAGNOSTIC_CODES = frozenset({"ROOT_CONTAINER_READINESS_WAIT", "ROOT_CONTAINER_TEXT_READ"})
_WHOLE_PAGE_READ_METHODS = frozenset({"inner_text", "text_content", "all_inner_texts", "all_text_contents"})
_TABLE_ROW_TAG_SELECTOR_RE = re.compile(r"(?<![a-z0-9_-])tr(?![a-z0-9_-])")
_TABLE_ROW_ROLE_SELECTOR_RE = re.compile(r"\[role\s*=\s*(['\"]?)row\1\]")


@cache
def _sandbox_shim_surface() -> dict[str, frozenset[str]]:
    return {
        name: frozenset(vars(value))
        for name, value in CodeBlock.build_safe_vars().items()
        if isinstance(value, SimpleNamespace)
    }


def strip_redundant_sandbox_imports(code: str) -> tuple[str, list[str]]:
    """Remove top-level imports the runtime sandbox already injects.

    A module import is removed only when the runtime sandbox provides the same
    name as a ``SimpleNamespace`` helper and every attribute the code reads on
    that name is present on the injected helper. Aliased imports, submodule
    imports, from-imports, compound-line imports, non-sandbox modules, imports
    whose used surface exceeds the injected helper, and bare uses of the name as
    a value are all left in place so ``CodeBlock.is_safe_code`` still rejects
    them with immediate author-time feedback.
    """

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    shim_surface = _sandbox_shim_surface()
    attribute_use = _module_attribute_use(tree)
    bare_use = _module_bare_use(tree)

    removable_spans: list[tuple[int, int]] = []
    stripped_modules: list[str] = []
    occupied_lines = _occupied_line_numbers(tree)
    for node in tree.body:
        if not isinstance(node, ast.Import):
            continue
        candidate_modules = _strippable_module_names(node, shim_surface, attribute_use, bare_use)
        if candidate_modules is None:
            continue
        if node.end_lineno is None:
            continue
        if _line_span_shares_other_statement(node, occupied_lines):
            continue
        removable_spans.append((node.lineno, node.end_lineno))
        stripped_modules.extend(candidate_modules)

    if not removable_spans:
        return code, []

    sanitized = _remove_line_spans(code, removable_spans)
    try:
        ast.parse(sanitized)
    except SyntaxError:
        return code, []
    return sanitized, stripped_modules


def _strippable_module_names(
    node: ast.Import,
    shim_surface: dict[str, frozenset[str]],
    attribute_use: dict[str, set[str]],
    bare_use: set[str],
) -> list[str] | None:
    modules: list[str] = []
    for alias in node.names:
        if alias.asname is not None or "." in alias.name:
            return None
        if alias.name not in shim_surface:
            return None
        if alias.name in bare_use:
            return None
        if not attribute_use.get(alias.name, set()).issubset(shim_surface[alias.name]):
            return None
        modules.append(alias.name)
    return modules or None


def _module_attribute_use(tree: ast.AST) -> dict[str, set[str]]:
    usage: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and isinstance(node.value.ctx, ast.Load)
        ):
            usage.setdefault(node.value.id, set()).add(node.attr)
    return usage


def _module_bare_use(tree: ast.AST) -> set[str]:
    # id() of each ast.Attribute value Name is stable across both walks of the same parsed tree.
    attribute_base_names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attribute_base_names.add(id(node.value))
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in attribute_base_names:
            bare.add(node.id)
    return bare


def _occupied_line_numbers(tree: ast.AST) -> dict[int, set[int]]:
    lines: dict[int, set[int]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.stmt):
            continue
        if node.lineno is None or node.end_lineno is None:
            continue
        for line in range(node.lineno, node.end_lineno + 1):
            lines.setdefault(line, set()).add(id(node))
    return lines


def _line_span_shares_other_statement(node: ast.Import, occupied_lines: dict[int, set[int]]) -> bool:
    if node.end_lineno is None:
        return True
    for line in range(node.lineno, node.end_lineno + 1):
        if any(owner != id(node) for owner in occupied_lines.get(line, set())):
            return True
    return False


def _remove_line_spans(code: str, spans: list[tuple[int, int]]) -> str:
    drop_lines: set[int] = set()
    for start, end in spans:
        drop_lines.update(range(start, end + 1))
    kept = [line for index, line in enumerate(code.splitlines(keepends=True), start=1) if index not in drop_lines]
    return "".join(kept)


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

    # Booting mypy costs ~1s per call, and the only diagnostic it can surface is
    # ``"Locator" not callable`` (a Locator invoked as a function). Skip the boot
    # entirely when a cheap AST scan proves the snippet contains no such call.
    tree, _ = _parse_static_ast(code)
    if tree is None or not _may_invoke_locator_object(tree):
        return []

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


def _may_invoke_locator_object(tree: ast.AST) -> bool:
    """True when the snippet could call a Locator object as a function.

    ``"Locator" not callable`` is the only diagnostic the mypy pass surfaces, and
    it arises from calling a Locator-returning property (``.first()`` / ``.last()``)
    or the result of another call (``page.locator(...)()``). Conservative: any such
    shape returns True so the mypy pass still runs; only snippets provably free of
    them skip it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _LOCATOR_NONCALLABLE_PROPERTIES:
            return True
        if isinstance(func, ast.Call):
            return True
    return False


def author_time_code_block_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    tree, _ = _parse_static_ast(code)
    if tree is None:
        return []
    return [*_author_time_security_diagnostics(code), *_author_time_ast_diagnostics(tree)]


def advisory_code_block_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    return [
        diagnostic
        for diagnostic in author_time_code_block_diagnostics(code)
        if diagnostic.code in _ADVISORY_DIAGNOSTIC_CODES
    ]


async def scanner_advisory_diagnostics(
    code: str, *, organization_id: str | None = None
) -> list[CodeBlockPreflightDiagnostic]:
    """Advisory diagnostics from the deployment's code scanner, via the AgentFunction hook.

    Bounded and fail-open by construction: a missing forge app, a scanner error, or a scan
    that outlives the timeout silently yields no diagnostics, so an author never waits on
    the scanner or sees it fail. Findings never gate a save, dispatch, or lint verdict.
    """
    # Deferred so this module stays importable on lightweight installs (MCP lint CLI).
    from skyvern.forge import app  # noqa: PLC0415

    try:
        async with asyncio.timeout(SCANNER_ADVISORY_TIMEOUT_SECONDS):
            findings = await app.AGENT_FUNCTION.scan_code_block_source(
                code,
                organization_id=organization_id,
                timeout_seconds=SCANNER_ADVISORY_TIMEOUT_SECONDS,
            )
        return [_scanner_finding_diagnostic(finding) for finding in findings]
    except Exception:
        return []


def _scanner_finding_diagnostic(finding: CodeBlockScanFinding) -> CodeBlockPreflightDiagnostic:
    text = f"Flagged by scanner rule `{finding.rule_id}` at line {finding.line}."
    if finding.message:
        text = f"{text} {finding.message}"
    return CodeBlockPreflightDiagnostic(code=SCANNER_ADVISORY_REASON_CODE, message=text)


def _static_ast_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    tree, syntax_error = _parse_static_ast(code)
    if syntax_error is not None:
        return [syntax_error]
    if tree is None:
        return []

    diagnostics = [
        *_author_time_security_diagnostics(code),
        *(
            diagnostic
            for diagnostic in _author_time_ast_diagnostics(tree)
            if diagnostic.code not in _ADVISORY_DIAGNOSTIC_CODES
        ),
    ]
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


def _parse_static_ast(code: str) -> tuple[ast.AST | None, CodeBlockPreflightDiagnostic | None]:
    try:
        tree = ast.parse(_build_typed_module(code, parameter_keys=()))
    except SyntaxError as exc:
        # The wrapper scaffolding is static and valid, so any SyntaxError is in the supplied code —
        # e.g. an attacker-page string with a raw line-boundary codepoint that splits a literal. Surface
        # it at authoring time instead of letting the block fail silently at run time.
        return None, CodeBlockPreflightDiagnostic(
            code="SYNTAX_ERROR",
            message=f"Code block does not parse as Python: {exc.msg}. Fix the snippet before persisting it.",
        )
    return tree, None


def _author_time_security_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    normalized_code = textwrap.dedent(code).strip()
    return [
        _author_time_security_diagnostic(error)
        for error in author_time_code_security_errors(label="code", code=normalized_code)
    ]


def _author_time_security_diagnostic(error: CodeBlockSecurityError) -> CodeBlockPreflightDiagnostic:
    return CodeBlockPreflightDiagnostic(
        code=error.reason_code,
        message=(
            f"{error.reason_code}: {error.surface} is not allowed in persisted workflow code blocks. "
            "Use locators and locator DOM-reading methods instead."
        ),
    )


def _author_time_ast_diagnostics(tree: ast.AST) -> list[CodeBlockPreflightDiagnostic]:
    diagnostics: list[CodeBlockPreflightDiagnostic] = []
    broad_table_scan = _broad_table_record_scan_diagnostic(tree)
    if broad_table_scan is not None:
        diagnostics.append(broad_table_scan)
    diagnostics.extend(_alias_wait_diagnostics(tree))
    return diagnostics


def _alias_wait_diagnostics(tree: ast.AST) -> list[CodeBlockPreflightDiagnostic]:
    diagnostics: list[CodeBlockPreflightDiagnostic] = []
    statements = [node for node in ast.iter_child_nodes(tree) if isinstance(node, ast.stmt)]
    _alias_wait_block_diagnostics(statements, {}, {}, {}, diagnostics)
    return diagnostics


def _alias_wait_block_diagnostics(
    statements: list[ast.stmt],
    text_aliases: dict[str, bool],
    table_aliases: dict[str, bool],
    root_aliases: dict[str, bool],
    diagnostics: list[CodeBlockPreflightDiagnostic],
) -> None:
    for statement in statements:
        _alias_wait_statement_diagnostics(statement, text_aliases, table_aliases, root_aliases, diagnostics)


def _alias_wait_statement_diagnostics(
    node: ast.stmt,
    text_aliases: dict[str, bool],
    table_aliases: dict[str, bool],
    root_aliases: dict[str, bool],
    diagnostics: list[CodeBlockPreflightDiagnostic],
) -> None:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        assigned_value, _targets = _assignment_value_and_targets(node)
        if assigned_value is not None:
            _alias_wait_expr_diagnostics(assigned_value, text_aliases, table_aliases, root_aliases, diagnostics)
        _update_global_get_by_text_aliases(node, text_aliases)
        _update_global_table_locator_aliases(node, table_aliases)
        _update_root_locator_aliases(node, root_aliases)
        return

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for decorator in node.decorator_list:
            _alias_wait_expr_diagnostics(decorator, text_aliases, table_aliases, root_aliases, diagnostics)
        _alias_wait_block_diagnostics(
            list(node.body), dict(text_aliases), dict(table_aliases), dict(root_aliases), diagnostics
        )
        return

    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            _alias_wait_expr_diagnostics(child, text_aliases, table_aliases, root_aliases, diagnostics)
    for child_statements in _alias_wait_child_statement_blocks(node):
        _alias_wait_block_diagnostics(
            child_statements, dict(text_aliases), dict(table_aliases), dict(root_aliases), diagnostics
        )


def _alias_wait_child_statement_blocks(node: ast.stmt) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for _field_name, value in ast.iter_fields(node):
        if isinstance(value, list):
            if all(isinstance(item, ast.stmt) for item in value):
                blocks.append(value)
            for item in value:
                if isinstance(item, ast.ExceptHandler):
                    blocks.append(item.body)
    return blocks


def _alias_wait_expr_diagnostics(
    node: ast.expr,
    text_aliases: Mapping[str, bool],
    table_aliases: Mapping[str, bool],
    root_aliases: Mapping[str, bool],
    diagnostics: list[CodeBlockPreflightDiagnostic],
) -> None:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        body_text_wait_diagnostic = _broad_body_text_wait_for_function_diagnostic(child)
        if body_text_wait_diagnostic is not None:
            diagnostics.append(body_text_wait_diagnostic)
        global_text_wait_diagnostic = _global_get_by_text_wait_for_diagnostic(child, text_aliases)
        if global_text_wait_diagnostic is not None:
            diagnostics.append(global_text_wait_diagnostic)
        global_table_wait_diagnostic = _global_table_wait_for_diagnostic(child, table_aliases)
        if global_table_wait_diagnostic is not None:
            diagnostics.append(global_table_wait_diagnostic)
        root_readiness_diagnostic = _root_readiness_wait_diagnostic(child, root_aliases)
        if root_readiness_diagnostic is not None:
            diagnostics.append(root_readiness_diagnostic)
        root_read_diagnostic = _root_container_text_read_diagnostic(child, root_aliases)
        if root_read_diagnostic is not None:
            diagnostics.append(root_read_diagnostic)


def _update_global_get_by_text_aliases(node: ast.Assign | ast.AnnAssign, aliases: dict[str, bool]) -> None:
    assigned_value, targets = _assignment_value_and_targets(node)
    if assigned_value is None:
        return
    is_global_text_locator, has_narrowing = _global_get_by_text_locator_chain(assigned_value, aliases)
    for target in targets:
        if isinstance(target, ast.Name):
            if is_global_text_locator:
                aliases[target.id] = has_narrowing
            else:
                aliases.pop(target.id, None)


def _update_global_table_locator_aliases(node: ast.Assign | ast.AnnAssign, aliases: dict[str, bool]) -> None:
    _update_selector_locator_aliases(node, aliases, _is_table_locator_selector, _LOCATOR_NARROWING_METHODS)


def _update_root_locator_aliases(node: ast.Assign | ast.AnnAssign, aliases: dict[str, bool]) -> None:
    _update_selector_locator_aliases(node, aliases, is_root_locator_selector, _ROOT_NARROWING_METHODS)


def _update_selector_locator_aliases(
    node: ast.Assign | ast.AnnAssign,
    aliases: dict[str, bool],
    matches_selector: Callable[[str], bool],
    narrowing_methods: frozenset[str],
) -> None:
    assigned_value, targets = _assignment_value_and_targets(node)
    if assigned_value is None:
        return
    is_selector_rooted, has_narrowing = _global_selector_locator_chain(
        assigned_value, aliases, matches_selector, narrowing_methods
    )
    for target in targets:
        if isinstance(target, ast.Name):
            if is_selector_rooted:
                aliases[target.id] = has_narrowing
            else:
                aliases.pop(target.id, None)


def _assignment_value_and_targets(node: ast.Assign | ast.AnnAssign) -> tuple[ast.expr | None, list[ast.expr]]:
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets)
    return node.value, [node.target]


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


def _global_get_by_text_wait_for_diagnostic(
    node: ast.Call,
    aliases: Mapping[str, bool],
) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "wait_for":
        return None
    is_global_text_locator, has_narrowing = _global_get_by_text_locator_chain(func.value, aliases)
    if not is_global_text_locator or has_narrowing:
        return None
    return CodeBlockPreflightDiagnostic(
        code="GLOBAL_GET_BY_TEXT_WAIT_FOR",
        message=(
            "Code block waits on global `page.get_by_text(...).wait_for(...)`, which can collide with "
            "multiple matching text nodes under Playwright strict mode. Scope the text lookup "
            "through a locator/container or narrow it with `first`, `nth`, or `filter` before waiting."
        ),
    )


def _global_get_by_text_locator_chain(node: ast.expr, aliases: Mapping[str, bool]) -> tuple[bool, bool]:
    if _is_global_page_get_by_text_call(node):
        return True, False
    if isinstance(node, ast.Name) and node.id in aliases:
        return True, aliases[node.id]
    if isinstance(node, ast.Attribute):
        is_global_text_locator, has_narrowing = _global_get_by_text_locator_chain(node.value, aliases)
        if is_global_text_locator and node.attr in _GET_BY_TEXT_NARROWING_ATTRIBUTES:
            return True, True
        return is_global_text_locator, has_narrowing
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        is_global_text_locator, has_narrowing = _global_get_by_text_locator_chain(node.func.value, aliases)
        if is_global_text_locator and node.func.attr in _GET_BY_TEXT_NARROWING_METHODS:
            return True, True
        return is_global_text_locator, has_narrowing
    return False, False


def _global_table_wait_for_diagnostic(
    node: ast.Call,
    aliases: Mapping[str, bool],
) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "wait_for":
        return None
    is_global_table_locator, has_narrowing = _global_table_locator_chain(func.value, aliases)
    if not is_global_table_locator or has_narrowing:
        return None
    return CodeBlockPreflightDiagnostic(
        code="BROAD_GLOBAL_TABLE_WAIT_FOR",
        message=(
            "Code block waits on broad `page.locator('table').wait_for(...)`. Pages can contain hidden layout "
            "tables while the intended content region is ready. Wait on a scoped container or a row-level/narrowed "
            "table locator before extracting output."
        ),
    )


def _is_table_locator_selector(selector: str) -> bool:
    return selector.strip().casefold() == "table"


def _global_table_locator_chain(node: ast.expr, aliases: Mapping[str, bool]) -> tuple[bool, bool]:
    return _global_selector_locator_chain(node, aliases, _is_table_locator_selector, _LOCATOR_NARROWING_METHODS)


def _global_root_locator_chain(node: ast.expr, aliases: Mapping[str, bool]) -> tuple[bool, bool]:
    return _global_selector_locator_chain(node, aliases, is_root_locator_selector, _ROOT_NARROWING_METHODS)


def _global_selector_locator_chain(
    node: ast.expr,
    aliases: Mapping[str, bool],
    matches_selector: Callable[[str], bool],
    narrowing_methods: frozenset[str],
) -> tuple[bool, bool]:
    if _is_global_page_selector_locator_call(node, matches_selector):
        return True, False
    if isinstance(node, ast.Name) and node.id in aliases:
        return True, aliases[node.id]
    if isinstance(node, ast.Attribute):
        return _global_selector_locator_chain(node.value, aliases, matches_selector, narrowing_methods)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        is_selector_rooted, has_narrowing = _global_selector_locator_chain(
            node.func.value, aliases, matches_selector, narrowing_methods
        )
        if is_selector_rooted and node.func.attr in narrowing_methods:
            return True, True
        return is_selector_rooted, has_narrowing
    return False, False


def _is_global_page_selector_locator_call(node: ast.expr, matches_selector: Callable[[str], bool]) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "locator"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "page"
        and node.args
    ):
        return False
    selector = node.args[0]
    return isinstance(selector, ast.Constant) and isinstance(selector.value, str) and matches_selector(selector.value)


def _root_readiness_wait_diagnostic(
    node: ast.Call,
    aliases: Mapping[str, bool],
) -> CodeBlockPreflightDiagnostic | None:
    if not _is_root_readiness_wait(node, aliases):
        return None
    return CodeBlockPreflightDiagnostic(
        code="ROOT_CONTAINER_READINESS_WAIT",
        message=(
            "Code block waits on a root container (`body`, `html`, `:root`, `*`) for page readiness. Every "
            "document already has one, so the wait encodes no precondition: it either passes immediately or "
            "burns its whole timeout with the container already resolved. Wait on the element whose content "
            "the block goes on to read, so a failed wait names what was actually missing."
        ),
    )


def _root_container_text_read_diagnostic(
    node: ast.Call,
    aliases: Mapping[str, bool],
) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _WHOLE_PAGE_READ_METHODS:
        return None
    is_root_locator, has_narrowing = _global_root_locator_chain(func.value, aliases)
    if not is_root_locator or has_narrowing:
        return None
    return CodeBlockPreflightDiagnostic(
        code="ROOT_CONTAINER_TEXT_READ",
        message=(
            "Code block reads text off a root container (`body`, `html`, `:root`, `*`) and scans the result. "
            "The value then depends on unrelated page copy, and there is no element whose readiness the block "
            "can wait on. Target the element that carries the value — `get_by_text`, `get_by_role`, or a "
            "selector verified on this page — and read that."
        ),
    )


def _is_root_readiness_wait(node: ast.Call, aliases: Mapping[str, bool]) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "wait_for":
        if not _waits_for_readiness_state(node):
            return False
        is_root_locator, has_narrowing = _global_root_locator_chain(func.value, aliases)
        return is_root_locator and not has_narrowing
    if func.attr == "wait_for_selector":
        if not (isinstance(func.value, ast.Name) and func.value.id == "page" and node.args):
            return False
        selector = node.args[0]
        if not (isinstance(selector, ast.Constant) and isinstance(selector.value, str)):
            return False
        return is_root_locator_selector(selector.value) and _waits_for_readiness_state(node)
    if func.attr in _READINESS_EXPECTATION_METHODS:
        subject = _expectation_subject(func.value)
        if subject is None:
            return False
        is_root_locator, has_narrowing = _global_root_locator_chain(subject, aliases)
        return is_root_locator and not has_narrowing
    return False


def _expectation_subject(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "expect" and node.args:
        return node.args[0]
    return None


def _waits_for_readiness_state(node: ast.Call) -> bool:
    """False for disappearance waits (`hidden`/`detached`), where a root container is a deliberate target."""
    state = next((kwarg.value for kwarg in node.keywords if kwarg.arg == "state"), None)
    if state is None:
        return True
    if not isinstance(state, ast.Constant) or not isinstance(state.value, str):
        return False
    return state.value.strip().casefold() in _READINESS_WAIT_STATES


def _is_global_page_get_by_text_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_by_text"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "page"
    )


def _broad_body_text_wait_for_function_diagnostic(node: ast.Call) -> CodeBlockPreflightDiagnostic | None:
    func = node.func
    if (
        not isinstance(func, ast.Attribute)
        or func.attr != "wait_for_function"
        or not isinstance(func.value, ast.Name)
        or func.value.id != "page"
    ):
        return None

    script: ast.expr | None
    if node.args:
        script = node.args[0]
    else:
        expression_keyword = next((keyword for keyword in node.keywords if keyword.arg == "expression"), None)
        script = expression_keyword.value if expression_keyword is not None else None
    if not isinstance(script, ast.Constant) or not isinstance(script.value, str):
        return None

    normalized_script = re.sub(r"\s+", "", script.value).lower()
    if not any(needle in normalized_script for needle in _BROAD_BODY_TEXT_WAIT_NEEDLES):
        return None

    return CodeBlockPreflightDiagnostic(
        code="BROAD_DOCUMENT_BODY_TEXT_WAIT",
        message=(
            "Code block waits for broad `document.body` text with `page.wait_for_function`. "
            "Target content can be visible while body-level polling still times out. "
            "Wait on a localized container or visible field text, then extract and return "
            "a keyed record from that region."
        ),
    )


def _broad_table_record_scan_diagnostic(tree: ast.AST) -> CodeBlockPreflightDiagnostic | None:
    selector_aliases = _selector_alias_values(tree)
    record_keys: set[str] = set()
    broad_selectors: set[str] = set()
    row_selector_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    record_keys.add(key.value.casefold())
        selector_arg = _locator_selector_arg(node)
        if selector_arg is not None:
            for selector in _selector_values(selector_arg, selector_aliases):
                selector = selector.strip().casefold()
                if selector in _BROAD_TABLE_SCAN_SELECTORS:
                    broad_selectors.add(selector)
                if _TABLE_ROW_TAG_SELECTOR_RE.search(selector) or _TABLE_ROW_ROLE_SELECTOR_RE.search(selector):
                    row_selector_found = True

    if not any(key in record_keys for key in _BROAD_TABLE_RECORD_KEYS):
        return None
    if not broad_selectors or row_selector_found:
        return None
    if broad_selectors == _LONE_LIST_ITEM_SELECTOR_EXEMPTION:
        return None

    return CodeBlockPreflightDiagnostic(
        code="BROAD_TABLE_RECORD_SCAN",
        message=(
            "Code block appears to extract row-like records by scanning broad containers such as `section`, "
            "`.card`, `article`, or `li`. For table-like or list-like records, iterate the actual row/item "
            'elements (`tr`, `[role="row"]`, or equivalent repeated item containers) and read fields from '
            "the same row so fields from separate records cannot be mixed. Derive summary status fields only "
            "from parsed row objects."
        ),
    )


def _literal_selector_values(expr: ast.AST) -> set[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        values: set[str] = set()
        for element in expr.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                values.add(element.value)
        return values
    return set()


def _selector_alias_values(tree: ast.AST) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
            value = node.iter
        if value is None:
            continue
        selector_values = _literal_selector_values(value)
        if not selector_values:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = selector_values
    return aliases


def _selector_values(selector_arg: ast.AST, selector_aliases: dict[str, set[str]]) -> set[str]:
    values = _literal_selector_values(selector_arg)
    if values:
        return values
    if isinstance(selector_arg, ast.Name):
        return selector_aliases.get(selector_arg.id, set())
    return set()


def _locator_selector_arg(node: ast.AST) -> ast.AST | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _BROAD_TABLE_SELECTOR_METHODS:
        return None
    return node.args[0]


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
