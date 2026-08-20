"""Shared static safety checks for CodeBlock Python snippets."""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from collections.abc import Callable
from string import Formatter

from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected

# Keep this policy aligned with codeblock/codeblock_safety.py; the runner image carries a local copy.
BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "subprocess_exec",
        "subprocess_shell",
        "system",
        "popen",
        "Popen",
        "exec",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "check_call",
        "check_output",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fexecve",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "fork",
        "forkpty",
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
        "dictConfig",
        "fileConfig",
        "locals",
        "eval",
        "vars",
        "format",
        "format_map",
        "vformat",
        "get_field",
        "get_type_hints",
    }
)
ALLOWED_SCRIPT_ATTRS: frozenset[str] = frozenset(
    {"format", "listdir", "makedirs", "rmdir", "stderr", "stdin", "stdout"}
)
BLOCKED_SCRIPT_ATTRS: frozenset[str] = (BLOCKED_ATTRS - ALLOWED_SCRIPT_ATTRS) | {"config", "read_pickle"}
BLOCKED_SCRIPT_IMPORTS: frozenset[str] = frozenset(
    {
        "antigravity",
        "bdb",
        "builtins",
        "cProfile",
        "code",
        "codeop",
        "ctypes",
        "dis",
        "doctest",
        "imp",
        "importlib",
        "inspect",
        "marshal",
        "multiprocessing",
        "operator",
        "nt",
        "pdb",
        "pickle",
        "pkgutil",
        "posix",
        "posixsubprocess",
        "profile",
        "pydoc",
        "pyrepl",
        "runpy",
        "site",
        "socket",
        "subprocess",
        "thread",
        "timeit",
        "trace",
        "webbrowser",
        "zipfile",
        "zipimport",
    }
)
BLOCKED_SCRIPT_IMPORT_PATHS: frozenset[str] = frozenset({"logging.config"})
BLOCKED_SCRIPT_BUILTINS: frozenset[str] = frozenset(
    {"__import__", "compile", "delattr", "eval", "exec", "getattr", "globals", "locals", "setattr", "vars"}
)
BLOCKED_SCRIPT_ESCAPE_ATTRS: frozenset[str] = frozenset(
    {
        "__bases__",
        "__builtins__",
        "__class__",
        "__code__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "__base__",
        "__delattr__",
        "__getattr__",
        "__getattribute__",
        "__init_subclass__",
        "__reduce__",
        "__reduce_ex__",
        "__setattr__",
    }
)
# ``self`` is an ordinary identifier that a module-level script can rebind to any object, so the
# self/super exemption is a name match, not proof of an instance method. Limit it to the parent-call
# dunders real subclasses need; every other dunder falls back to the private-attribute refusal.
ALLOWED_SELF_SUPER_DUNDERS: frozenset[str] = frozenset({"__init__", "__post_init__", "__enter__", "__exit__"})
ALLOWED_SCRIPT_DUNDER_READS: frozenset[str] = frozenset({"__all__", "__doc__", "__file__", "__name__"})
ALLOWED_SCRIPT_DUNDER_WRITES: frozenset[str] = frozenset({"__all__"})
ALLOWED_SCRIPT_NAMEDTUPLE_ATTRS: frozenset[str] = frozenset({"_asdict", "_fields", "_make", "_replace"})


def _import_roots(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name.partition(".")[0] for alias in node.names)
    if node.level or node.module is None:
        return ()
    return (node.module.partition(".")[0],)


def _import_paths(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if node.level or node.module is None:
        return ()
    return (node.module, *(f"{node.module}.{alias.name}" for alias in node.names))


def _normalized_import_root(module_name: str) -> str:
    return module_name.lstrip("_")


def _is_self_or_super(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "self") or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"
    )


def _format_field_uses_unsafe_traversal(field_name: str) -> bool:
    components = field_name.replace("[", ".").replace("]", ".").split(".")[1:]
    return any(
        component in BLOCKED_SCRIPT_ATTRS
        or component in BLOCKED_SCRIPT_ESCAPE_ATTRS
        or (component.startswith("__") and component.endswith("__"))
        for component in components
        if component
    )


