from __future__ import annotations

import ast
from pathlib import Path

EXPECTED_FIELDS = {
    "CREDENTIAL_FILL_FIELDS": frozenset({"username", "password", "totp"}),
    "LIVE_SCOUT_CREDENTIAL_FIELDS": frozenset({"username", "password"}),
}


def _is_expected_field_set(node: ast.AST, expected_fields: frozenset[str]) -> bool:
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return False
    if len(node.elts) != len(expected_fields):
        return False
    values = {
        element.s for element in node.elts if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }
    return len(values) == len(expected_fields) and values == expected_fields


def _collect_expected_set_hits(node: ast.AST, path: Path, line: int) -> list[str]:
    matches: list[str] = []
    for expected_name, expected_fields in EXPECTED_FIELDS.items():
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)) and _is_expected_field_set(node, expected_fields):
            matches.append(f"{path}:{line}:{expected_name}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "frozenset"}:
            if len(node.args) == 1 and _is_expected_field_set(node.args[0], expected_fields):
                matches.append(f"{path}:{line}:{expected_name}")
    return matches


def _iter_literal_set_locations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches: list[str] = []
    for node in ast.walk(tree):
        if not hasattr(node, "lineno"):
            continue
        matches.extend(_collect_expected_set_hits(node, path, node.lineno))
    return matches


def test_no_credential_fill_field_set_literals_reintroduced() -> None:
    copilot_dir = Path(__file__).resolve().parents[5] / "skyvern" / "forge" / "sdk" / "copilot"
    violations: list[str] = []
    for file in sorted(copilot_dir.glob("**/*.py")):
        if file.name == "credential_fill_fields.py":
            continue
        violations.extend(_iter_literal_set_locations(file))

    assert not violations, (
        "Detected literal credential field set duplication that should be centralized in "
        f"credential_fill_fields.py: {violations}"
    )


def test_no_reintroduced_literal_set_shapes() -> None:
    snippets = {
        "three_field_set": "x = {'username', 'password', 'totp'}\n",
        "three_field_frozenset": "x = frozenset({'username', 'password', 'totp'})\n",
        "two_field_set": "x = {'username', 'password'}\n",
    }

    for label, text in snippets.items():
        tree = ast.parse(text)
        matches: list[str] = []
        for node in ast.walk(tree):
            if not hasattr(node, "lineno"):
                continue
            matches.extend(_collect_expected_set_hits(node, Path("snippet.py"), node.lineno))
        assert matches, f"Expected mutation coverage to fail for {label}"


def test_credential_fill_field_set_is_derived_from_the_literal_type() -> None:
    source_path = (
        Path(__file__).resolve().parents[5] / "skyvern" / "forge" / "sdk" / "copilot" / "credential_fill_fields.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "CREDENTIAL_FILL_FIELDS"
    )

    assert ast.unparse(assignment.value) == "frozenset(get_args(CredentialFillField))"
