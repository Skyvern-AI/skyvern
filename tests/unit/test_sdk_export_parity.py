"""Mechanical parity guard for the vendored Python SDK's public surface.

The SDK's generated type modules and their lazy exports are maintained separately.
This test derives the source-defined types with AST instead of a hand-maintained
allowlist, then verifies that both public namespaces expose the same objects.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

import skyvern.client as client
import skyvern.client.types as client_types

_TYPES_DIRECTORY = Path(client_types.__file__).parent
_CLIENT_DIRECTORY = Path(client.__file__).parent
_CLIENT_MODULE_FILENAMES = frozenset({"client.py", "raw_client.py"})


def _module_path(path: Path, package_directory: Path) -> str:
    """Return the relative import path used by a package's lazy-export table."""
    return "." + ".".join(path.relative_to(package_directory).with_suffix("").parts)


def _module_scope_statements(statements: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements at module scope, including module-level control-flow bodies."""
    for statement in statements:
        yield statement
        if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            yield from _module_scope_statements(statement.body)
            yield from _module_scope_statements(statement.orelse)
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            yield from _module_scope_statements(statement.body)
            yield from _module_scope_statements(statement.orelse)
            yield from _module_scope_statements(statement.finalbody)
            for handler in statement.handlers:
                yield from _module_scope_statements(handler.body)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            yield from _module_scope_statements(statement.body)
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                yield from _module_scope_statements(case.body)


def _top_level_type_names(path: Path) -> set[str]:
    """Collect public classes and aliases defined at module scope in one generated type module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()

    for statement in _module_scope_statements(tree.body):
        if isinstance(statement, ast.ClassDef):
            names.add(statement.name)
        elif isinstance(statement, ast.Assign):
            names.update(target.id for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names.add(statement.target.id)

    return {name for name in names if not name.startswith("_")}


def _derived_type_imports() -> dict[str, str]:
    """Map every source-defined public type to its generated lazy-import path."""
    definitions: dict[str, list[str]] = defaultdict(list)
    for path in sorted(_TYPES_DIRECTORY.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        module_path = _module_path(path, _TYPES_DIRECTORY)
        for name in _top_level_type_names(path):
            definitions[name].append(module_path)

    duplicated = {name: modules for name, modules in definitions.items() if len(modules) > 1}
    assert not duplicated, f"A public SDK type is defined by more than one module: {duplicated}"
    return {name: modules[0] for name, modules in definitions.items()}


def _public_methods(class_node: ast.ClassDef) -> set[str]:
    """Return a generated client class's directly defined public method names."""
    return {
        method.name
        for method in class_node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and not method.name.startswith("_")
    }


def _client_classes(path: Path) -> dict[str, ast.ClassDef]:
    """Collect direct client classes from a generated client source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return {statement.name: statement for statement in tree.body if isinstance(statement, ast.ClassDef)}


def test_top_level_type_names_include_module_scope_control_flow(tmp_path: Path) -> None:
    """Generated types inside a module-level conditional must remain part of the export surface."""
    module = tmp_path / "conditional_type.py"
    module.write_text(
        """
if enabled:
    class ConditionalType:
        pass

try:
    TryType = str
except ImportError:
    FallbackType = str
"""
    )

    assert _top_level_type_names(module) == {"ConditionalType", "FallbackType", "TryType"}


def test_type_exports_match_generated_type_modules() -> None:
    """Every generated type is registered once in both type-package export declarations."""
    derived_imports = _derived_type_imports()
    dynamic_imports = client_types._dynamic_imports
    dynamic_drift = {
        "missing_from_dynamic_imports": sorted(set(derived_imports) - set(dynamic_imports)),
        "unexpected_dynamic_imports": sorted(set(dynamic_imports) - set(derived_imports)),
        "wrong_dynamic_import_modules": {
            name: {"expected": derived_imports[name], "actual": dynamic_imports[name]}
            for name in sorted(set(derived_imports) & set(dynamic_imports))
            if derived_imports[name] != dynamic_imports[name]
        },
    }
    all_counts = Counter(client_types.__all__)
    all_drift = {
        "missing_from___all__": sorted(set(derived_imports) - set(all_counts)),
        "unexpected_in___all__": sorted(set(all_counts) - set(derived_imports)),
        "duplicated_in___all__": sorted(name for name, count in all_counts.items() if count > 1),
    }
    drift = {name: values for name, values in {**dynamic_drift, **all_drift}.items() if values}

    assert not drift, (
        f"SDK type export drift detected: {drift}. "
        "Register the listed types in the generated lazy-export tables; for a full regeneration, "
        "run `fern generate --group python-sdk` and sync the vendored SDK."
    )


def test_types_resolve_to_the_same_object_from_both_public_sdk_namespaces() -> None:
    """The top-level SDK must re-export each type-package public type unchanged."""
    for name in client_types.__all__:
        assert getattr(client_types, name) is getattr(client, name), name


def test_top_level_sdk_dynamic_imports_and_all_match() -> None:
    """Star imports expose every lazy top-level SDK export, and no other names."""
    dynamic_import_names = set(client._dynamic_imports)
    all_names = set(client.__all__)
    drift = {
        "missing_from___all__": sorted(dynamic_import_names - all_names),
        "unexpected_in___all__": sorted(all_names - dynamic_import_names),
    }

    assert not any(drift.values()), f"Top-level SDK export declarations differ: {drift}"


def test_all_top_level_sdk_public_exports_resolve() -> None:
    """Every top-level SDK export must resolve for star-import users."""
    unresolved: dict[str, str] = {}
    for name in client.__all__:
        try:
            getattr(client, name)
        except (AttributeError, ImportError) as error:
            unresolved[name] = f"{type(error).__name__}: {error}"

    assert not unresolved, f"Unresolvable skyvern.client public exports: {unresolved}"


def test_sync_and_async_client_public_methods_are_paired() -> None:
    """Generated sync and async client classes expose identical public method names."""
    unpaired_classes: list[str] = []
    method_mismatches: dict[str, dict[str, list[str]]] = {}
    paired_class_count = 0

    for path in sorted(_CLIENT_DIRECTORY.rglob("*.py")):
        if path.name not in _CLIENT_MODULE_FILENAMES:
            continue
        classes = _client_classes(path)
        relative_path = path.relative_to(_CLIENT_DIRECTORY)
        sync_class_names = {name for name in classes if not name.startswith("Async")}
        async_class_names = {name.removeprefix("Async") for name in classes if name.startswith("Async")}

        for class_name in sorted(sync_class_names ^ async_class_names):
            unpaired_classes.append(f"{relative_path}: {class_name}")

        for class_name in sorted(sync_class_names & async_class_names):
            paired_class_count += 1
            sync_methods = _public_methods(classes[class_name])
            async_methods = _public_methods(classes[f"Async{class_name}"])
            if sync_methods != async_methods:
                method_mismatches[f"{relative_path}: {class_name}"] = {
                    "sync_only": sorted(sync_methods - async_methods),
                    "async_only": sorted(async_methods - sync_methods),
                }

    assert paired_class_count, "No generated sync/async client class pairs were inspected."
    assert not unpaired_classes, f"Generated sync/async client class pairs are missing: {unpaired_classes}"
    assert not method_mismatches, f"Generated sync/async public method pairs differ: {method_mismatches}"
