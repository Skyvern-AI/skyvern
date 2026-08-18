"""Mechanical guard for generated SDK sub-client property imports."""

from __future__ import annotations

import ast
import importlib
import importlib.util
from collections.abc import Iterator
from pathlib import Path

import skyvern.client.client as client_module

_CLIENT_SOURCE = Path(client_module.__file__)
_CLIENT_PACKAGE = client_module.__package__
_CLIENT_CLASS_NAMES = frozenset({"Skyvern", "AsyncSkyvern"})


def _is_public_property(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether a method is a public property."""
    return not function.name.startswith("_") and any(
        isinstance(decorator, ast.Name) and decorator.id == "property" for decorator in function.decorator_list
    )


def _relative_client_imports(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[str]:
    """Yield direct relative imports to modules inside the SDK client package."""
    for statement in ast.walk(function):
        if not isinstance(statement, ast.ImportFrom) or not statement.level:
            continue

        if statement.module:
            names = ("." * statement.level + statement.module,)
        else:
            names = tuple("." * statement.level + imported.name for imported in statement.names if imported.name != "*")

        for name in names:
            target = importlib.util.resolve_name(name, _CLIENT_PACKAGE)
            if target.startswith(f"{_CLIENT_PACKAGE}."):
                yield target


def _subclient_property_imports() -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Return public property names and their relative client-module import targets."""
    tree = ast.parse(_CLIENT_SOURCE.read_text(), filename=str(_CLIENT_SOURCE))
    properties: dict[str, set[str]] = {class_name: set() for class_name in _CLIENT_CLASS_NAMES}
    imports: set[tuple[str, str]] = set()

    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef) or class_node.name not in _CLIENT_CLASS_NAMES:
            continue
        for function in class_node.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_public_property(function):
                continue
            properties[class_node.name].add(function.name)
            for target in _relative_client_imports(function):
                imports.add((f"{class_node.name}.{function.name}", target))

    return properties, imports


def test_subclient_property_imports_resolve() -> None:
    """Every public sub-client property must import an existing client module."""
    properties, imports = _subclient_property_imports()
    assert set(properties) == _CLIENT_CLASS_NAMES
    assert all(properties.values()), "No public properties were inspected on a generated SDK client class."
    assert imports, "No relative client-module imports were discovered in public SDK properties."

    unresolved: dict[str, str] = {}
    for property_name, target in sorted(imports):
        try:
            importlib.import_module(target)
        except ImportError as error:
            unresolved[property_name] = f"{target}: {type(error).__name__}: {error}"

    assert not unresolved, f"Unresolvable SDK sub-client property imports: {unresolved}"
