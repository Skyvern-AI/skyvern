"""Validate skills — dry-run checks before a persist.

Shared between mid-run and post-run agents. Each validator is a pure function
of ``code`` (or ``new_code`` + ``existing_code``); none mutate. The agent
calls them BEFORE ``persist_block_edit`` / ``persist_script_rewrite`` so
syntax / API / structural regressions are caught without burning a script
revision.

The implementations re-use the v2 validators where possible to keep behavior
identical across reviewers.
"""

from __future__ import annotations

import ast
import re
from typing import Any

import libcst as cst
import structlog

from skyvern.core.script_generations.generate_script import (
    MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
    MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
)
from skyvern.core.script_generations.script_validators import (
    INTERACTION_METHODS,
    PAGE_CALL_RE,
    validate_missing_selectors,
    validate_proactive_misuse,
)
from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillError, SkillResult

LOG = structlog.get_logger()


# Mirrors v2's ScriptReviewer.PROSE_LITERAL_MIN_LEN — a literal qualifies for
# substring matching against a param value only if it has whitespace AND is at
# least this many chars. Filters out short structural tokens like dict keys.
_PROSE_LITERAL_MIN_LEN = 8


# Patterns + regexes ported from v2's ScriptReviewer. Kept in sync by
# copy-paste; future refactor: extract to script_validators.py and reuse
# from both v2 and v3 to eliminate drift.
_PARAM_REF_RE = re.compile(
    r"""context\s*\.\s*parameters\s*(?:\[\s*['"](\w+)['"]\s*\]|\.\s*get\s*\(\s*['"](\w+)['"]\s*(?:,[^)]*)?\))"""
)
_SELECTOR_SINGLE_RE = re.compile(r"""\bselector\s*=\s*f?'([^'"]*(?:"[^"]*"[^'"]*)*)'""")
_SELECTOR_DOUBLE_RE = re.compile(r'''\bselector\s*=\s*f?"([^"']*(?:'[^']*'[^"']*)*)"''')
_FRAGILE_ID_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"#dnn_\w+"),  # DotNetNuke
    re.compile(r"#ember[\-_]?\d+"),  # Ember.js
    re.compile(r"#react-select-\d+"),  # React Select
    re.compile(r"\[data-reactid=['\"][\d.]+['\"]\]"),  # React (legacy)
    re.compile(r"#ext-gen-?\d+"),  # ExtJS
    re.compile(r"\.css-[a-z0-9]{4,}"),  # CSS-in-JS (Emotion, styled-components)
    re.compile(r"\.MuiButton-root|\.Mui\w+-\w+", re.IGNORECASE),  # Material UI
    re.compile(r"#__next\w+"),  # Next.js internal
    re.compile(r"\[data-v-[a-f0-9]+\]"),  # Vue scoped styles
]
_DATE_RE = re.compile(r"\b(?:\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_SHORT_HAS_TEXT_RE = re.compile(r""":has-text\(\s*['"](.{1,2})['"]\s*\)""")
_PROMPT_RE = re.compile(r"""\bprompt\s*=\s*(?:f?['"])(.*?)(?:['"])""", re.DOTALL)


def _strip_comment_lines(code: str) -> str:
    """Blank out lines that are pure comments before regex scans so we don't
    flag values that only appear in docstring-style comments."""
    out = []
    for line in code.split("\n"):
        if line.lstrip().startswith("#"):
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def _find_param_refs(code: str) -> list[str]:
    """Return every parameter key referenced via ``context.parameters[...]``
    or ``context.parameters.get(...)``. Skips comment-only lines.
    """
    scrubbed = _strip_comment_lines(code)
    refs: list[str] = []
    for match in _PARAM_REF_RE.finditer(scrubbed):
        key = match.group(1) or match.group(2)
        if key:
            refs.append(key)
    return refs


def _collect_code_literals(code: str) -> tuple[set[str], set[str]]:
    """Walk ``code`` via libcst and return ``(exact_literals, prose_literals)``.

    See v2's ScriptReviewer._collect_code_literals for the full rationale.
    Used by ``_handler_validate_no_hardcoded_values`` to detect param values
    embedded inside strings (selector="...", prompt="...").
    """
    exact: set[str] = set()
    prose: set[str] = set()
    try:
        module = cst.parse_module(code)
    except cst.ParserSyntaxError:
        return exact, prose

    def _add(value: str) -> None:
        if not value:
            return
        exact.add(value)
        if len(value) >= _PROSE_LITERAL_MIN_LEN and any(c.isspace() for c in value):
            prose.add(value)

    class _Collector(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            try:
                value = node.evaluated_value
            except Exception:
                return
            if isinstance(value, str):
                _add(value)

        def visit_FormattedString(self, node: cst.FormattedString) -> bool:
            text_parts = [part.value for part in node.parts if isinstance(part, cst.FormattedStringText)]
            if text_parts:
                _add("".join(text_parts))
            return False

    module.visit(_Collector())
    return exact, prose


def _find_call_end(lines: list[str], start_line: int) -> int:
    depth = 0
    for i in range(start_line, len(lines)):
        depth += lines[i].count("(") - lines[i].count(")")
        if depth <= 0:
            return i
    return start_line


def _find_selector_values(text: str) -> list[str]:
    results: list[str] = []
    for m in _SELECTOR_SINGLE_RE.finditer(text):
        results.append(m.group(1))
    for m in _SELECTOR_DOUBLE_RE.finditer(text):
        results.append(m.group(1))
    return results


async def _resolve_run_parameter_values(context: Any) -> dict[str, str]:
    """Pull non-secret workflow-run parameter values from context.

    Used by validators that need to detect hardcoding against THIS run's
    inputs. Reuses v2's ``load_filtered_run_param_values`` so the
    secret/credential filtering stays consistent.
    """
    workflow_run_id = None
    if hasattr(context, "workflow_run_id"):
        workflow_run_id = getattr(context, "workflow_run_id")
    elif hasattr(context, "context") and hasattr(context.context, "workflow_run_id"):
        workflow_run_id = context.context.workflow_run_id
    if not workflow_run_id:
        return {}
    try:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        return await load_filtered_run_param_values(workflow_run_id=str(workflow_run_id))
    except Exception:
        LOG.debug("Failed to load run parameter values for validator", exc_info=True)
        return {}


async def _resolve_parameter_keys(context: Any) -> list[str]:
    """Load the workflow definition's declared parameter keys.

    Used by validate_parameter_references and validate_parameter_preservation
    to know which ``context.parameters['X']`` keys are valid.
    """
    from skyvern.forge import app

    org_id = None
    wpid = None
    for obj in (context, getattr(context, "context", None)):
        if obj is None:
            continue
        if not org_id and hasattr(obj, "organization_id"):
            org_id = getattr(obj, "organization_id")
        if not wpid and hasattr(obj, "workflow_permanent_id"):
            wpid = getattr(obj, "workflow_permanent_id")
        if org_id and wpid:
            break
    if not (org_id and wpid):
        return []
    try:
        workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=str(wpid),
            organization_id=str(org_id),
        )
        if workflow is None or workflow.workflow_definition is None:
            return []
        keys = [p.key for p in workflow.workflow_definition.parameters if getattr(p, "key", None)]
        return sorted(set(keys))
    except Exception:
        LOG.debug("Failed to load workflow parameter keys for validator", exc_info=True)
        return []


# Allowlist of page.* methods the generated code may invoke. Mirrors the
# SkyvernPage API surface used by v2 codegen. Anything else is flagged so the
# agent doesn't hallucinate methods.
_ALLOWED_PAGE_METHODS: frozenset[str] = frozenset(
    {
        # Interaction
        "click",
        "fill",
        "fill_autocomplete",
        "type",
        "select_option",
        "press",
        "hover",
        "check",
        "uncheck",
        # Navigation / state
        "goto",
        "go_back",
        "go_forward",
        "reload",
        "wait_for_load_state",
        "wait_for_url",
        "wait_for_selector",
        "wait_for_timeout",
        # Read-only
        "url",
        "title",
        "content",
        "evaluate",
        "screenshot",
        "locator",
        "query_selector",
        "query_selector_all",
        "get_by_role",
        "get_by_text",
        "get_by_label",
        "get_by_placeholder",
        # Skyvern extensions
        "extract",
        "classify",
        "agent",
        "upload_file",
        "download",
    }
)


async def _handler_compile_check(args: dict[str, Any], context: Any) -> SkillResult:
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required and must be a string")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return SkillResult.ok(
            data={
                "compiles": False,
                "error": f"{exc.msg} at line {exc.lineno}, col {exc.offset}",
                "lineno": exc.lineno,
                "offset": exc.offset,
            }
        )
    return SkillResult.ok(data={"compiles": True})


async def _handler_validate_page_api(args: dict[str, Any], context: Any) -> SkillResult:
    """Flag page.* calls whose method is NOT in the SkyvernPage allowlist."""
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    bad: list[str] = []
    for match in PAGE_CALL_RE.finditer(code):
        method = match.group(1)
        if method not in _ALLOWED_PAGE_METHODS:
            line_no = code.count("\n", 0, match.start()) + 1
            bad.append(f"page.{method}() on line {line_no}")
    if bad:
        return SkillResult.ok(
            data={
                "valid": False,
                "unknown_methods": bad,
                "hint": "Use only methods from the SkyvernPage API surface.",
            }
        )
    return SkillResult.ok(data={"valid": True})


async def _handler_validate_method_kwargs(args: dict[str, Any], context: Any) -> SkillResult:
    """Check that interaction methods have a sensible kwarg set.

    Conservative implementation: looks for the documented invariants from
    ``validate_missing_selectors``. Doesn't enforce a full kwarg schema —
    that would require parsing SkyvernPage signatures dynamically.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    msg = validate_missing_selectors(code)
    if msg:
        return SkillResult.ok(data={"valid": False, "error": msg})
    msg2 = validate_proactive_misuse(code)
    if msg2:
        return SkillResult.ok(data={"valid": False, "error": msg2})
    return SkillResult.ok(data={"valid": True})


async def _handler_validate_required_blocks_present(args: dict[str, Any], context: Any) -> SkillResult:
    """Full-script validator: every @skyvern.cached function from the
    existing code must still exist in the new code.

    Conservative: identifies cached functions by the @skyvern.cached decorator
    pattern via AST. Catches block deletion in a full-script rewrite.
    """
    new_code = args.get("new_code")
    existing_code = args.get("existing_code")
    if not isinstance(new_code, str) or not isinstance(existing_code, str):
        raise SkillError("new_code and existing_code are required")
    try:
        old_blocks = _extract_cached_function_names(existing_code)
        new_blocks = _extract_cached_function_names(new_code)
    except SyntaxError as exc:
        return SkillResult.ok(data={"valid": False, "error": f"parse_error: {exc.msg}"})
    missing = sorted(old_blocks - new_blocks)
    if missing:
        return SkillResult.ok(
            data={
                "valid": False,
                "missing_blocks": missing,
                "hint": "Full-script rewrite must preserve every @skyvern.cached function from the original.",
            }
        )
    return SkillResult.ok(data={"valid": True, "block_count": len(new_blocks)})


async def _handler_validate_structural_regression(args: dict[str, Any], context: Any) -> SkillResult:
    """Reject obvious deletion of meaningful code.

    Threshold: new_code length >= 60% of old length AND new has at least as
    many `await page.*` interaction calls as old. Conservative — false
    positives are preferred to silent block deletion.
    """
    new_code = args.get("new_code")
    existing_code = args.get("existing_code")
    if not isinstance(new_code, str) or not isinstance(existing_code, str):
        raise SkillError("new_code and existing_code are required")
    old_len = max(1, len(existing_code))
    new_len = len(new_code)
    ratio = new_len / old_len
    old_calls = len([m for m in PAGE_CALL_RE.finditer(existing_code) if m.group(1) in INTERACTION_METHODS])
    new_calls = len([m for m in PAGE_CALL_RE.finditer(new_code) if m.group(1) in INTERACTION_METHODS])
    issues: list[str] = []
    if ratio < 0.6:
        issues.append(f"size_drop ratio={ratio:.2f} (new={new_len}, old={old_len})")
    if new_calls < old_calls:
        issues.append(f"interaction_count_drop new={new_calls} old={old_calls}")
    if issues:
        return SkillResult.ok(data={"valid": False, "issues": issues})
    return SkillResult.ok(data={"valid": True, "size_ratio": round(ratio, 3)})


def _extract_cached_function_names(code: str) -> set[str]:
    """Return the set of function names decorated with ``@skyvern.cached``."""
    tree = ast.parse(code)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for dec in node.decorator_list:
                if _is_cached_decorator(dec):
                    names.add(node.name)
                    break
    return names


def _is_cached_decorator(node: ast.AST) -> bool:
    # @skyvern.cached(...) or @cached(...)
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute) and target.attr == "cached":
        return True
    if isinstance(target, ast.Name) and target.id == "cached":
        return True
    return False


async def _handler_validate_no_hardcoded_values(args: dict[str, Any], context: Any) -> SkillResult:
    """Detect run-specific parameter values baked into the code as string literals.

    Ports v2's ``_validate_no_hardcoded_values``. Loads run-parameter values
    from context (the current workflow run's inputs); flags any code string
    literal that contains a param value substring.

    Cross-run note: the agent should ALSO call ``get_cross_run_parameter_values``
    to see how param values vary across past runs of this wpid. A value that
    repeats across runs is NOT a constant — it's still runtime data.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    run_parameter_values = await _resolve_run_parameter_values(context)
    if not run_parameter_values:
        return SkillResult.ok(
            data={
                "valid": True,
                "note": "No run parameter values available; hardcoding check skipped.",
            }
        )

    exact_literals, prose_literals = _collect_code_literals(code)
    hardcoded: list[tuple[str, str]] = []
    for key, value in run_parameter_values.items():
        if len(value) < MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB:
            continue
        if len(value) > MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB:
            continue
        if value in exact_literals or any(value in lit for lit in prose_literals):
            hardcoded.append((key, value))
    if not hardcoded:
        return SkillResult.ok(data={"valid": True, "param_count_checked": len(run_parameter_values)})

    examples = [{"param_key": k, "preview": v[:60]} for k, v in hardcoded[:5]]
    return SkillResult.ok(
        data={
            "valid": False,
            "hardcoded_param_count": len(hardcoded),
            "examples": examples,
            "hint": (
                "Replace each hardcoded value with context.parameters['key']. These literals are "
                "this-run-specific and will break on future runs with different parameters."
            ),
        }
    )


async def _handler_validate_parameter_references(args: dict[str, Any], context: Any) -> SkillResult:
    """Flag ``context.parameters['X']`` where ``X`` isn't a declared parameter.

    Catches KeyError at runtime when the LLM invents parameter names. Ports
    v2's ``_validate_parameter_references``.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    parameter_keys = await _resolve_parameter_keys(context)
    if not parameter_keys:
        return SkillResult.ok(
            data={
                "valid": True,
                "note": "No declared parameters found; skipping parameter-reference check.",
            }
        )
    refs = set(_find_param_refs(code))
    invalid = sorted(refs - set(parameter_keys))
    if not invalid:
        return SkillResult.ok(data={"valid": True, "checked_keys": parameter_keys})
    return SkillResult.ok(
        data={
            "valid": False,
            "invalid_keys": invalid,
            "valid_keys": parameter_keys,
            "hint": (
                "Each invalid key would raise KeyError at runtime. Use one of the declared keys, "
                "an upstream block's output, or ai='proactive' with a descriptive prompt."
            ),
        }
    )


async def _handler_validate_parameter_preservation(args: dict[str, Any], context: Any) -> SkillResult:
    """Flag rewrites that silently drop ``value=context.parameters['X']`` refs.

    Catches the case where the agent's rewrite replaces a parameterized fill
    with an ``ai='proactive'`` no-op. Ports v2's ``_validate_parameter_preservation``.
    """
    new_code = args.get("new_code")
    existing_code = args.get("existing_code")
    if not isinstance(new_code, str) or not isinstance(existing_code, str):
        raise SkillError("new_code and existing_code are required")
    parameter_keys = await _resolve_parameter_keys(context)
    if not parameter_keys:
        return SkillResult.ok(data={"valid": True, "note": "No declared parameters to check preservation against."})
    old_refs = set(_find_param_refs(existing_code)) & set(parameter_keys)
    new_refs = set(_find_param_refs(new_code)) & set(parameter_keys)
    dropped = sorted(old_refs - new_refs)
    if not dropped:
        return SkillResult.ok(data={"valid": True, "preserved_refs": sorted(old_refs)})
    return SkillResult.ok(
        data={
            "valid": False,
            "dropped_refs": dropped,
            "hint": (
                "Every page.fill / page.fill_autocomplete for a field that maps to a workflow "
                "parameter MUST include value=context.parameters['key']. Do NOT replace with "
                "ai='proactive' — parameter values are deterministic; AI-generated values are not."
            ),
        }
    )


async def _handler_validate_fragile_selectors(args: dict[str, Any], context: Any) -> SkillResult:
    """Flag selectors using auto-generated framework IDs.

    Ports v2's ``_validate_fragile_selectors``. Catches `#ember-123`,
    `.css-abc1`, `[data-reactid="X"]`, Material UI auto-classes, etc. —
    selectors that change across deployments and break caching.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    issues: list[dict[str, Any]] = []
    lines = code.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            i += 1
            continue
        match = PAGE_CALL_RE.search(lines[i])
        if match:
            end_line = _find_call_end(lines, i)
            call_text = "\n".join(lines[i : end_line + 1]) if end_line > i else lines[i]
            line_num = i + 1
        else:
            call_text = lines[i]
            end_line = i
            line_num = i + 1
        for sel_val in _find_selector_values(call_text):
            for pattern in _FRAGILE_ID_PATTERNS:
                if pattern.search(sel_val):
                    issues.append(
                        {
                            "line": line_num,
                            "selector_preview": sel_val[:80],
                            "pattern": pattern.pattern,
                        }
                    )
                    break
        i = end_line + 1 if match and end_line > i else i + 1
    if not issues:
        return SkillResult.ok(data={"valid": True})
    return SkillResult.ok(
        data={
            "valid": False,
            "fragile_count": len(issues),
            "examples": issues[:5],
            "hint": (
                "Replace with stable selectors: aria-label, placeholder, name, role, data-testid, "
                "or :has-text() with stable text. If no stable selector exists, use ai='proactive' "
                "with a descriptive prompt and OMIT selector="
            ),
        }
    )


async def _handler_validate_hardcoded_run_data(args: dict[str, Any], context: Any) -> SkillResult:
    """Flag selectors/prompts containing dates, PII, or 1-2 char :has-text().

    Ports v2's ``_validate_hardcoded_run_data``. Catches per-run data baked
    into the script — dates in MM/DD/YYYY or YYYY-MM-DD format, very-short
    `:has-text("X")` strings (almost certainly hardcoded data), and email
    addresses in text_patterns.
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise SkillError("code is required")
    issues: list[dict[str, Any]] = []
    lines = code.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("#"):
            i += 1
            continue
        match = PAGE_CALL_RE.search(lines[i])
        if match:
            end_line = _find_call_end(lines, i)
            call_text = "\n".join(lines[i : end_line + 1]) if end_line > i else lines[i]
        else:
            call_text = lines[i]
            end_line = i
        line_num = i + 1
        for sel_val in _find_selector_values(call_text):
            if d := _DATE_RE.search(sel_val):
                issues.append({"line": line_num, "kind": "date_in_selector", "value": d.group()})
            for ht in _SHORT_HAS_TEXT_RE.finditer(sel_val):
                short = ht.group(1)
                if short.lower() not in {"ok", "no", "x", "✓", "→", "←"}:
                    issues.append({"line": line_num, "kind": "short_has_text", "value": short})
        if prompt_match := _PROMPT_RE.search(call_text):
            if d := _DATE_RE.search(prompt_match.group(1)):
                issues.append({"line": line_num, "kind": "date_in_prompt", "value": d.group()})
        i = end_line + 1 if match and end_line > i else i + 1

    # PII (emails) inside text_patterns dicts — flat scan independent of
    # interaction-method gathering above.
    tp_idx = code.find("text_patterns")
    while tp_idx != -1:
        depth = 0
        end = tp_idx
        for j in range(tp_idx, len(code)):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end == tp_idx:
            end = tp_idx + len("text_patterns")
        block = code[tp_idx:end]
        tp_line = code[:tp_idx].count("\n") + 1
        if e := _EMAIL_RE.search(block):
            issues.append({"line": tp_line, "kind": "email_in_text_patterns", "value": e.group()})
        tp_idx = code.find("text_patterns", end)

    if not issues:
        return SkillResult.ok(data={"valid": True})
    return SkillResult.ok(
        data={
            "valid": False,
            "issue_count": len(issues),
            "examples": issues[:5],
            "hint": (
                "Dates, invoice numbers, emails, and 1-2 char :has-text() values must NOT be "
                "hardcoded. Use context.parameters['key'] for dynamic values, or generic page "
                "structure indicators (form labels, button text, navigation links) in text_patterns."
            ),
        }
    )


def all_validate_skills() -> list[Skill]:
    return [
        Skill(
            name="compile_check",
            handler=_handler_compile_check,
            schema={
                "name": "compile_check",
                "description": (
                    "Parse the given Python source. Returns compiles=False with the "
                    "syntax error line/column if it fails. Run this BEFORE every persist."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_page_api",
            handler=_handler_validate_page_api,
            schema={
                "name": "validate_page_api",
                "description": (
                    "Check that every page.* call uses a method on the SkyvernPage allowlist. "
                    "Flags hallucinated method names."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_method_kwargs",
            handler=_handler_validate_method_kwargs,
            schema={
                "name": "validate_method_kwargs",
                "description": (
                    "Check that page.click / page.fill / page.type / page.fill_autocomplete / "
                    "page.select_option calls have correct kwargs (e.g., selector= or "
                    "ai='proactive'). Surfaces missing-selector regressions."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_required_blocks_present",
            handler=_handler_validate_required_blocks_present,
            schema={
                "name": "validate_required_blocks_present",
                "description": (
                    "Full-script edit validator. Confirms every @skyvern.cached function from "
                    "the existing main.py is still present in the proposed rewrite."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "new_code": {"type": "string"},
                        "existing_code": {"type": "string"},
                    },
                    "required": ["new_code", "existing_code"],
                },
            },
        ),
        Skill(
            name="validate_structural_regression",
            handler=_handler_validate_structural_regression,
            schema={
                "name": "validate_structural_regression",
                "description": (
                    "Reject obvious deletions: new_code length < 60% of existing_code OR new "
                    "code has fewer page.<interaction> calls than existing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "new_code": {"type": "string"},
                        "existing_code": {"type": "string"},
                    },
                    "required": ["new_code", "existing_code"],
                },
            },
        ),
        Skill(
            name="validate_no_hardcoded_values",
            handler=_handler_validate_no_hardcoded_values,
            schema={
                "name": "validate_no_hardcoded_values",
                "description": (
                    "CRITICAL cross-run safety check. Loads the current run's workflow parameter "
                    "values and flags any code string literal that contains them. Catches the "
                    "case where the agent bakes a per-run value (search term, email, date) into "
                    "a selector or click value — the script will break for other runs with "
                    "different parameters. Run BEFORE every persist."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_parameter_references",
            handler=_handler_validate_parameter_references,
            schema={
                "name": "validate_parameter_references",
                "description": (
                    "Check that every context.parameters['X'] or .get('X') references a key "
                    "declared in the workflow definition. Catches KeyError crashes at runtime."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_parameter_preservation",
            handler=_handler_validate_parameter_preservation,
            schema={
                "name": "validate_parameter_preservation",
                "description": (
                    "Confirm the new code preserves every context.parameters['X'] reference "
                    "from existing_code. Catches the silent regression where the agent replaces "
                    "value=context.parameters['X'] with ai='proactive' (which has no value)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "new_code": {"type": "string"},
                        "existing_code": {"type": "string"},
                    },
                    "required": ["new_code", "existing_code"],
                },
            },
        ),
        Skill(
            name="validate_fragile_selectors",
            handler=_handler_validate_fragile_selectors,
            schema={
                "name": "validate_fragile_selectors",
                "description": (
                    "Flag selectors using auto-generated framework IDs (#ember-123, .css-abc1, "
                    "[data-reactid=X], #__next..., MuiButton-root, etc.). These change across "
                    "deployments and are the leading cause of selector breakage."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
        Skill(
            name="validate_hardcoded_run_data",
            handler=_handler_validate_hardcoded_run_data,
            schema={
                "name": "validate_hardcoded_run_data",
                "description": (
                    "Flag dates, PII (emails in text_patterns), and very-short :has-text() "
                    "values (1-2 chars) baked into the code. These are almost certainly "
                    "per-run data, not stable selectors."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        ),
    ]


__all__ = ["all_validate_skills"]
