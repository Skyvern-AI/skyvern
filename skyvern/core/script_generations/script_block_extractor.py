from __future__ import annotations

import ast
import builtins
import textwrap
from dataclasses import dataclass
from typing import Any, Iterator

CACHEABLE_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "task",
        "task_v2",
        "action",
        "navigation",
        "extraction",
        "login",
        "file_download",
        "for_loop",
        "while_loop",
    }
)

KNOWN_NON_CACHEABLE_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "validation",
        "wait",
        "conditional",
        "code",
        "goto_url",
        "send_email",
        "file_url_parser",
        "pdf_parser",
        "http_request",
    }
)

RUNTIME_GLOBALS: frozenset[str] = frozenset({"skyvern", "__builtins__"})


class ScriptBlockExtractionError(ValueError):
    pass


class RunSignatureValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedScriptBlock:
    label: str
    primitive: str
    run_signature: str
    block_type: str | None
    is_cacheable: bool
    is_compound: bool
    missing_globals: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptBlockExtractionResult:
    blocks: tuple[ExtractedScriptBlock, ...]
    warnings: tuple[str, ...] = ()

    @property
    def cacheable_blocks(self) -> list[ExtractedScriptBlock]:
        return [block for block in self.blocks if block.is_cacheable]


def _iter_workflow_blocks(blocks: list[Any]) -> Iterator[dict[str, Any]]:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        yield block
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            yield from _iter_workflow_blocks(loop_blocks)


def _label_to_block_type(workflow_definition: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for block in _iter_workflow_blocks(workflow_definition.get("blocks") or []):
        label = block.get("label")
        block_type = block.get("block_type")
        if label and block_type:
            labels[label] = block_type
    return labels


def _is_skyvern_call(call: ast.AST) -> bool:
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "skyvern"


def _find_entry_function(tree: ast.Module) -> ast.AsyncFunctionDef | None:
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "skyvern"
                and decorator.func.attr == "workflow"
            ):
                return node

    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            return node
    return None


def _walk_entry_statements(nodes: list[ast.stmt], depth: int = 0) -> Iterator[tuple[str, ast.AST]]:
    if depth > 8:
        return

    for node in nodes:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await):
            yield ("await", node.value)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Await):
            yield ("await", node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Await):
            yield ("await", node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.value, ast.Await):
            yield ("await", node.value)
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Await):
            yield ("await", node.value)
        elif isinstance(node, ast.AsyncFor) and _is_skyvern_call(node.iter):
            iter_call = node.iter
            if not isinstance(iter_call, ast.Call):
                continue
            iter_func = iter_call.func
            if not isinstance(iter_func, ast.Attribute):
                continue
            if iter_func.attr in ("loop", "while_loop"):
                yield ("async_for", node)
                yield from _walk_entry_statements(node.body, depth + 1)
        elif isinstance(node, ast.Try):
            yield from _walk_entry_statements(node.body, depth + 1)
            for handler in node.handlers:
                yield from _walk_entry_statements(handler.body, depth + 1)
            yield from _walk_entry_statements(node.orelse, depth + 1)
            yield from _walk_entry_statements(node.finalbody, depth + 1)
        elif hasattr(ast, "TryStar") and isinstance(node, ast.TryStar):
            yield from _walk_entry_statements(node.body, depth + 1)
            for handler in node.handlers:
                yield from _walk_entry_statements(handler.body, depth + 1)
            yield from _walk_entry_statements(node.orelse, depth + 1)
            yield from _walk_entry_statements(node.finalbody, depth + 1)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _walk_entry_statements(node.body, depth + 1)
        elif isinstance(node, ast.If):
            yield from _walk_entry_statements(node.body, depth + 1)
            yield from _walk_entry_statements(node.orelse, depth + 1)


def _node_source(source_lines: list[str], node: ast.AST) -> str:
    start = (getattr(node, "lineno", 1) or 1) - 1
    end = getattr(node, "end_lineno", None) or start + 1
    start_col = getattr(node, "col_offset", 0) or 0
    end_col = getattr(node, "end_col_offset", 0) or 0
    if start == end - 1:
        return source_lines[start][start_col:end_col]
    chunks = [source_lines[start][start_col:]]
    chunks.extend(source_lines[start + 1 : end - 1])
    chunks.append(source_lines[end - 1][:end_col])
    return "".join(chunks)


