from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, replace
from typing import Any, Iterator
from urllib.parse import urlsplit

import structlog
import yaml

from skyvern.forge.sdk.copilot.code_block_synthesis import _RESERVED_PARAM_NAMES
from skyvern.utils.templating import mask_jinja_control_blocks, strip_jinja_control_blocks

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
    # SkyvernPage @action_wrap high-level API (mirrors skyvern_page.py). `extract` is excluded:
    # code blocks run on a raw Playwright page and must not reach the LLM extraction path.
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
# Non-state-changing DOM reads -> an extract step in the editor outline only; deliberately absent
# from the runtime recorder maps so a read never fires a run-timeline action. Predicates, count(),
# and all() are excluded so a control-flow check never fabricates an extraction step.
_READ_METHODS: dict[str, str] = {
    "text_content": "extract",
    "all_text_contents": "extract",
    "inner_text": "extract",
    "all_inner_texts": "extract",
    "inner_html": "extract",
    "get_attribute": "extract",
    "input_value": "extract",
    "content": "extract",
}
# Methods whose natural-language `prompt` is the first positional argument (it is keyword-only on the
# interaction methods, which the keyword scan below already covers).
_PROMPT_POSITIONAL_METHODS: frozenset[str] = frozenset({"complete", "solve_captcha", "verification_code"})
# Injected into the code block's namespace as a bare builtin rather than a page method
# (block.py build_safe_vars), so it is called as `await solve_captcha(page)` with no receiver.
_BARE_NAME_METHODS: frozenset[str] = frozenset({"solve_captcha"})
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
    store_name: str | None = None  # where a read's value is stored: assigned variable, appended list, or dict key
    element_name: str | None = None  # visible name from get_by_role(name=...)/get_by_label/get_by_text
    goto_name: str | None = None
    goto_literal: str | None = None  # the string constant goto_name is bound to, when bound exactly once


