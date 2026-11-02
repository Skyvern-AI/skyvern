import ast
import re
from pathlib import Path

# Task V3 sends input only through input_dispatch.
_INPUT_DISPATCH = Path("skyvern/forge/taskv3/input_dispatch.py")
_TASKV3_BANNED_INPUT_METHODS = frozenset(
    """check click dblclick dispatch_event drag_and_drop drag_to fill focus hover insert_text press press_sequentially select_option
    select_text set_checked set_files set_input_files tap type uncheck""".split()
)
_TASKV3_BANNED_DEVICES = frozenset({"mouse", "keyboard"})
_IN_PAGE_INPUT_JS = re.compile(r"\.click\(\)|dispatchEvent\(|new (Mouse|Keyboard|Pointer)Event|Input\.dispatch")
# `clear` is also a list and dict method, so it is banned only on a receiver shaped like an element.
_ELEMENT_SOURCES = frozenset({"locator", "query_selector", "element_handle", "nth", "first", "last"})


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return current.name
    return "<module>"


def _is_element(node: ast.expr, element_names: set[str]) -> bool:
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id in element_names
    return isinstance(node, ast.Attribute) and (node.attr in _ELEMENT_SOURCES or node.attr.startswith("get_by_"))


def _direct_input_sites(path: Path, only_function: str | None = None) -> list[str]:
    tree = ast.parse(path.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    element_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_element(node.value, set())
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    sites: list[str] = []
    for node in ast.walk(tree):
        found: str | None = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            dispatched = isinstance(receiver, ast.Name) and receiver.id == "input_dispatch"
            if node.func.attr in _TASKV3_BANNED_INPUT_METHODS and not dispatched:
                found = f"{node.func.attr} on {ast.unparse(node.func.value)}"
            elif node.func.attr == "clear" and _is_element(receiver, element_names):
                found = f"clear on {ast.unparse(receiver)}"
        elif isinstance(node, ast.Attribute) and node.attr in _TASKV3_BANNED_DEVICES:
            found = f".{node.attr} on {ast.unparse(node.value)}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _TASKV3_BANNED_INPUT_METHODS | _TASKV3_BANNED_DEVICES
        ):
            found = f"getattr {node.args[1].value!r}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _IN_PAGE_INPUT_JS.search(node.value):
            found = "in-page input script"
        if found is None:
            continue
        function = _enclosing_function(parents, node)
        if only_function is not None and function != only_function:
            continue
        sites.append(f"{path}:{getattr(node, 'lineno', '?')} {function} {found}")
    return sites


def test_taskv3_input_flows_only_through_input_dispatch() -> None:
    sites = [
        site
        for path in sorted(Path("skyvern/forge/taskv3").rglob("*.py"))
        if path != _INPUT_DISPATCH
        for site in _direct_input_sites(path)
    ]
    # The captcha ladder lives outside taskv3 and V3 hands it the click to use.
    sites += _direct_input_sites(Path("skyvern/webeye/utils/captcha_solver.py"), "_solve_challenge_ladder_impl")

    assert sites == []