def _extract_label_from_call(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "label" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def _add_target(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _add_target(elt)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _add_target(target)
        elif isinstance(node, ast.AnnAssign):
            _add_target(node.target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    raise RunSignatureValidationError("Wildcard imports are not supported for script deploy validation")
                names.add(alias.asname or alias.name)

    return names


def _entry_function_scope_names(entry_fn: ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    args = entry_fn.args

    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        names.add(arg.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)

    _loads, stores = _name_loads_and_stores(entry_fn)
    names.update(stores)
    return names


def _name_loads_and_stores(tree: ast.AST) -> tuple[set[str], set[str]]:
    loads: set[str] = set()
    stores: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loads.add(node.id)
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                stores.add(node.id)
    return loads, stores


def validate_run_signature(run_signature: str, available_globals: set[str]) -> tuple[str, ...]:
    normalized_signature = textwrap.dedent(run_signature).strip()
    compound_prefixes = ("async for ", "for ", "if ", "while ", "with ", "async with ")
    if normalized_signature.startswith(compound_prefixes):
        wrapper_code = f"async def __run_signature_wrapper():\n{textwrap.indent(normalized_signature, '    ')}\n"
    else:
        wrapper_code = (
            "async def __run_signature_wrapper():\n"
            "    return (\n"
            f"{textwrap.indent(normalized_signature, '        ')}\n"
            "    )\n"
        )

    try:
        wrapper_tree = ast.parse(wrapper_code)
        compile(wrapper_tree, "<run_signature>", "exec")
    except SyntaxError as exc:
        raise RunSignatureValidationError(str(exc)) from exc

    loads, stores = _name_loads_and_stores(wrapper_tree)
    allowed = available_globals | RUNTIME_GLOBALS | set(dir(builtins)) | stores | {"__run_signature_wrapper"}
    return tuple(sorted(name for name in loads if name not in allowed))


def extract_script_blocks(source: str, workflow_definition: dict[str, Any]) -> ScriptBlockExtractionResult:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptBlockExtractionError(f"script source is not valid Python: {exc}") from exc

    entry_fn = _find_entry_function(tree)
    if entry_fn is None:
        raise ScriptBlockExtractionError(
            "Could not find an @skyvern.workflow-decorated async function or top-level async def run(...)"
        )

    source_lines = source.splitlines(keepends=True)
    label_to_block_type = _label_to_block_type(workflow_definition)
    available_globals = _top_level_names(tree) | _entry_function_scope_names(entry_fn)
    seen_labels: set[str] = set()
    blocks: list[ExtractedScriptBlock] = []
    warnings: list[str] = []

    for kind, node in _walk_entry_statements(entry_fn.body):
        if kind == "await":
            await_node = node
            if not isinstance(await_node, ast.Await) or not isinstance(await_node.value, ast.Call):
                continue
            call = await_node.value
            if not _is_skyvern_call(call) or not isinstance(call.func, ast.Attribute):
                continue
            primitive = call.func.attr
            if primitive == "setup":
                continue
            label = _extract_label_from_call(call)
            if not label:
                continue
            run_signature = "await " + textwrap.dedent(_node_source(source_lines, call)).strip()
            is_compound = False
        elif kind == "async_for":
            async_for_node = node
            if not isinstance(async_for_node, ast.AsyncFor):
                continue
            iter_call = async_for_node.iter
            if not isinstance(iter_call, ast.Call) or not isinstance(iter_call.func, ast.Attribute):
                continue
            primitive = iter_call.func.attr
            label = _extract_label_from_call(iter_call)
            if not label:
                continue
            run_signature = textwrap.dedent(_node_source(source_lines, async_for_node)).strip()
            is_compound = True
        else:
            continue

        if label in seen_labels:
            warnings.append(f"Duplicate label {label!r}; only the first invocation is used")
            continue
        seen_labels.add(label)

        block_type = label_to_block_type.get(label)
        if (
            block_type is not None
            and block_type not in CACHEABLE_BLOCK_TYPES
            and block_type not in KNOWN_NON_CACHEABLE_BLOCK_TYPES
        ):
            warnings.append(
                f"Unknown workflow block type {block_type!r} for label {label!r}; treating as non-cacheable"
            )

        missing_globals = validate_run_signature(run_signature, available_globals)
        blocks.append(
            ExtractedScriptBlock(
                label=label,
                primitive=primitive,
                run_signature=run_signature,
                block_type=block_type,
                is_cacheable=block_type in CACHEABLE_BLOCK_TYPES,
                is_compound=is_compound,
                missing_globals=missing_globals,
            )
        )

    return ScriptBlockExtractionResult(blocks=tuple(blocks), warnings=tuple(warnings))
