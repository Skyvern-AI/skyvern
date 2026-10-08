"""Deterministic AST denylist over Copilot-synthesized code blocks, enforced at authoring and at runtime pre-dispatch.
Denies `page.request`, `page.context` and the dynamic attribute/namespace builtins that reach the live session's
credentials; a guardrail on one code path, not a sandbox, and it does not cover exfiltration through `page.evaluate`."""

from __future__ import annotations

import ast
import re
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
DENIED_PAGE_MEMBERS = tuple(_AUTHOR_ATTR_REASONS)
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
_VALUE_SLOT_RE = re.compile(r"\{\{(-?).*?(-?)\}\}", re.DOTALL)
_PRINT_SLOT_RE = re.compile(r"\{%(-?)\s*print\b.*?(-?)%\}", re.DOTALL)
_SLOT_INDEX_RE = re.compile(rf"{INERT_SLOT_NAME}(\d+)")
_JSON_ONLY_NAMES = frozenset({"true", "false", "null"})


class SlottedReference(str):
    """The inert-slot reference render; slot `__skyvern_slot__<n>` stands for `slot_expressions[n]`."""

    slot_expressions: tuple[str, ...]

    def __new__(cls, rendered: str, slot_expressions: tuple[str, ...]) -> SlottedReference:
        item = str.__new__(cls, rendered)
        item.slot_expressions = slot_expressions
        return item


def slot_template(template: str) -> tuple[str, tuple[str, ...]]:
    """Replace every value slot with a numbered inert constant, keeping whitespace-control markers so both renders trim
    identically; returns the slotted template and the original expression behind each number."""
    expressions: list[str] = []

    def _value(match: re.Match[str]) -> str:
        expressions.append(match.group(0))
        return f'{{{{{match.group(1)} "{INERT_SLOT_NAME}{len(expressions) - 1}" {match.group(2)}}}}}'

    def _print(match: re.Match[str]) -> str:
        expressions.append(match.group(0))
        return f'{{%{match.group(1)} print "{INERT_SLOT_NAME}{len(expressions) - 1}" {match.group(2)}%}}'

    slotted = _VALUE_SLOT_RE.sub(_value, template)
    return _PRINT_SLOT_RE.sub(_print, slotted), tuple(expressions)


def is_inert_slot(value: object) -> bool:
    return isinstance(value, str) and _SLOT_INDEX_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class _SlotViolation:
    slot_index: int | None
    rendered_json: bool


def rendering_introduced_security_errors(
    *, label: str, authored_code: str | None, rendered_code: str
) -> list[CodeBlockSecurityError]:
    """Refuse a render whose parameter values contributed anything but literals. `authored_code` is the same template
    rendered under the same control flow with every value slot replaced by a numbered inert slot; None fails closed.
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
    violation = _first_slot_violation(authored_tree, rendered_tree)
    if violation is None:
        return []
    expressions = authored_code.slot_expressions if isinstance(authored_code, SlottedReference) else ()
    expression = (
        expressions[violation.slot_index]
        if violation.slot_index is not None and violation.slot_index < len(expressions)
        else None
    )
    return [_parameter_code_error(label, expression=expression, rendered_json=violation.rendered_json)]


def _first_slot_violation(authored: ast.AST, rendered: ast.AST) -> _SlotViolation | None:
    if isinstance(authored, ast.Name) and is_inert_slot(authored.id):
        if _is_literal(rendered):
            return None
        return _SlotViolation(_slot_index(authored.id), _is_literal(rendered, json_names=True))
    if isinstance(authored, ast.Constant) and isinstance(authored.value, str) and INERT_SLOT_NAME in authored.value:
        if isinstance(rendered, ast.Constant) and isinstance(rendered.value, str):
            return None
        return _SlotViolation(_slot_index(authored.value), False)
    if type(authored) is not type(rendered):
        return _SlotViolation(None, False)
    for field, authored_value in ast.iter_fields(authored):
        rendered_value = getattr(rendered, field, None)
        if isinstance(authored_value, ast.AST):
            if not isinstance(rendered_value, ast.AST):
                return _SlotViolation(None, False)
            violation = _first_slot_violation(authored_value, rendered_value)
            if violation is not None:
                return violation
        elif isinstance(authored_value, list):
            if not isinstance(rendered_value, list) or len(authored_value) != len(rendered_value):
                return _SlotViolation(None, False)
            for authored_item, rendered_item in zip(authored_value, rendered_value, strict=True):
                if isinstance(authored_item, ast.AST):
                    if not isinstance(rendered_item, ast.AST):
                        return _SlotViolation(None, False)
                    violation = _first_slot_violation(authored_item, rendered_item)
                    if violation is not None:
                        return violation
                elif authored_item != rendered_item:
                    return _SlotViolation(None, False)
        elif authored_value != rendered_value:
            return _SlotViolation(None, False)
    return None


def _slot_index(text: str) -> int | None:
    match = _SLOT_INDEX_RE.search(text)
    return int(match.group(1)) if match else None


def _is_literal(node: ast.AST, *, json_names: bool = False) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if json_names and isinstance(node, ast.Name) and node.id in _JSON_ONLY_NAMES:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(element, json_names=json_names) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _is_literal(key, json_names=json_names) for key in node.keys) and all(
            _is_literal(value, json_names=json_names) for value in node.values
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal(node.operand, json_names=json_names)
    return False


def _parameter_code_error(label: str, *, expression: str | None, rendered_json: bool) -> CodeBlockSecurityError:
    error = _error(label, _PARAMETER_CODE_REASON)
    if expression is None:
        return error
    if rendered_json:
        detail = (
            f"`{expression}` rendered JSON into the Python source, and Python reads its `true`, `false` and `null` "
            "as undefined names, not values."
        )
    else:
        detail = f"`{expression}` rendered text that Python parses as code, not as a literal value."
    message = (
        f"Code block `{label}` was blocked before browser dispatch: {detail} A `{{{{ }}}}` value is pasted into the "
        "code as text; a key listed in the block's parameter_keys reaches the code as a Python variable of that name."
    )
    return CodeBlockSecurityError(message, block_label=label, reason_code=error.reason_code, surface=error.surface)


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
