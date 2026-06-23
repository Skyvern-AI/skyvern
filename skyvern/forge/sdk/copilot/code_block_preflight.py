"""Static preflight checks for generated Workflow Copilot code blocks."""

from __future__ import annotations

import ast
import keyword
import re
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from skyvern.forge.sdk.copilot.code_block_security import CodeBlockSecurityError, author_time_code_security_errors
from skyvern.forge.sdk.workflow.models.block import CodeBlock


@dataclass(frozen=True)
class CodeBlockPreflightDiagnostic:
    code: str
    message: str


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\([AB]")
_MYPY_ERROR_RE = re.compile(r"^(?P<path>.*?):(?P<line>\d+): (?P<severity>error): (?P<message>.*)$")
_LOCATOR_NOT_CALLABLE_RE = re.compile(r'"Locator" not callable\s+\[operator\]')
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
_TABLE_ROW_TAG_SELECTOR_RE = re.compile(r"(?<![a-z0-9_-])tr(?![a-z0-9_-])")
_TABLE_ROW_ROLE_SELECTOR_RE = re.compile(r"\[role\s*=\s*(['\"]?)row\1\]")


@cache
def _sandbox_global_names() -> frozenset[str]:
    # Keep this derived from the runtime sandbox, but resolve it lazily so the
    # analyzer does not bind itself to CodeBlock import-time behavior.
    return frozenset(name for name in CodeBlock.build_safe_vars() if not name.startswith("__")) | {"page"}


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


def sandbox_unresolved_name_diagnostics(
    code: str,
    *,
    parameter_keys: Iterable[str] = (),
) -> list[CodeBlockPreflightDiagnostic]:
    """Find names that the generated code-block sandbox cannot resolve.

    This models the runtime wrapper from ``CodeBlock.generate_async_user_function``:
    sandbox helpers and ``page`` are exec globals, while valid block parameter
    keys become wrapper default-argument locals. The analysis is conservative;
    ambiguous control-flow bindings do not satisfy later reads.
    """

    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return []

    unresolved_names, class_names = _SandboxNameAnalyzer(parameter_keys=parameter_keys).analyze(tree.body)
    if not unresolved_names and not class_names:
        return []

    names = sorted(unresolved_names)
    rejected_classes = sorted(class_names)
    detail_parts: list[str] = []
    if names:
        detail_parts.append(f"unresolved names: {', '.join(f'`{name}`' for name in names)}")
    if rejected_classes:
        detail_parts.append(
            "class definitions unavailable in the code sandbox: " + ", ".join(f"`{name}`" for name in rejected_classes)
        )
    detail = "; ".join(detail_parts)
    return [
        CodeBlockPreflightDiagnostic(
            code="SANDBOX_UNRESOLVED_NAME",
            message=(
                f"Code block references names that are unavailable in the runtime code sandbox or are not "
                f"definitely initialized before use ({detail}). The sandbox provides `page`, declared code-block "
                "parameter keys, and its explicit safe helper namespace; `Exception` is the only available "
                "exception type."
            ),
        )
    ]


def author_time_code_block_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    tree, _ = _parse_static_ast(code)
    if tree is None:
        return []
    return [*_author_time_security_diagnostics(code), *_author_time_ast_diagnostics(tree)]


def _static_ast_diagnostics(code: str) -> list[CodeBlockPreflightDiagnostic]:
    tree, syntax_error = _parse_static_ast(code)
    if syntax_error is not None:
        return [syntax_error]
    if tree is None:
        return []

    diagnostics = [*_author_time_security_diagnostics(code), *_author_time_ast_diagnostics(tree)]
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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        body_text_wait_diagnostic = _broad_body_text_wait_for_function_diagnostic(node)
        if body_text_wait_diagnostic is not None:
            diagnostics.append(body_text_wait_diagnostic)
    return diagnostics


class _SandboxNameAnalyzer:
    def __init__(self, *, parameter_keys: Iterable[str], outer_names: Iterable[str] = ()) -> None:
        self.parameter_names = {key for key in parameter_keys if _valid_python_identifier(key)}
        self.outer_names = set(outer_names)
        self.unresolved_names: set[str] = set()
        self.class_names: set[str] = set()

    def analyze(self, statements: list[ast.stmt]) -> tuple[set[str], set[str]]:
        local_names = self._local_names(statements)
        function_names = self._function_names(statements)
        self._statements(
            statements,
            set(self.parameter_names),
            local_names=local_names,
            function_names=function_names,
        )
        return self.unresolved_names, self.class_names

    def _report_name(self, name: str) -> None:
        if not name.startswith("__"):
            self.unresolved_names.add(name)

    def _local_names(self, statements: list[ast.stmt]) -> set[str]:
        names: set[str] = set()

        class Collector(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                names.add(node.name)

            visit_AsyncFunctionDef = visit_FunctionDef

            def _skip_nested_scope(self, node: ast.AST) -> None:
                return

            visit_Lambda = visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _skip_nested_scope

            def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
                names.update(_target_names(node.target))
                self.visit(node.value)

            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                if node.name:
                    names.add(node.name)
                if node.type is not None:
                    self.visit(node.type)
                for statement in node.body:
                    self.visit(statement)

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".", 1)[0])

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for alias in node.names:
                    names.add(alias.asname or alias.name)

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                names.add(node.name)

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    names.add(node.id)

        collector = Collector()
        for statement in statements:
            collector.visit(statement)
        return names

    def _function_names(self, statements: list[ast.stmt]) -> set[str]:
        return {
            statement.name for statement in statements if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def _statements(
        self,
        statements: list[ast.stmt],
        initialized: set[str],
        *,
        local_names: set[str],
        function_names: set[str],
    ) -> set[str]:
        current = set(initialized)
        for statement in statements:
            current = self._statement(
                statement,
                current,
                local_names=local_names,
                function_names=function_names,
            )
        return current

    def _statement(
        self,
        node: ast.stmt,
        initialized: set[str],
        *,
        local_names: set[str],
        function_names: set[str],
    ) -> set[str]:
        if isinstance(node, ast.Assign):
            self._expr(node.value, initialized, local_names=local_names)
            return self._store(node.targets, initialized)
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._expr(node.value, initialized, local_names=local_names)
                return self._store([node.target], initialized)
            return initialized
        if isinstance(node, ast.AugAssign):
            self._augmented_target_read(node.target, initialized, local_names=local_names)
            self._expr(node.value, initialized, local_names=local_names)
            return self._store([node.target], initialized)
        if isinstance(node, ast.Delete):
            return initialized - {name for target in node.targets for name in _target_names(target)}
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._expr(node.iter, initialized, local_names=local_names)
            self._statements(
                node.body,
                self._store([node.target], initialized),
                local_names=local_names,
                function_names=function_names,
            )
            self._statements(
                node.orelse,
                set(initialized),
                local_names=local_names,
                function_names=function_names,
            )
            return initialized
        if isinstance(node, ast.While):
            self._expr(node.test, initialized, local_names=local_names)
            self._statements(node.body, set(initialized), local_names=local_names, function_names=function_names)
            self._statements(node.orelse, set(initialized), local_names=local_names, function_names=function_names)
            return initialized
        if isinstance(node, ast.If):
            self._expr(node.test, initialized, local_names=local_names)
            body = self._statements(node.body, set(initialized), local_names=local_names, function_names=function_names)
            orelse = self._statements(
                node.orelse,
                set(initialized),
                local_names=local_names,
                function_names=function_names,
            )
            return body & orelse
        if isinstance(node, (ast.With, ast.AsyncWith)):
            current = set(initialized)
            for item in node.items:
                self._expr(item.context_expr, current, local_names=local_names)
                if item.optional_vars is not None:
                    current = self._store([item.optional_vars], current)
            return self._statements(node.body, current, local_names=local_names, function_names=function_names)
        if isinstance(node, (ast.Try, ast.TryStar)):
            normal = self._statements(
                node.orelse,
                self._statements(
                    node.body,
                    set(initialized),
                    local_names=local_names,
                    function_names=function_names,
                ),
                local_names=local_names,
                function_names=function_names,
            )
            branches = [normal]
            for handler in node.handlers:
                if handler.type is not None:
                    self._expr(handler.type, initialized, local_names=local_names)
                handler_state = set(initialized)
                if handler.name:
                    handler_state.add(handler.name)
                handler_state = self._statements(
                    handler.body,
                    handler_state,
                    local_names=local_names,
                    function_names=function_names,
                )
                if handler.name:
                    handler_state.discard(handler.name)
                branches.append(handler_state)
            return self._statements(
                node.finalbody,
                set.intersection(*branches),
                local_names=local_names,
                function_names=function_names,
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for expr in _definition_expressions(node):
                self._expr(expr, initialized, local_names=local_names)
            self._analyze_nested_function(node, initialized, sibling_function_names=function_names)
            return {*initialized, node.name}
        if isinstance(node, ast.ClassDef):
            self.class_names.add(node.name)
            for expr in [*node.decorator_list, *node.bases, *[keyword.value for keyword in node.keywords]]:
                self._expr(expr, initialized, local_names=local_names)
            return initialized

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._expr(child, initialized, local_names=local_names)
            elif isinstance(child, ast.stmt):
                initialized = self._statement(
                    child,
                    initialized,
                    local_names=local_names,
                    function_names=function_names,
                )
        return initialized

    def _analyze_nested_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        initialized: set[str],
        *,
        sibling_function_names: set[str],
    ) -> None:
        parameter_names = _argument_names(node.args)
        nested = _SandboxNameAnalyzer(
            parameter_keys=parameter_names,
            outer_names=initialized | self.outer_names | sibling_function_names | {node.name},
        )
        unresolved_names, class_names = nested.analyze(node.body)
        self.unresolved_names.update(unresolved_names)
        self.class_names.update(class_names)

    def _expr(self, node: ast.expr, initialized: set[str], *, local_names: set[str]) -> None:
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if node.id in initialized:
                    return
                if node.id in local_names:
                    self._report_name(node.id)
                elif node.id not in _sandbox_global_names() and node.id not in self.outer_names:
                    self._report_name(node.id)
            return
        if isinstance(node, ast.NamedExpr):
            self._expr(node.value, initialized, local_names=local_names)
            # Named expressions bind into the surrounding scope immediately.
            initialized.update(_target_names(node.target))
            return
        if isinstance(node, ast.Lambda):
            for expr in [*node.args.defaults, *[default for default in node.args.kw_defaults if default is not None]]:
                self._expr(expr, initialized, local_names=local_names)
            parameters = _argument_names(node.args)
            nested = _SandboxNameAnalyzer(parameter_keys=parameters, outer_names=initialized | self.outer_names)
            nested._expr(node.body, set(parameters), local_names=nested._local_names([ast.Expr(value=node.body)]))
            self.unresolved_names.update(nested.unresolved_names)
            self.class_names.update(nested.class_names)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            self._comprehension([node.elt], node.generators, initialized, local_names=local_names)
            return
        if isinstance(node, ast.DictComp):
            self._comprehension([node.key, node.value], node.generators, initialized, local_names=local_names)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._expr(child, initialized, local_names=local_names)

    def _comprehension(
        self,
        values: list[ast.expr],
        generators: list[ast.comprehension],
        initialized: set[str],
        *,
        local_names: set[str],
    ) -> None:
        comp_state = set(initialized)
        comp_locals = set(local_names)
        for generator in generators:
            self._expr(generator.iter, comp_state, local_names=local_names)
            comp_state = self._store([generator.target], comp_state)
            comp_locals.update(_target_names(generator.target))
            for condition in generator.ifs:
                self._expr(condition, comp_state, local_names=comp_locals)
        for value in values:
            self._expr(value, comp_state, local_names=comp_locals)

    def _store(self, targets: list[ast.expr], initialized: set[str]) -> set[str]:
        next_initialized = set(initialized)
        for target in targets:
            next_initialized.update(_target_names(target))
        return next_initialized

    def _augmented_target_read(self, node: ast.expr, initialized: set[str], *, local_names: set[str]) -> None:
        if isinstance(node, ast.Name):
            if node.id not in initialized:
                self._report_name(node.id)
            return
        self._expr(node, initialized, local_names=local_names)


def _argument_names(args: ast.arguments) -> set[str]:
    return {
        arg.arg
        for arg in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg is not None else []),
            *([args.kwarg] if args.kwarg is not None else []),
        ]
    }


def _definition_expressions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = node.args
    annotations = [
        arg.annotation
        for arg in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg is not None else []),
            *([args.kwarg] if args.kwarg is not None else []),
        ]
        if arg.annotation is not None
    ]
    return [
        *node.decorator_list,
        *args.defaults,
        *[default for default in args.kw_defaults if default is not None],
        *annotations,
        *([node.returns] if node.returns is not None else []),
    ]


def _target_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names.update(_target_names(element))
    elif isinstance(node, ast.Starred):
        names.update(_target_names(node.value))
    return names


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
            "Loaded result/detail pages can be visible while body-level polling still times out. "
            "Wait on a localized result/detail locator or visible field text, then extract and return "
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
