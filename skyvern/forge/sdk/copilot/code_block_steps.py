from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

import structlog
import yaml

from skyvern.config import settings
from skyvern.forge.prompts import prompt_engine

LOG = structlog.get_logger()

# Playwright/SkyvernPage method name -> step action_type (values are ActionType members;
# kept as string literals so this module does not import skyvern.webeye, matching code_block_synthesis.py).
# Mirror the runtime recorder's maps (code_block_recorder._PAGE_ACTION_MAP / _LOCATOR_ACTION_MAP) so the
# static editor preview surfaces the same calls the timeline records — otherwise a recorded call (e.g.
# page.evaluate) renders in the timeline but is silently dropped from the editor step list.
_METHOD_ACTION_TYPES: dict[str, str] = {
    "goto": "goto_url",
    "click": "click",
    "dblclick": "click",
    "check": "click",
    "uncheck": "click",
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
}
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


def analyze_code_actions(code: str) -> list[CodeActionSpan]:
    """Find browser-action calls in `code` and map each to an action_type + exact line range."""
    if not code or not code.strip():
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

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
            )
        )
    spans.sort(key=lambda s: (s.line_start, s.line_end))
    return spans


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


def _describe(span: CodeActionSpan) -> str:
    value = _string_value(span.first_arg)
    if span.action_type == "goto_url":
        return f"Open {value}" if value else "Open the page"
    if span.action_type == "click":
        return f"Click {_target_label(span.receiver, span.first_arg)}"
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


_STEP_DESC_PROMPT = "workflow-copilot-code-block-steps"
_STEP_DESC_TIMEOUT_SECONDS = 20


async def refine_step_descriptions(
    code: str,
    goal: str | None,
    steps: list[dict[str, Any]],
    handler: Any | None,
) -> list[dict[str, Any]]:
    """Rewrite only the `description` of each step via an LLM; return steps unchanged on any failure."""
    if not steps or handler is None:
        return steps
    actions_json = json.dumps([{"line_start": s["line_start"], "action_type": s["action_type"]} for s in steps])
    try:
        prompt = prompt_engine.load_prompt(
            template=_STEP_DESC_PROMPT,
            goal=(goal or "").strip(),
            code=code,
            actions_json=actions_json,
        )
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=_STEP_DESC_PROMPT),
            timeout=_STEP_DESC_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        LOG.info("code-block step description pass failed; keeping templated descriptions", error=str(exc))
        return steps

    overrides = _parse_description_overrides(raw)
    if not overrides:
        return steps
    return [{**s, "description": overrides.get(s["line_start"], s["description"])} for s in steps]


def _parse_description_overrides(raw: Any) -> dict[int, str]:
    """Parse the LLM response into {line_start: description}; tolerant of dict/str/list shapes."""
    payload = raw
    if isinstance(raw, dict):
        payload = raw.get("content") or raw.get("response") or raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, list):
        return {}
    result: dict[int, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_line = item.get("line_start")
        # LLMs occasionally stringify the integer; accept "2" as 2 but reject bools/non-numerics.
        if isinstance(raw_line, bool) or not isinstance(raw_line, (int, str)):
            continue
        try:
            line_start = int(raw_line)
        except (TypeError, ValueError):
            continue
        description = str(item.get("description") or "").strip()
        if description:
            result[line_start] = description
    return result


def derive_code_block_steps_in_yaml(workflow_yaml: str) -> str:
    """Return workflow_yaml with each code block's `steps` filled from its `code` when absent.

    Deterministic, sync, and refinement-safe: a block that already carries steps
    (e.g. an LLM description pass) is left untouched, so this can run on the shared
    YAML->Workflow seam without clobbering richer steps produced upstream."""
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


async def apply_derived_code_block_steps(workflow_yaml: str, handler: Any | None = None) -> str:
    """Return workflow_yaml with each code block's `steps` recomputed from its `code`."""
    try:
        data = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml
    if not isinstance(data, (dict, list)):
        return workflow_yaml

    use_llm = settings.WORKFLOW_COPILOT_CODE_BLOCK_STEP_DESCRIPTIONS_LLM and handler is not None
    changed = False
    for block in _iter_code_block_dicts(data):
        steps = derive_code_block_steps(block["code"], block.get("prompt"))
        if use_llm:
            steps = await refine_step_descriptions(block["code"], block.get("prompt"), steps, handler)
        block["steps"] = steps
        changed = True

    if not changed:
        return workflow_yaml
    return yaml.safe_dump(data, sort_keys=False)
