from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Iterator

import structlog
import yaml

LOG = structlog.get_logger()

# Method name -> step action_type for the editor's static step preview (values are ActionType members,
# kept as string literals so this module does not import skyvern.webeye, matching code_block_synthesis.py).
# Must stay consistent with the two surfaces that record actions at runtime, so preview matches timeline:
# the runtime recorder's raw-Playwright maps (code_block_recorder._PAGE_ACTION_MAP / _LOCATOR_ACTION_MAP)
# and the @action_wrap(ActionType.X) decorators on the SkyvernPage high-level API (skyvern_page.py, e.g.
# page.extract / page.complete). null_action is excluded as a no-op probe.
_METHOD_ACTION_TYPES: dict[str, str] = {
    # raw Playwright (mirrors code_block_recorder)
    "goto": "goto_url",
    "click": "click",
    "dblclick": "click",
    "check": "checkbox",
    "uncheck": "checkbox",
    "tap": "click",
    "fill": "input_text",
    "type": "input_text",
    "press": "keypress",
    "press_sequentially": "input_text",
    "select_option": "select_option",
    "set_input_files": "upload_file",
    "hover": "hover",
    "go_back": "go_back",
    "go_forward": "go_forward",
    "reload": "reload_page",
    "evaluate": "execute_js",
    "wait_for_timeout": "wait",
    # SkyvernPage @action_wrap high-level API (mirrors skyvern_page.py)
    "extract": "extract",
    "fill_autocomplete": "input_text",
    "upload_file": "upload_file",
    "complete": "complete",
    "terminate": "terminate",
    "verification_code": "verification_code",
    "solve_captcha": "solve_captcha",
    "download_file": "download_file",
    "wait": "wait",
    "reload_page": "reload_page",
    "scroll": "scroll",
    "keypress": "keypress",
    "move": "move",
    "drag": "drag",
    "left_mouse": "left_mouse",
}
# Methods whose natural-language `prompt` is the first positional argument (it is keyword-only on the
# interaction methods, which the keyword scan below already covers).
_PROMPT_POSITIONAL_METHODS: frozenset[str] = frozenset({"extract", "complete", "solve_captcha", "verification_code"})
# Awaited calls that are sync/no-op helpers — never surfaced as their own step.
_IGNORED_METHODS: frozenset[str] = frozenset(
    {"wait_for_load_state", "wait_for_selector", "wait_for_url", "wait_for_function"}
)

_STRING_LITERAL = re.compile(r"""^\s*['"](.*)['"]\s*$""", re.DOTALL)
_NAME_KWARG = re.compile(r"""name\s*=\s*['"]([^'"]+)['"]""")


@dataclass
class CodeActionSpan:
    action_type: str
    line_start: int
    line_end: int
    method: str
    receiver: str  # source of the call receiver, e.g. "page" or "page.get_by_role('link', name='Login')"
    first_arg: str | None  # source of the first call arg, if any
    prompt: str | None  # natural-language `prompt` argument value, if a string literal
    loop_var: str | None  # name of the enclosing for-loop target, if the call is inside one


def analyze_code_actions(code: str) -> list[CodeActionSpan]:
    """Find browser-action calls in `code` and map each to an action_type + exact line range."""
    if not code or not code.strip():
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    spans: list[CodeActionSpan] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute):
            continue
        method = call.func.attr
        if method in _IGNORED_METHODS:
            continue
        action_type = _METHOD_ACTION_TYPES.get(method)
        if action_type is None:
            continue
        spans.append(
            CodeActionSpan(
                action_type=action_type,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", None) or node.lineno,
                method=method,
                receiver=_safe_unparse(call.func.value),
                first_arg=_safe_unparse(call.args[0]) if call.args else None,
                prompt=_prompt_literal(call, method),
                loop_var=_enclosing_loop_var(node, parents),
            )
        )
    spans.sort(key=lambda s: (s.line_start, s.line_end))
    return spans


def _prompt_literal(call: ast.Call, method: str) -> str | None:
    """The natural-language `prompt` argument as a string literal, else None.

    Decode the constant straight off the AST node — re-parsing ast.unparse output would
    re-escape real newlines/tabs in a block-scalar prompt into literal "\\n" the copy can't collapse.
    """
    for keyword in call.keywords:
        if keyword.arg == "prompt":
            return _constant_str(keyword.value)
    if method in _PROMPT_POSITIONAL_METHODS and call.args:
        return _constant_str(call.args[0])
    return None


def _constant_str(node: ast.AST) -> str | None:
    """The decoded value of a string-literal AST node, else None (e.g. a variable or f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _enclosing_loop_var(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    """Name of the nearest enclosing for-loop's target, when it is a plain variable."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.For, ast.AsyncFor)) and isinstance(current.target, ast.Name):
            return current.target.id
        current = parents.get(current)
    return None


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _string_value(arg: str | None) -> str | None:
    """Return the inner text of a simple string-literal call argument, else None."""
    if not arg:
        return None
    m = _STRING_LITERAL.match(arg)
    return m.group(1) if m else None


def _target_label(receiver: str, first_arg: str | None) -> str:
    """A short human label for the element a call acts on."""
    # Prefer an accessible name from get_by_role/get_by_label/get_by_text(name=... or "literal").
    name_kwarg = _NAME_KWARG.search(receiver)
    if name_kwarg:
        return f'"{name_kwarg.group(1)}"'
    for getter in ("get_by_label", "get_by_text", "get_by_placeholder"):
        idx = receiver.find(f"{getter}(")
        if idx != -1:
            inner = receiver[idx + len(getter) + 1 :]
            lit = _string_value(inner.split(",", 1)[0].rstrip(")"))
            if lit:
                return f'"{lit}"'
    return "the element"


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _humanize_identifier(name: str) -> str:
    return name.replace("_", " ").strip()


