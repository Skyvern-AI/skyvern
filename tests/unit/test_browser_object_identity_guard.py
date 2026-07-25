"""AST guard against package-native browser-object identity checks in production.

Skyvern selects among several browser engines whose ``Page`` / ``Frame`` /
``Browser`` / ... classes have distinct identities. A runtime ``isinstance`` /
``issubclass`` / ``type(x) is`` check against one driver's object class silently
misclassifies an equivalent object from a different engine. This guard walks
production ``skyvern/**`` and ``cloud/**`` and fails when a new such check
appears; the structural predicates in ``skyvern.webeye.browser_object_predicates``
are the sanctioned alternative.

Deliberately out of scope (so this does not pretend the later exception-catch
migration is finished, and does not raise false positives):
  * annotations, ``TYPE_CHECKING`` imports, and ``cast(...)`` -- type-only usage,
    never a runtime branch;
  * ``except <native error>`` catches and identity checks against driver *error*
    classes (Error/TimeoutError/TargetClosedError) -- that is a separate tranche;
  * classes named Page/Frame/Response/... imported from non-driver packages
    (fastapi, starlette, httpx, ...), resolved by import origin.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

# Import roots of the engines whose object classes this guard polices. A new
# engine adapter that ships its own Page/Frame-alike classes under a distinct
# import root must be registered here, or identity checks against it slip past.
_BROWSER_DRIVER_ROOTS = ("playwright", "patchright", "rustwright")

# Public API submodules of a browser driver whose ``Page``/``Frame``/... attributes
# are engine-native object classes. Kept as an explicit allowlist so that a value
# imported from a driver package (``from playwright import expect`` / ``Error``) is
# never mistaken for a module in ``from <driver> import <name> as alias`` form.
# ``async_api`` is the only such submodule imported in this repo today; ``sync_api``
# is the driver's other public API module. Add a name here only with import evidence.
_BROWSER_DRIVER_API_SUBMODULES = frozenset({"async_api", "sync_api"})

# Browser *object* classes whose identity is engine-specific. Driver *error*
# classes (Error, TimeoutError, TargetClosedError, ...) are intentionally absent:
# migrating those catches is a separate tranche, not this guard's concern.
_BROWSER_OBJECT_CLASSES = frozenset(
    {
        "Page",
        "Frame",
        "FrameLocator",
        "Locator",
        "ElementHandle",
        "JSHandle",
        "Browser",
        "BrowserContext",
        "BrowserType",
        "CDPSession",
        "Download",
        "Request",
        "Response",
        "Route",
        "WebSocket",
        "WebSocketRoute",
        "Worker",
        "Dialog",
        "ConsoleMessage",
        "FileChooser",
        "Video",
        "Keyboard",
        "Mouse",
        "Touchscreen",
        "Accessibility",
        "Clock",
        "Selectors",
        "Tracing",
        "Playwright",
    }
)


@dataclass(frozen=True)
class Violation:
    filename: str
    lineno: int
    class_name: str
    check: str


def _module_root_is_driver(module: str | None) -> bool:
    if not module:
        return False
    return module.split(".", 1)[0] in _BROWSER_DRIVER_ROOTS


def _dotted_path(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return ".".join(parts)


class _IdentityCheckVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[Violation] = []
        # local name -> originating driver object-class name (e.g. "P" -> "Frame")
        self._driver_object_names: dict[str, str] = {}
        # local module alias -> driver module (e.g. "pw" -> "playwright.async_api")
        self._driver_module_aliases: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if _module_root_is_driver(node.module):
            for alias in node.names:
                if alias.name in _BROWSER_OBJECT_CLASSES:
                    self._driver_object_names[alias.asname or alias.name] = alias.name
                elif alias.name in _BROWSER_DRIVER_API_SUBMODULES:
                    # ``from playwright import async_api as pw``: bind the local name to
                    # the full driver module path so ``pw.Page`` resolves by origin.
                    self._driver_module_aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _module_root_is_driver(alias.name):
                self._driver_module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def _module_expr_is_driver(self, node: ast.expr) -> bool:
        # ``module_alias.Page`` where the module part may be a single alias
        # (``pw``) or a dotted package path (``playwright.async_api``). Match the
        # rendered path against a registered driver import, resolving by origin
        # (never by the ``Page`` text): an exact hit, or a submodule of an
        # imported driver root (``import playwright`` + ``playwright.async_api.Page``).
        path = _dotted_path(node)
        if path is None:
            return False
        for alias in self._driver_module_aliases:
            if path == alias or path.startswith(alias + "."):
                return True
        return False

    def _resolve(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self._driver_object_names.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr in _BROWSER_OBJECT_CLASSES:
            if self._module_expr_is_driver(node.value):
                return node.attr
        return None

    def _record(self, node: ast.expr, check: str) -> None:
        targets = node.elts if isinstance(node, ast.Tuple) else [node]
        for target in targets:
            class_name = self._resolve(target)
            if class_name is not None:
                self.violations.append(Violation(self.filename, target.lineno, class_name, check))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in ("isinstance", "issubclass") and len(node.args) >= 2:
            self._record(node.args[1], node.func.id)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        left = node.left
        is_type_call = isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and left.func.id == "type"
        if is_type_call:
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                    self._record(comparator, "type-is")
        self.generic_visit(node)


def find_browser_object_identity_checks(source: str, filename: str = "<test>") -> list[Violation]:
    visitor = _IdentityCheckVisitor(filename)
    visitor.visit(ast.parse(source))
    return visitor.violations


# --- detector unit tests (synthetic sources) --------------------------------


def test_flags_isinstance_against_driver_page() -> None:
    src = "from playwright.async_api import Page\nx = isinstance(obj, Page)\n"
    v = find_browser_object_identity_checks(src)
    assert [(x.class_name, x.check) for x in v] == [("Page", "isinstance")]


def test_flags_aliased_import() -> None:
    src = "from patchright.async_api import Frame as F\nx = isinstance(obj, F)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Frame"]


def test_flags_each_member_of_isinstance_tuple() -> None:
    src = "from playwright.async_api import Frame, Page\nx = isinstance(obj, (Page, Frame))\n"
    assert sorted(x.class_name for x in find_browser_object_identity_checks(src)) == ["Frame", "Page"]


def test_flags_issubclass_and_type_is() -> None:
    src = "from rustwright import Browser\na = issubclass(cls, Browser)\nb = type(obj) is Browser\n"
    checks = sorted(x.check for x in find_browser_object_identity_checks(src))
    assert checks == ["issubclass", "type-is"]


def test_flags_module_attribute_access() -> None:
    src = "import playwright.async_api as pw\nx = isinstance(obj, pw.Page)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Page"]


def test_flags_unaliased_dotted_import_attribute_access() -> None:
    src = "import playwright.async_api\nx = isinstance(obj, playwright.async_api.Page)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Page"]


def test_flags_dotted_attribute_access_under_bare_driver_root_import() -> None:
    src = "import playwright\nx = isinstance(obj, playwright.async_api.Frame)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Frame"]


def test_flags_unaliased_dotted_import_for_other_driver_root() -> None:
    src = "import patchright.async_api\nx = issubclass(cls, patchright.async_api.Browser)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Browser"]


def test_flags_aliased_importfrom_submodule_attribute_access() -> None:
    src = "from playwright import async_api as pw\nx = isinstance(obj, pw.Page)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Page"]


def test_flags_unaliased_importfrom_submodule_attribute_access() -> None:
    src = "from playwright import async_api\nx = isinstance(obj, async_api.Page)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Page"]


def test_flags_importfrom_submodule_for_other_driver_root() -> None:
    src = "from patchright import async_api as pr\nx = issubclass(cls, pr.Frame)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Frame"]


def test_flags_aliased_importfrom_sync_api_submodule_attribute_access() -> None:
    src = "from playwright import sync_api as pw\nx = isinstance(obj, pw.Page)\n"
    assert [x.class_name for x in find_browser_object_identity_checks(src)] == ["Page"]


def test_ignores_importfrom_submodule_from_non_driver_package() -> None:
    src = "from starlette import responses as r\nx = isinstance(summary, r.Response)\n"
    assert find_browser_object_identity_checks(src) == []


def test_ignores_non_module_value_imported_from_driver_root() -> None:
    src = "from playwright import expect\nx = isinstance(obj, expect.Page)\n"
    assert find_browser_object_identity_checks(src) == []


def test_ignores_dotted_attribute_access_from_non_driver_package() -> None:
    src = "import fastapi.responses\nx = isinstance(summary, fastapi.responses.Response)\n"
    assert find_browser_object_identity_checks(src) == []


def test_ignores_same_named_class_from_non_driver_package() -> None:
    src = "from fastapi import Response\nx = isinstance(summary, Response)\n"
    assert find_browser_object_identity_checks(src) == []


def test_ignores_driver_error_classes() -> None:
    src = "from playwright.async_api import TimeoutError as PWTimeout\nx = isinstance(exc, PWTimeout)\n"
    assert find_browser_object_identity_checks(src) == []


def test_ignores_annotations_and_cast() -> None:
    src = (
        "from typing import cast\n"
        "from playwright.async_api import Page\n"
        "def f(x: Page) -> Page:\n"
        "    y: Page = x\n"
        "    return cast(Page, y)\n"
    )
    assert find_browser_object_identity_checks(src) == []


def test_ignores_type_checking_import_used_only_in_annotation() -> None:
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from playwright.async_api import Page\n"
        "def f(x: 'Page') -> None:\n"
        "    return None\n"
    )
    assert find_browser_object_identity_checks(src) == []


# --- repo enforcement -------------------------------------------------------


def _production_python_files() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    files: list[Path] = []
    for package in ("skyvern", "cloud"):
        root = repo_root / package
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "tests" in parts or path.name.startswith("test_") or "__pycache__" in parts:
                continue
            files.append(path)
    return files


def test_no_browser_object_identity_checks_in_production() -> None:
    offenders: list[Violation] = []
    for path in _production_python_files():
        try:
            tree_src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            offenders.extend(find_browser_object_identity_checks(tree_src, str(path)))
        except SyntaxError:
            continue
    assert not offenders, (
        "Package-native browser-object identity checks found. Use the structural "
        "predicates in skyvern.webeye.browser_object_predicates (e.g. is_page_like) "
        "instead of isinstance/issubclass/type() against a driver's object class:\n"
        + "\n".join(f"  {o.filename}:{o.lineno} {o.check}({o.class_name})" for o in offenders)
    )


def test_repo_scan_finds_at_least_the_production_tree() -> None:
    # Guard against the enforcement test silently passing because the scan walked
    # nothing (wrong root, glob typo). skyvern/** must always yield files.
    assert len(_production_python_files()) > 100


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
