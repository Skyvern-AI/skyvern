from __future__ import annotations

import ast
from collections.abc import Collection, Iterable
from dataclasses import dataclass

COPILOT_CODE_SECURITY_FAILURE_CATEGORY = "COPILOT_CODE_SECURITY"

_AUTHOR_ATTR_REASONS = {
    "request": "AUTHOR_PAGE_REQUEST",
    "context": "AUTHOR_PAGE_CONTEXT",
    "evaluate": "AUTHOR_PAGE_EVALUATE",
    "evaluate_handle": "AUTHOR_PAGE_EVALUATE",
}
_RUNTIME_ATTR_REASONS = {
    "request": "RUNTIME_PAGE_REQUEST",
    "context": "RUNTIME_PAGE_CONTEXT",
    "evaluate": "RUNTIME_PAGE_EVALUATE",
    "evaluate_handle": "RUNTIME_PAGE_EVALUATE",
}


@dataclass(frozen=True)
class CodeBlockSecurityInput:
    label: str
    code: str


class CodeBlockSecurityError(str):
    block_label: str
    reason_code: str
    surface: str

    def __new__(cls, message: str, *, block_label: str, reason_code: str, surface: str) -> CodeBlockSecurityError:
        item = str.__new__(cls, message)
        item.block_label = block_label
        item.reason_code = reason_code
        item.surface = surface
        return item

    def to_failure_category(self) -> dict[str, str | float]:
        return {
            "category": COPILOT_CODE_SECURITY_FAILURE_CATEGORY,
            "reason_code": self.reason_code,
            "confidence_float": 0.99,
            "reasoning": f"{self.reason_code}: blocked {self.surface} before browser dispatch",
        }


def author_time_code_security_errors(*, label: str, code: str) -> list[CodeBlockSecurityError]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    return _security_errors_for_tree(label, tree, _AUTHOR_ATTR_REASONS)


def runtime_code_security_errors(
    blocks: Iterable[CodeBlockSecurityInput],
    *,
    selected_labels: Collection[str] | None = None,
) -> list[CodeBlockSecurityError]:
    selected = set(selected_labels) if selected_labels is not None else None
    errors: list[CodeBlockSecurityError] = []
    for block in blocks:
        if selected is not None and block.label not in selected:
            continue
        try:
            tree = ast.parse(block.code)
        except SyntaxError:
            errors.append(_error(block.label, "RUNTIME_SYNTAX_ERROR"))
            continue
        errors.extend(_security_errors_for_tree(block.label, tree, _RUNTIME_ATTR_REASONS))
    return errors


def _security_errors_for_tree(label: str, tree: ast.AST, attr_reasons: dict[str, str]) -> list[CodeBlockSecurityError]:
    errors: list[CodeBlockSecurityError] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        # This AST layer catches direct attribute access. The sandbox's unresolved-name,
        # private-attribute, and builtins restrictions remain the backstop for dynamic forms.
        if isinstance(node, ast.Attribute) and node.attr in attr_reasons:
            reason = attr_reasons[node.attr]
            if reason not in seen:
                errors.append(_error(label, reason))
                seen.add(reason)
    return errors


def _error(label: str, reason_code: str) -> CodeBlockSecurityError:
    surface = _surface_for_reason(reason_code)
    return CodeBlockSecurityError(
        _message_for_reason(label=label, reason_code=reason_code, surface=surface),
        block_label=label,
        reason_code=reason_code,
        surface=surface,
    )


def _surface_for_reason(reason_code: str) -> str:
    if reason_code.endswith("PAGE_REQUEST"):
        return "page.request"
    if reason_code.endswith("PAGE_CONTEXT"):
        return "page.context"
    if reason_code.endswith("PAGE_EVALUATE"):
        return "page.evaluate"
    return "python_ast"


def _message_for_reason(*, label: str, reason_code: str, surface: str) -> str:
    if reason_code.startswith("AUTHOR_"):
        return f"Code block `{label}` failed the Copilot code security check: {surface} is not allowed."
    return f"Code block `{label}` was blocked before browser dispatch: {surface} is not allowed at Copilot runtime."