def _format_string_uses_unsafe_traversal(format_string: str) -> bool:
    pending = [format_string]
    while pending:
        try:
            for _, field_name, format_spec, _ in Formatter().parse(pending.pop()):
                if field_name is not None and _format_field_uses_unsafe_traversal(field_name):
                    return True
                if format_spec:
                    pending.append(format_spec)
        except ValueError:
            continue
    return False


def _script_bindings(
    tree: ast.AST,
) -> tuple[dict[str, list[ast.expr]], dict[str, set[str]], Counter[str]]:
    assignment_values: dict[str, list[ast.expr]] = {}
    import_paths: dict[str, set[str]] = {}
    binding_counts: Counter[str] = Counter()

    def add_assignment(name: str, value: ast.expr) -> None:
        assignment_values.setdefault(name, []).append(value)

    def add_import(name: str, path: str) -> None:
        binding_counts[name] += 1
        import_paths.setdefault(name, set()).add(path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            binding_counts[node.id] += 1
        elif isinstance(node, ast.arg):
            binding_counts[node.arg] += 1
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            binding_counts[node.name] += 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binding_counts[node.name] += 1
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            binding_counts[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            binding_counts[node.rest] += 1

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add_assignment(target.id, node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            add_assignment(node.target.id, node.value)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            add_assignment(node.target.id, node.value)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                add_import(bound_name, alias.name if alias.asname else bound_name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if node.level or node.module is None:
                    binding_counts[bound_name] += 1
                else:
                    add_import(bound_name, f"{node.module}.{alias.name}")

    return assignment_values, import_paths, binding_counts


def _constant_string(
    node: ast.expr,
    assignment_values: dict[str, list[ast.expr]],
    binding_counts: Counter[str],
    resolving: frozenset[str] = frozenset(),
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, assignment_values, binding_counts, resolving)
        right = _constant_string(node.right, assignment_values, binding_counts, resolving)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Name) and node.id not in resolving:
        values = assignment_values.get(node.id, [])
        if binding_counts[node.id] == 1 and len(values) == 1:
            return _constant_string(values[0], assignment_values, binding_counts, resolving | {node.id})
    return None


def _constant_format_string(
    node: ast.Call,
    assignment_values: dict[str, list[ast.expr]],
    binding_counts: Counter[str],
) -> str | None:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "format":
        return None
    format_string = _constant_string(node.func.value, assignment_values, binding_counts)
    if format_string is not None:
        return format_string
    if isinstance(node.func.value, ast.Name) and node.func.value.id == "str" and node.args:
        return _constant_string(node.args[0], assignment_values, binding_counts)
    return None


def _import_paths_for_expression(
    node: ast.expr,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
    resolving: frozenset[str] = frozenset(),
) -> set[str]:
    if isinstance(node, ast.Name):
        paths = set(import_paths.get(node.id, set()))
        if node.id not in resolving:
            for value in assignment_values.get(node.id, []):
                paths.update(
                    _import_paths_for_expression(value, assignment_values, import_paths, resolving | {node.id})
                )
        return paths
    if isinstance(node, ast.Attribute):
        return {
            f"{path}.{node.attr}"
            for path in _import_paths_for_expression(node.value, assignment_values, import_paths, resolving)
        }
    return set()


def _numpy_load_enables_pickle(
    node: ast.Call,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
) -> bool:
    if not _is_numpy_load_reference(node.func, assignment_values, import_paths):
        return False
    for keyword in node.keywords:
        if keyword.arg is None:
            return True
        if keyword.arg == "allow_pickle":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    if len(node.args) >= 3:
        return not (isinstance(node.args[2], ast.Constant) and node.args[2].value is False)
    return False


def _is_numpy_load_reference(
    node: ast.expr,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
) -> bool:
    paths = _import_paths_for_expression(node, assignment_values, import_paths)
    return any(path.partition(".")[0] == "numpy" and path.rpartition(".")[2] == "load" for path in paths)


LOADER_ARGUMENT_YAML_FUNCTIONS: frozenset[str] = frozenset({"load", "load_all", "unsafe_load", "unsafe_load_all"})

SAFE_YAML_LOADER_NAMES: frozenset[str] = frozenset(
    {"BaseLoader", "CBaseLoader", "CSafeLoader", "FullLoader", "CFullLoader", "SafeLoader"}
)


def _is_yaml_load_reference(
    node: ast.expr,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
) -> bool:
    paths = _import_paths_for_expression(node, assignment_values, import_paths)
    return any(
        path.partition(".")[0] == "yaml" and path.rpartition(".")[2] in LOADER_ARGUMENT_YAML_FUNCTIONS for path in paths
    )


def _is_explicit_safe_yaml_loader(
    node: ast.expr,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
) -> bool:
    paths = _import_paths_for_expression(node, assignment_values, import_paths)
    return bool(paths) and all(
        path.partition(".")[0] == "yaml" and path.rpartition(".")[2] in SAFE_YAML_LOADER_NAMES for path in paths
    )


def _yaml_load_uses_unsafe_loader(
    node: ast.Call,
    assignment_values: dict[str, list[ast.expr]],
    import_paths: dict[str, set[str]],
) -> bool:
    paths = _import_paths_for_expression(node.func, assignment_values, import_paths)
    yaml_load_paths = {
        path
        for path in paths
        if path.partition(".")[0] == "yaml" and path.rpartition(".")[2] in LOADER_ARGUMENT_YAML_FUNCTIONS
    }
    if not yaml_load_paths:
        return False
    if any(path.rpartition(".")[2] in {"unsafe_load", "unsafe_load_all"} for path in yaml_load_paths):
        return True
    for keyword in node.keywords:
        if keyword.arg is None:
            return True
        if keyword.arg == "Loader":
            return not _is_explicit_safe_yaml_loader(keyword.value, assignment_values, import_paths)
    if len(node.args) >= 2:
        return not _is_explicit_safe_yaml_loader(node.args[1], assignment_values, import_paths)
    return True


def _validate_tree(
    tree: ast.AST,
    *,
    blocked_imports: frozenset[str] | None,
    blocked_import_paths: frozenset[str] = frozenset(),
    blocked_names: frozenset[str],
    error_factory: Callable[[str], Exception],
    blocked_attrs: frozenset[str] = BLOCKED_ATTRS,
    allowed_dunder_reads: frozenset[str] = frozenset(),
    allowed_dunder_writes: frozenset[str] = frozenset(),
    allowed_private_reads: frozenset[str] = frozenset(),
    blocked_private_attrs: frozenset[str] = frozenset(),
    allow_self_super_private_attrs: bool = False,
    check_constant_format_fields: bool = False,
    check_unsafe_loaders: bool = False,
    block_class_keywords: bool = False,
) -> None:
    assignment_values, import_paths, binding_counts = _script_bindings(tree)
    directly_called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if block_class_keywords and isinstance(node, ast.ClassDef) and node.keywords:
            raise error_factory("Not allowed to use metaclasses or keywords in class definitions")
        if isinstance(node, ast.Attribute) and node.attr in blocked_private_attrs:
            raise error_factory(f"Not allowed to access '{node.attr}'")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            is_allowed_read = isinstance(node.ctx, ast.Load) and (
                node.attr in allowed_dunder_reads or node.attr in allowed_private_reads
            )
            is_dunder = node.attr.startswith("__") and node.attr.endswith("__")
            is_self_super_private = (
                allow_self_super_private_attrs
                and _is_self_or_super(node.value)
                and (not is_dunder or node.attr in ALLOWED_SELF_SUPER_DUNDERS)
            )
            if not is_allowed_read and not is_self_super_private:
                raise error_factory("Not allowed to access private methods or attributes")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in blocked_names:
            raise error_factory(f"Not allowed to use '{node.id}'")
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value in blocked_names
        ):
            raise error_factory(f"Not allowed to use '{node.slice.value}'")
        if (
            isinstance(node, ast.Name)
            and node.id.startswith("__")
            and not (
                (isinstance(node.ctx, ast.Load) and node.id in allowed_dunder_reads)
                or (isinstance(node.ctx, ast.Store) and node.id in allowed_dunder_writes)
            )
        ):
            raise error_factory("Not allowed to access private methods or attributes")
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                raise error_factory("Wildcard imports are not allowed")
            if blocked_imports is None:
                raise error_factory("Not allowed to import modules")
            for module_name in _import_roots(node):
                if _normalized_import_root(module_name) in blocked_imports:
                    raise error_factory(f"Not allowed to import module '{module_name}'")
            for module_path in _import_paths(node):
                if module_path in blocked_import_paths:
                    raise error_factory(f"Not allowed to import module '{module_path}'")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (
                        alias.name in blocked_attrs
                        or alias.name in blocked_names
                        or (
                            alias.name.startswith("_")
                            and alias.name not in allowed_dunder_reads | allowed_private_reads
                        )
                    ):
                        raise error_factory(f"Not allowed to import '{alias.name}'")
        if (
            blocked_imports is not None
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in blocked_attrs
        ):
            raise error_factory(f"Not allowed to use '{node.func.id}'")
        if (
            check_constant_format_fields
            and isinstance(node, ast.Attribute)
            and node.attr == "format"
            and id(node) not in directly_called
        ):
            raise error_factory("Format method must be called directly")
        if (
            check_constant_format_fields
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            format_string = _constant_format_string(node, assignment_values, binding_counts)
            if format_string is None:
                raise error_factory("Format string must be statically resolvable")
            if _format_string_uses_unsafe_traversal(format_string):
                raise error_factory("Not allowed to traverse attributes or subscripts in a format field")
        if (
            check_unsafe_loaders
            and isinstance(node, ast.Call)
            and _numpy_load_enables_pickle(node, assignment_values, import_paths)
        ):
            raise error_factory("Not allowed to enable pickle loading")
        if (
            check_unsafe_loaders
            and isinstance(node, ast.Name | ast.Attribute)
            and id(node) not in directly_called
            and _is_numpy_load_reference(node, assignment_values, import_paths)
        ):
            raise error_factory("NumPy load must be called directly")
        if (
            check_unsafe_loaders
            and isinstance(node, ast.Call)
            and _yaml_load_uses_unsafe_loader(node, assignment_values, import_paths)
        ):
            raise error_factory("Not allowed to use an unsafe YAML loader")
        if (
            check_unsafe_loaders
            and isinstance(node, ast.Name | ast.Attribute)
            and id(node) not in directly_called
            and _is_yaml_load_reference(node, assignment_values, import_paths)
        ):
            raise error_factory("YAML load must be called directly")
        if isinstance(node, ast.Attribute) and node.attr in blocked_attrs:
            raise error_factory(f"Not allowed to access '{node.attr}'")


def is_safe_code(
    code: str,
    *,
    error_factory: Callable[[str], Exception] = InsecureCodeDetected,
) -> None:
    """Reject imports, private members, and known escape hatches."""
    tree = ast.parse(textwrap.dedent(code))
    _validate_tree(
        tree,
        blocked_imports=None,
        blocked_names=frozenset(),
        error_factory=error_factory,
        block_class_keywords=True,
    )


def is_safe_script_code(
    code: str,
    *,
    error_factory: Callable[[str], Exception] = InsecureCodeDetected,
) -> None:
    tree = ast.parse(code)
    _validate_tree(
        tree,
        blocked_imports=BLOCKED_SCRIPT_IMPORTS,
        blocked_import_paths=BLOCKED_SCRIPT_IMPORT_PATHS,
        blocked_names=BLOCKED_SCRIPT_BUILTINS | BLOCKED_SCRIPT_ESCAPE_ATTRS,
        error_factory=error_factory,
        blocked_attrs=BLOCKED_SCRIPT_ATTRS,
        allowed_dunder_reads=ALLOWED_SCRIPT_DUNDER_READS,
        allowed_dunder_writes=ALLOWED_SCRIPT_DUNDER_WRITES,
        allowed_private_reads=ALLOWED_SCRIPT_NAMEDTUPLE_ATTRS,
        blocked_private_attrs=BLOCKED_SCRIPT_ESCAPE_ATTRS,
        allow_self_super_private_attrs=True,
        check_constant_format_fields=True,
        check_unsafe_loaders=True,
    )