def analyze_code_actions(code: str) -> list[CodeActionSpan]:
    """Find browser-action calls in `code` and map each to an action_type + exact line range."""
    if not code or not code.strip():
        return []
    tree: ast.Module | None = None
    # Stripping keeps Python that shares a line with a tag; commenting is the fallback for tag bodies that are not Python.
    for mask in (strip_jinja_control_blocks, mask_jinja_control_blocks):
        try:
            tree = ast.parse(mask(code))
            break
        # CPython's parser reports source nested too deeply as MemoryError, not RecursionError.
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
    if tree is None:
        return []

    parents: dict[ast.AST, ast.AST] = {}
    parent_fields: dict[ast.AST, str] = {}
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    parents[child] = parent
                    parent_fields[child] = field
    bindings: dict[str, list[ast.AST]] | None = None
    store_names: dict[ast.AST, str | None] = {}
    loop_vars: dict[ast.AST, str | None] = {}

    spans: list[CodeActionSpan] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        receiver_node: ast.expr | None
        if isinstance(call.func, ast.Attribute):
            method = call.func.attr
            receiver_node = call.func.value
        elif isinstance(call.func, ast.Name) and call.func.id in _BARE_NAME_METHODS:
            method = call.func.id
            receiver_node = None
        else:
            continue
        if method in _IGNORED_METHODS:
            continue
        action_type = _METHOD_ACTION_TYPES.get(method) or _READ_METHODS.get(method)
        if action_type is None:
            continue
        goto_url = _goto_url_node(call, method)
        goto_arg = goto_url if isinstance(goto_url, ast.Name) else None
        first_arg = (call.args[0] if call.args else goto_url) if receiver_node is not None else None
        goto_literal = None
        if goto_arg is not None:
            if bindings is None:
                bindings = _name_bindings(tree)
            goto_literal = _same_body_string_binding(node, goto_arg, bindings, parents, parent_fields)
        spans.append(
            CodeActionSpan(
                action_type=action_type,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", None) or node.lineno,
                method=method,
                receiver=_safe_unparse(receiver_node) if receiver_node is not None else "",
                first_arg=_safe_unparse(first_arg) if first_arg is not None else None,
                prompt=_prompt_literal(call, method),
                loop_var=_enclosing_loop_var(node, parents, loop_vars),
                store_name=_store_name(node, parents, store_names) if action_type == "extract" else None,
                element_name=(
                    _element_name(receiver_node) if action_type == "extract" and receiver_node is not None else None
                ),
                goto_name=goto_arg.id if goto_arg else None,
                goto_literal=goto_literal,
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


def _goto_url_node(call: ast.Call, method: str) -> ast.expr | None:
    if method != "goto":
        return None
    if call.args:
        return call.args[0]
    return next((keyword.value for keyword in call.keywords if keyword.arg == "url"), None)


_PASS_UP = object()


def _store_name(node: ast.AST, parents: dict[ast.AST, ast.AST], cache: dict[ast.AST, str | None]) -> str | None:
    """Where the enclosing statement stores a read's value, or None for any use that could borrow an unrelated name.

    Memoized per node, so reads sharing one long expression (a chain of `+`) walk it once in total."""
    visited: list[ast.AST] = []
    current = node
    while current not in cache:
        visited.append(current)
        outcome = _stored_in(parents.get(current), current)
        if outcome is _PASS_UP:
            current = parents[current]
            continue
        cache[current] = outcome if isinstance(outcome, str) else None
    result = cache[current]
    for seen in visited:
        cache[seen] = result
    return result


def _stored_in(parent: ast.AST | None, current: ast.AST) -> object:
    """The name `parent` stores `current` in, None when there is none, or _PASS_UP to keep walking up."""
    if parent is None:
        return None
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if parent.value is current and len(targets) == 1 and isinstance(targets[0], ast.Name):
            return targets[0].id
        return None
    if isinstance(parent, ast.Dict):
        key = next((key for key, value in zip(parent.keys, parent.values) if value is current), None)
        return _constant_str(key) if key is not None else None
    if isinstance(parent, ast.Call):
        if parent.func is current:
            return _PASS_UP
        func = parent.func
        if (
            current in parent.args
            and isinstance(func, ast.Attribute)
            and func.attr == "append"
            and isinstance(func.value, ast.Name)
        ):
            return func.value.id
        return None
    if isinstance(parent, (ast.Attribute, ast.Subscript)) and parent.value is current:
        return _PASS_UP
    if isinstance(parent, (ast.BinOp, ast.BoolOp, ast.UnaryOp)):
        return _PASS_UP
    return None


def _element_name(receiver: ast.AST) -> str | None:
    """The nearest visible element name cited by a get_by_role/get_by_label/get_by_text call in the receiver chain."""
    node: ast.AST | None = receiver
    while node is not None:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("get_by_role", "get_by_label", "get_by_text"):
                for keyword in node.keywords:
                    if keyword.arg == "name" or (keyword.arg == "text" and func.attr != "get_by_role"):
                        return _constant_str(keyword.value)
                if func.attr != "get_by_role" and node.args:
                    return _constant_str(node.args[0])
                return None
            node = func
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        else:
            node = None
    return None


def _name_bindings(tree: ast.AST) -> dict[str, list[ast.AST]]:
    bindings: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names = [node.id]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.alias):
            names = [node.asname or node.name.split(".")[0]]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names = list(node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names = [node.name]
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names = [node.rest]
        else:
            continue
        for name in names:
            bindings.setdefault(name, []).append(node)
    return bindings


def _same_body_string_binding(
    goto: ast.AST,
    name: ast.Name,
    bindings: dict[str, list[ast.AST]],
    parents: dict[ast.AST, ast.AST],
    parent_fields: dict[ast.AST, str],
) -> str | None:
    """The address `name` holds, when its only binding is `name = "<str>"` earlier in the goto's own statement body."""
    if len(bindings.get(name.id, [])) != 1:
        return None
    assignment = parents.get(bindings[name.id][0])
    if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
        return None
    statement = goto
    while not isinstance(statement, ast.stmt):
        statement = parents[statement]
    if (
        parents.get(assignment) is not parents.get(statement)
        or parent_fields.get(assignment) != parent_fields.get(statement)
        or assignment.lineno >= statement.lineno
    ):
        return None
    return _address_without_secrets(_constant_str(assignment.value))


def _address_without_secrets(value: str | None) -> str | None:
    """The literal unchanged when it is a plain http(s) address, else None so the label names the variable instead.

    Credentials, a query, a fragment, or a template placeholder never reach a label."""
    if not value or "{{" in value:
        return None
    try:
        parts = urlsplit(value)
        plain = (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and "@" not in parts.netloc
            and not parts.query
            and not parts.fragment
            and (parts.port is None or parts.port > 0)
        )
    except ValueError:
        return None
    return value if plain else None


def _constant_str(node: ast.AST) -> str | None:
    """The decoded value of a string-literal AST node, else None (e.g. a variable or f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _enclosing_loop_var(node: ast.AST, parents: dict[ast.AST, ast.AST], cache: dict[ast.AST, str | None]) -> str | None:
    """Name of the nearest enclosing for-loop's target, when it is a plain variable (memoized per ancestor)."""
    visited: list[ast.AST] = []
    current = parents.get(node)
    result: str | None = None
    while current is not None:
        if current in cache:
            result = cache[current]
            break
        visited.append(current)
        if isinstance(current, (ast.For, ast.AsyncFor)) and isinstance(current.target, ast.Name):
            result = current.target.id
            break
        current = parents.get(current)
    for seen in visited:
        cache[seen] = result
    return result


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
        if span.goto_literal:
            return f"Open {span.goto_literal}"
        if span.goto_name:
            return f"Open the {_humanize_identifier(span.goto_name)}"
        return "Open the linked page"
    if span.action_type == "extract":
        store = _humanize_identifier(span.store_name) if span.store_name else None
        if store and span.element_name:
            return f'Extract {store} from "{span.element_name}"'
        if store:
            return f"Extract {store}"
        if span.element_name:
            return f'Extract "{span.element_name}"'
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


def _consolidate_read_spans(spans: list[CodeActionSpan]) -> list[CodeActionSpan]:
    """Merge adjacent raw DOM reads that carry the same label into one extract span."""
    consolidated: list[CodeActionSpan] = []
    for span in spans:
        prev = consolidated[-1] if consolidated else None
        if (
            span.method in _READ_METHODS
            and prev is not None
            and prev.method in _READ_METHODS
            and _describe(prev) == _describe(span)
        ):
            consolidated[-1] = replace(prev, line_end=max(prev.line_end, span.line_end))
            continue
        consolidated.append(span)
    return consolidated


def derive_code_block_steps(code: str) -> list[dict[str, Any]]:
    """Derive the ordered plain-language steps for a code block from its code (deterministic)."""
    return [
        {
            "description": _describe(span),
            "action_type": span.action_type,
            "line_start": span.line_start,
            "line_end": span.line_end,
        }
        for span in _consolidate_read_spans(analyze_code_actions(code))
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
    """Return workflow_yaml with each code block's `steps` rebuilt from its `code`, discarding any it carried."""
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    changed = False
    for block in _iter_code_block_dicts(data):
        derived = derive_code_block_steps(block["code"])
        if block.get("steps") != derived:
            block["steps"] = derived
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


def fill_code_block_error_code_mappings_in_yaml(workflow_yaml: str, *, prior_yaml: str | None = None) -> str:
    """Preserve omitted code-block manifests by label while honoring explicit removal.

    An absent key means regeneration omitted the manifest. ``null`` and ``{}``
    are deliberate values and therefore remain untouched.
    """
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)) or not prior_yaml:
        return workflow_yaml

    try:
        prior_data = yaml.safe_load(prior_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(prior_data, (dict, list)):
        return workflow_yaml

    prior_mappings: dict[str, Any] = {}
    for block in _iter_code_block_dicts(prior_data):
        label = block.get("label")
        if isinstance(label, str) and "error_code_mapping" in block:
            prior_mappings[label] = block["error_code_mapping"]

    changed = False
    for block in _iter_code_block_dicts(data):
        label = block.get("label")
        if "error_code_mapping" not in block and isinstance(label, str) and label in prior_mappings:
            block["error_code_mapping"] = prior_mappings[label]
            changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)


def _declared_parameter_keys(data: Any) -> set[str]:
    definition = data.get("workflow_definition") if isinstance(data, dict) else None
    parameters = definition.get("parameters") if isinstance(definition, dict) else None
    if not isinstance(parameters, list):
        return set()
    return {
        key
        for parameter in parameters
        if isinstance(parameter, dict)
        for key in [str(parameter.get("key") or "").strip()]
        if key
    }


def _referenced_names(code: str) -> set[str]:
    """Identifiers the code actually reads.

    Read from the syntax rather than the text: a parameter named in a docstring or a string
    literal is not a reference, and binding on one would put a credential in the scope of a
    block that never asked for it. Unparseable code names nothing rather than everything.
    """
    try:
        tree = ast.parse(textwrap.dedent(code or "").strip())
    except SyntaxError:
        return set()
    # Load context only: a block that assigns the name defines its own, and binding the real
    # value there would widen a credential's scope to code that never read the parameter.
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def bind_referenced_parameters_in_yaml(workflow_yaml: str) -> str:
    """Return workflow_yaml with each code block's ``parameter_keys`` covering the declared
    parameters its own code names.

    A block's runtime scope is built from ``parameter_keys``; a name the code uses but the
    block never lists is absent at runtime, and the block dies on ``NameError`` after the
    browser work is already done. The reference in the code is the intent, so bind from it
    rather than require the submission to repeat itself. Only keys the workflow already
    declares can be added, so this cannot invent a binding.
    """
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    declared = _declared_parameter_keys(data)
    if not declared:
        return workflow_yaml

    changed = False
    for block in _iter_code_block_dicts(data):
        code = block["code"]
        # A key colliding with an executor-reserved name is dropped at bind time, and the
        # credential-field names resolve to a bound credential's secret instead of the
        # parameter, so binding one would hand the block the wrong value entirely.
        referenced = (declared & _referenced_names(code)) - _RESERVED_PARAM_NAMES
        raw_keys = block.get("parameter_keys")
        existing = [key for key in raw_keys if isinstance(key, str)] if isinstance(raw_keys, list) else []
        missing = [key for key in sorted(referenced) if key not in existing]
        if not missing:
            continue
        block["parameter_keys"] = existing + missing
        changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)
