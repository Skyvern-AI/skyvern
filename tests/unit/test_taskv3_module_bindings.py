import ast
import pathlib
from collections import Counter

from skyvern.forge import taskv3

_MODULES = sorted(pathlib.Path(taskv3.__file__).parent.glob("*.py"))


def _target_names(target: ast.expr) -> list[str]:
    # `d[KEY] = ...` and `obj.attr = ...` bind no module-level name.
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(ast.unparse(decorator) in ("overload", "typing.overload") for decorator in node.decorator_list)


def _top_level_bindings(source: str) -> list[str]:
    # Top-level statements only: a name bound in both arms of a `try:` or an `if TYPE_CHECKING:` block is one binding.
    names: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_overload(node)
        ):
            names.append(node.name)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names.extend(_target_names(node.target))
        elif isinstance(node, ast.Assign):
            names.extend(name for target in node.targets for name in _target_names(target))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # `import a.b` binds `a` to the same module `import a` does.
            names.extend(alias.asname or alias.name for alias in node.names if alias.asname or "." not in alias.name)
    return names


def _rebound(source: str) -> list[str]:
    return sorted(name for name, count in Counter(_top_level_bindings(source)).items() if count > 1)


def test_no_taskv3_module_binds_a_module_level_name_twice() -> None:
    """A second binding silently replaces the first: every caller of the first constant runs the second one's code."""
    assert len(_MODULES) >= 15, "the glob found too few taskv3 modules; it is not looking at the package"
    tools_names = _top_level_bindings((pathlib.Path(taskv3.__file__).parent / "tools.py").read_text())
    assert sum(1 for n in tools_names if n.startswith("_") and n.endswith("_JS")) > 100, "the scan misses tools.py"

    rebound = {path.name: names for path in _MODULES if (names := _rebound(path.read_text()))}
    assert not rebound, f"module-level names bound more than once (give the second one its own name): {rebound}"


def test_the_binding_scan_sees_a_rebound_constant() -> None:
    source = '_CARET_JS = "a"\n\n\ndef f() -> None:\n    _CARET_JS = "local"\n\n\n_CARET_JS = "b"\n'
    assert _rebound(source) == ["_CARET_JS"]
    assert _rebound("def g() -> None: ...\n\n\nclass g: ...\n") == ["g"]
    assert _rebound("A, B = 1, 2\nB: int = 3\n") == ["B"]
    assert _rebound("try:\n    X = 1\nexcept ImportError:\n    X = 2\nX: int\n") == []
    assert _rebound("from m import X\nX = 1\n") == ["X"]
    assert _rebound("import os\nimport os.path\nimport a.b as os\n") == ["os"]
    assert _rebound("C = {}\nKEY = 1\nC[KEY] = 2\nC.attr = 3\n") == []
    assert (
        _rebound("@overload\ndef h(x: int) -> int: ...\n\n\n@overload\ndef h(x: str) -> str: ...\n\n\ndef h(x): ...\n")
        == []
    )