def _describe(span: CodeActionSpan) -> str:
    # A natural-language `prompt` is the author's own reader-facing intent (e.g.
    # "Extract the URLs of the top 20 posts"); prefer it over a structural label, but
    # ignore a whitespace-only prompt so a step never renders with blank copy.
    if span.prompt:
        normalized = _normalize_whitespace(span.prompt)
        if normalized:
            return normalized
    value = _string_value(span.first_arg)
    if span.action_type == "goto_url":
        if value:
            return f"Open {value}"
        # A non-literal URL is a link discovered at runtime; say what/why instead of
        # a repeated generic "Open the page". Inside a loop it opens each iterated item.
        if span.loop_var:
            return f"Open each {_humanize_identifier(span.loop_var)}"
        return "Open the linked page"
    if span.action_type == "extract":
        return "Extract information from the page"
    if span.action_type == "click":
        return f"Click {_target_label(span.receiver, span.first_arg)}"
    if span.action_type == "checkbox":
        return f"Toggle {_target_label(span.receiver, span.first_arg)}"
    if span.action_type == "hover":
        return f"Hover over {_target_label(span.receiver, span.first_arg)}"
    if span.action_type == "input_text":
        return f"Type into {_target_label(span.receiver, span.first_arg)}"
    if span.action_type == "select_option":
        target = _target_label(span.receiver, span.first_arg)
        return f"Select {value} in {target}" if value else f"Select an option in {target}"
    if span.action_type == "keypress":
        return f"Press {value}" if value else "Press a key"
    if span.action_type == "upload_file":
        return f"Upload a file to {_target_label(span.receiver, span.first_arg)}"
    if span.action_type == "wait":
        return "Wait"
    if span.action_type == "go_back":
        return "Go back"
    if span.action_type == "go_forward":
        return "Go forward"
    if span.action_type == "reload_page":
        return "Reload the page"
    if span.action_type == "execute_js":
        return "Run a script"
    if span.action_type == "complete":
        return "Confirm the page is complete"
    if span.action_type == "terminate":
        return "Stop the workflow"
    if span.action_type == "solve_captcha":
        return "Solve the captcha"
    if span.action_type == "verification_code":
        return "Enter the verification code"
    if span.action_type == "download_file":
        return "Download a file"
    if span.action_type == "scroll":
        return "Scroll the page"
    if span.action_type in ("move", "drag", "left_mouse"):
        return "Move the cursor"
    return "Run a step"


def derive_code_block_steps(code: str, goal: str | None = None) -> list[dict[str, Any]]:
    """Derive the ordered plain-language steps for a code block from its code (deterministic)."""
    return [
        {
            "description": _describe(span),
            "action_type": span.action_type,
            "line_start": span.line_start,
            "line_end": span.line_end,
        }
        for span in analyze_code_actions(code)
    ]


def _iter_code_block_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every code-block dict anywhere in the workflow structure (handles nested loop_blocks)."""
    if isinstance(node, dict):
        if node.get("block_type") == "code" and isinstance(node.get("code"), str):
            yield node
        for value in node.values():
            yield from _iter_code_block_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_code_block_dicts(item)


def derive_code_block_steps_in_yaml(workflow_yaml: str) -> str:
    """Return workflow_yaml with each code block's `steps` filled from its `code` when absent.

    Deterministic and sync: a block that already carries steps is left untouched, so this
    can run on the shared YAML->Workflow seam without clobbering steps produced upstream."""
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    changed = False
    for block in _iter_code_block_dicts(data):
        if block.get("steps"):
            continue
        block["steps"] = derive_code_block_steps(block["code"], block.get("prompt"))
        changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)


def fill_code_block_prompts_in_yaml(
    workflow_yaml: str,
    *,
    prior_yaml: str | None = None,
    fallback_goals: dict[str, str] | None = None,
) -> str:
    """Return workflow_yaml with each code block's `prompt` (goal) filled when absent.

    The editor treats a code block as code-first (plain view + steps) only when it
    carries a `prompt`; the model authors the goal as artifact `declared_goal`, not on
    the block, and code regeneration replaces the whole block YAML and drops it. Prefer
    the prior block's prompt by label (exact user text, preserved across regen), then a
    fallback goal by label (e.g. the model's `declared_goal`)."""
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    prior_prompts: dict[str, str] = {}
    if prior_yaml:
        try:
            prior_data = yaml.safe_load(prior_yaml)
        except yaml.YAMLError:
            prior_data = None
        if isinstance(prior_data, (dict, list)):
            for block in _iter_code_block_dicts(prior_data):
                label = block.get("label")
                prompt = block.get("prompt")
                if isinstance(label, str) and isinstance(prompt, str) and prompt:
                    prior_prompts[label] = prompt

    fallback_goals = fallback_goals or {}
    changed = False
    for block in _iter_code_block_dicts(data):
        if block.get("prompt"):
            continue
        label = block.get("label")
        if not isinstance(label, str):
            continue
        goal = prior_prompts.get(label) or fallback_goals.get(label)
        if goal:
            block["prompt"] = goal
            changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)


async def apply_derived_code_block_steps(workflow_yaml: str) -> str:
    """Return workflow_yaml with each code block's `steps` recomputed from its `code`."""
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    changed = False
    for block in _iter_code_block_dicts(data):
        block["steps"] = derive_code_block_steps(block["code"], block.get("prompt"))
        changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)
