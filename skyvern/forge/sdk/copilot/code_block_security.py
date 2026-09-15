"""Deterministic AST denylist over Copilot-synthesized code blocks, enforced at authoring and at runtime pre-dispatch.
Denies `page.request`, `page.context` and the dynamic attribute/namespace builtins that reach the live session's
credentials; a guardrail on one code path, not a sandbox, and it does not cover exfiltration through `page.evaluate`."""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterable
from dataclasses import dataclass

COPILOT_CODE_SECURITY_FAILURE_CATEGORY = "COPILOT_CODE_SECURITY"

_AUTHOR_ATTR_REASONS = {
    "request": "AUTHOR_PAGE_REQUEST",
    "context": "AUTHOR_PAGE_CONTEXT",
}
_RUNTIME_ATTR_REASONS = {
    "request": "RUNTIME_PAGE_REQUEST",
    "context": "RUNTIME_PAGE_CONTEXT",
}
_DYNAMIC_ATTRIBUTE_BUILTINS = frozenset({"getattr", "setattr", "delattr", "vars", "globals", "locals"})
_AUTHOR_DYNAMIC_ATTRIBUTE_REASON = "AUTHOR_DYNAMIC_ATTRIBUTE"
_RUNTIME_DYNAMIC_ATTRIBUTE_REASON = "RUNTIME_DYNAMIC_ATTRIBUTE"


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
        return [_error(label, "AUTHOR_SYNTAX_ERROR")]
    return _security_errors_for_tree(label, tree, _AUTHOR_ATTR_REASONS, _AUTHOR_DYNAMIC_ATTRIBUTE_REASON)


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
        errors.extend(
            _security_errors_for_tree(block.label, tree, _RUNTIME_ATTR_REASONS, _RUNTIME_DYNAMIC_ATTRIBUTE_REASON)
        )
    return errors


INERT_SLOT_NAME = "__skyvern_slot__"
_PARAMETER_CODE_REASON = "RUNTIME_PARAMETER_CODE"


def rendering_introduced_security_errors(
    *, label: str, authored_code: str | None, rendered_code: str
) -> list[CodeBlockSecurityError]:
    """Refuse a render whose parameter values contributed anything but literals. `authored_code` is the same template
    rendered under the same control flow with every value slot replaced by `INERT_SLOT_NAME`; None fails closed.
    Byte equality of the two renders is never a pass: a value equal to the marker produces identical text."""
    try:
        rendered_tree = ast.parse(rendered_code)
    except SyntaxError:
        return []
    if authored_code is None:
        return [_error(label, _PARAMETER_CODE_REASON)]
    try:
        authored_tree = ast.parse(authored_code)
    except SyntaxError:
        return [_error(label, _PARAMETER_CODE_REASON)]
    if _slots_hold_only_literals(authored_tree, rendered_tree):
        return []
    return [_error(label, _PARAMETER_CODE_REASON)]


def _slots_hold_only_literals(authored: ast.AST, rendered: ast.AST) -> bool:
    if isinstance(authored, ast.Name) and authored.id == INERT_SLOT_NAME:
        return _is_literal(rendered)
    if isinstance(authored, ast.Constant) and isinstance(authored.value, str) and INERT_SLOT_NAME in authored.value:
        return isinstance(rendered, ast.Constant) and isinstance(rendered.value, str)
    if type(authored) is not type(rendered):
        return False
    for field, authored_value in ast.iter_fields(authored):
        rendered_value = getattr(rendered, field, None)
        if isinstance(authored_value, ast.AST):
            if not isinstance(rendered_value, ast.AST) or not _slots_hold_only_literals(authored_value, rendered_value):
                return False
        elif isinstance(authored_value, list):
            if not isinstance(rendered_value, list) or len(authored_value) != len(rendered_value):
                return False
            for authored_item, rendered_item in zip(authored_value, rendered_value, strict=True):
                if isinstance(authored_item, ast.AST):
                    if not isinstance(rendered_item, ast.AST) or not _slots_hold_only_literals(
                        authored_item, rendered_item
                    ):
                        return False
                elif authored_item != rendered_item:
                    return False
        elif authored_value != rendered_value:
            return False
    return True


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _is_literal(key) for key in node.keys) and all(
            _is_literal(value) for value in node.values
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand)
    return False


def _security_errors_for_tree(
    label: str, tree: ast.AST, attr_reasons: dict[str, str], dynamic_attribute_reason: str
) -> list[CodeBlockSecurityError]:
    errors: list[CodeBlockSecurityError] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        reasons: list[str] = []
        if isinstance(node, ast.Attribute) and node.attr in attr_reasons:
            reasons.append(attr_reasons[node.attr])
        # A class pattern reads `page.context` through getattr with no ast.Attribute node.
        elif isinstance(node, ast.MatchClass):
            reasons.extend(attr_reasons[attr] for attr in node.kwd_attrs if attr in attr_reasons)
        # The sandbox's getattr/vars wrappers strip only private names and globals() exposes the
        # builtins dict, so a string-built public name would reach the denied members.
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _DYNAMIC_ATTRIBUTE_BUILTINS:
            reasons.append(dynamic_attribute_reason)
        for reason in reasons:
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
    if reason_code.endswith("DYNAMIC_ATTRIBUTE"):
        return f"dynamic attribute access ({'/'.join(sorted(_DYNAMIC_ATTRIBUTE_BUILTINS))})"
    if reason_code == _PARAMETER_CODE_REASON:
        return "code from a parameter value"
    return "python_ast"


def _message_for_reason(*, label: str, reason_code: str, surface: str) -> str:
    if reason_code == _PARAMETER_CODE_REASON:
        return (
            f"Code block `{label}` was blocked before browser dispatch: a parameter value contributes code; "
            "only a literal value (string, number, boolean, null, or JSON list/object) may be rendered into code."
        )
    if reason_code == "AUTHOR_SYNTAX_ERROR":
        return f"Code block `{label}` failed the Copilot code security check: the code does not parse as Python."
    if reason_code.startswith("AUTHOR_"):
        return f"Code block `{label}` failed the Copilot code security check: {surface} is not allowed."
    return f"Code block `{label}` was blocked before browser dispatch: {surface} is not allowed at Copilot runtime."
