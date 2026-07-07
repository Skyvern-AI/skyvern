from __future__ import annotations

import abc
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal, Union

import structlog
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field, model_validator

from skyvern.config import settings
from skyvern.exceptions import ConditionalBranchEvaluationError, MalformedBranchEvaluationError
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter, MissingJinjaVariables
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.utils.strings import generate_random_string
from skyvern.utils.templating import get_missing_variables

LOG = structlog.get_logger()


class BranchEvaluationContext:
    """Collection of runtime data that BranchCriteria evaluators can consume."""

    def __init__(
        self,
        *,
        workflow_run_context: WorkflowRunContext | None = None,
        block_label: str | None = None,
        template_renderer: Callable[[str], str] | None = None,
    ) -> None:
        self.workflow_run_context = workflow_run_context
        self.block_label = block_label
        self.template_renderer = template_renderer

    def build_llm_safe_context_snapshot(self) -> dict[str, Any]:
        """
        Build a minimal context blob for LLM-facing branch evaluation.

        Only includes essential data the LLM needs to evaluate conditions:
        - Parameter values (base_date, date_1, etc.)
        - Extracted information from previous blocks
        - Loop variables (current_value, current_index, current_item)
        """
        if self.workflow_run_context is None:
            return {}

        ctx = self.workflow_run_context
        raw_values: dict[str, Any] = ctx.values.copy()

        # Keys to skip - these are not useful for evaluating conditions
        keys_to_skip = {
            "blocks_metadata",
            "params",
            "outputs",
            "environment",
            "env",
            "llm",
            "workflow_title",
            "workflow_id",
            "workflow_permanent_id",
            "workflow_run_id",
        }

        snapshot: dict[str, Any] = {}
        for key, value in raw_values.items():
            # Skip noisy keys
            if key in keys_to_skip:
                continue

            # For block outputs (dicts with extracted_information), only include extracted_information
            if isinstance(value, dict) and "extracted_information" in value:
                extracted = value.get("extracted_information")
                if extracted is not None:
                    snapshot[key] = extracted
            else:
                # Include parameter values directly
                snapshot[key] = value

        # Copy loop variables (current_value, current_index, current_item) to top level
        # Required for pure NatLang expressions like "current_value['date']" to work
        if self.block_label:
            block_metadata = ctx.get_block_metadata(self.block_label)
            if "current_value" in block_metadata:
                snapshot["current_value"] = block_metadata["current_value"]
            if "current_index" in block_metadata:
                snapshot["current_index"] = block_metadata["current_index"]
            if "current_item" in block_metadata:
                snapshot["current_item"] = block_metadata["current_item"]

        # Mask any real secret values that may have leaked into values
        snapshot = ctx.mask_secrets_in_data(snapshot)

        return snapshot

    def build_template_data(self) -> dict[str, Any]:
        """Build Jinja template data mirroring block parameter rendering context."""
        if self.workflow_run_context is None:
            return {
                "params": {},
                "outputs": {},
                "environment": {},
                "env": {},
                "llm": {},
            }

        ctx = self.workflow_run_context
        template_data = ctx.values.copy()
        if ctx.include_secrets_in_templates:
            template_data.update(ctx.secrets)

            credential_params: list[tuple[str, dict[str, Any]]] = []
            for key, value in template_data.items():
                if isinstance(value, dict) and "context" in value and "username" in value and "password" in value:
                    credential_params.append((key, value))

            for key, value in credential_params:
                username_secret_id = value.get("username", "")
                password_secret_id = value.get("password", "")
                real_username = template_data.get(username_secret_id, "")
                real_password = template_data.get(password_secret_id, "")
                template_data[f"{key}_real_username"] = real_username
                template_data[f"{key}_real_password"] = real_password

        if self.block_label:
            block_reference_data: dict[str, Any] = ctx.get_block_metadata(self.block_label)
            if self.block_label in template_data:
                current_value = template_data[self.block_label]
                if isinstance(current_value, dict):
                    block_reference_data.update(current_value)
            template_data[self.block_label] = block_reference_data

            if "current_index" in block_reference_data:
                template_data["current_index"] = block_reference_data["current_index"]
            if "current_item" in block_reference_data:
                template_data["current_item"] = block_reference_data["current_item"]
            if "current_value" in block_reference_data:
                template_data["current_value"] = block_reference_data["current_value"]

        template_data.setdefault("workflow_title", ctx.workflow_title)
        template_data.setdefault("workflow_id", ctx.workflow_id)
        template_data.setdefault("workflow_permanent_id", ctx.workflow_permanent_id)
        template_data.setdefault("workflow_run_id", ctx.workflow_run_id)

        # Late import: block.py is the facade over this leaf module, so pull its
        # constants only at call time to keep this module importable on its own.
        from skyvern.forge.sdk.workflow.models.block import CURRENT_DATE_FORMAT

        template_data.setdefault("current_date", datetime.now(timezone.utc).strftime(CURRENT_DATE_FORMAT))

        template_data.setdefault("params", template_data.get("params", {}))
        template_data.setdefault("outputs", template_data.get("outputs", {}))
        template_data.setdefault("environment", template_data.get("environment", {}))
        template_data.setdefault("env", template_data.get("environment"))
        template_data.setdefault("llm", template_data.get("llm", {}))

        return template_data


class BranchCriteria(BaseModel, abc.ABC):
    """Abstract interface describing how a branch condition should be evaluated."""

    criteria_type: str
    expression: str
    description: str | None = None

    @abc.abstractmethod
    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        """Return True when the branch should execute."""
        raise NotImplementedError

    def requires_llm(self) -> bool:
        """Whether the criteria relies on an LLM classification step."""
        return False


def _evaluate_truthy_string(value: str) -> bool:
    """
    Evaluate a string as a boolean, handling common truthy/falsy representations.

    Truthy: "true", "True", "TRUE", "1", "yes", "y", "on", non-zero numbers
    Falsy: "", "false", "False", "FALSE", "0", "no", "n", "off", "null", "None", whitespace-only

    For other strings, use Python's default bool() behavior (non-empty = truthy).
    """
    if not value or not value.strip():
        return False

    normalized = value.strip().lower()

    # Explicit falsy values
    if normalized in ("false", "0", "no", "n", "off", "null", "none"):
        return False

    # Explicit truthy values
    if normalized in ("true", "1", "yes", "y", "on"):
        return True

    # Try to parse as a number
    try:
        num = float(normalized)
        return num != 0.0
    except ValueError:
        pass

    # For any other non-empty string, consider it truthy
    # This allows expressions like "{{ 'some text' }}" to be truthy
    return True


class JinjaBranchCriteria(BranchCriteria):
    """Jinja2-templated branch criteria (only supported criteria type for now)."""

    criteria_type: Literal["jinja2_template"] = "jinja2_template"

    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        # Prefer the renderer provided by the caller (matches block parameter rendering),
        # otherwise build a minimal sandboxed renderer using the evaluation context.
        if context.template_renderer:
            try:
                rendered = context.template_renderer(self.expression)
            except MissingJinjaVariables:
                # Let upstream MissingJinjaVariables bubble as-is.
                raise
            except Exception as exc:  # pragma: no cover - caught for robustness
                raise FailedToFormatJinjaStyleParameter(self.expression, str(exc)) from exc
        else:
            template_data = context.build_template_data()
            sandbox_env = (
                SandboxedEnvironment(undefined=StrictUndefined)
                if settings.WORKFLOW_TEMPLATING_STRICTNESS == "strict"
                else SandboxedEnvironment()
            )

            try:
                missing_vars = get_missing_variables(self.expression, template_data)
                if missing_vars:
                    raise MissingJinjaVariables(self.expression, missing_vars)

                template = sandbox_env.from_string(self.expression)
                rendered = template.render(template_data)
            except MissingJinjaVariables:
                raise
            except Exception as exc:
                # Covers syntax errors and rendering issues
                raise FailedToFormatJinjaStyleParameter(self.expression, str(exc)) from exc

        return _evaluate_truthy_string(rendered)


class PromptBranchCriteria(BranchCriteria):
    """Natural language branch criteria."""

    criteria_type: Literal["prompt"] = "prompt"

    async def evaluate(self, context: BranchEvaluationContext) -> bool:
        # Evaluated via ConditionalBlock.execute (batched) or WhileLoopBlock
        # _evaluate_condition (single-branch batch helper).
        raise NotImplementedError("PromptBranchCriteria is evaluated via extraction batch helpers, not per-branch.")

    def requires_llm(self) -> bool:
        return True


def _is_pure_jinja_expression(expression: str) -> bool:
    """
    Determine if an expression is a pure Jinja template (single block) vs Jinja+NatLang (mixed).

    Pure Jinja: "{{ A == B }}" - single Jinja block, should be evaluated server-side
    Jinja+NatLang: "{{ A }} is same as {{ B }}" - multiple Jinja blocks mixed with natural language

    Returns True only for pure Jinja expressions that can be evaluated to boolean server-side.
    """
    if not expression:
        return False

    stripped = expression.strip()

    # Must start with {{ and end with }}
    if not (stripped.startswith("{{") and stripped.endswith("}}")):
        return False

    # Count the number of {{ occurrences
    # If there's more than one, it's Jinja+NatLang (e.g., "{{ A }} is same as {{ B }}")
    jinja_open_count = stripped.count("{{")
    if jinja_open_count > 1:
        return False

    # Single {{ and ends with }} - this is pure Jinja
    return True


def _resolve_nested_path(value: Any, path: str) -> Any:
    """
    Resolve a dotted/bracket access path on a nested value.

    Examples:
        _resolve_nested_path({"a": {"b": 1}}, ".a.b") -> 1
        _resolve_nested_path([{"x": 2}], "[0].x") -> 2

    Args:
        value: The root value to traverse
        path: The access path (e.g., ".field1.field2[0].field3")

    Returns:
        The resolved leaf value

    Raises:
        LookupError: If the path cannot be resolved
    """
    segments = re.findall(r"\.([a-zA-Z_]\w*)|\[(\d+)\]", path)
    current = value
    for dot_key, bracket_idx in segments:
        if dot_key:
            if isinstance(current, dict):
                if dot_key not in current:
                    raise LookupError(f"Key {dot_key!r} not found")
                current = current[dot_key]
            else:
                raise LookupError(f"Cannot access .{dot_key} on {type(current).__name__}")
        elif bracket_idx:
            idx = int(bracket_idx)
            if isinstance(current, (list, tuple)):
                if idx >= len(current):
                    raise LookupError(f"Index [{idx}] out of range")
                current = current[idx]
            else:
                raise LookupError(f"Cannot index [{idx}] on {type(current).__name__}")
    return current


_JINJA_DISPLAY_FILTERS: dict[str, Callable[[Any], Any]] = {
    "lower": lambda v: str(v).lower(),
    "upper": lambda v: str(v).upper(),
    "trim": lambda v: str(v).strip(),
    "title": lambda v: str(v).title(),
    "capitalize": lambda v: str(v).capitalize(),
    "int": lambda v: int(v),
    "float": lambda v: float(v),
    "string": lambda v: str(v),
    "length": lambda v: len(v),
    "abs": lambda v: abs(v),
}


def _render_jinja_expression_for_display(
    expression: str,
    context_values: dict[str, Any],
    block_label: str | None = None,
) -> str:
    """
    Render a pure Jinja expression for UI display by substituting variable names with values.

    This is for display purposes only - it shows users what values were compared
    without actually evaluating the expression. For example:
    - Input: "{{ base_date == date_1 }}" with context {"base_date": "01-25-2026", "date_1": "01-25-2026"}
    - Output: '"01-25-2026" == "01-25-2026"'
    - Input: "{{ output.extracted_information.field != None }}" with nested dict context
    - Output: '"some_value" != None'
    - Input: "{{ output.status|lower == 'active' }}" with context {"output": {"status": "Active"}}
    - Output: '"active" == \'active\''

    Known Jinja filters (lower, upper, trim, etc.) are applied to the resolved value.
    Unknown filters are left as-is in the output.

    Returns the original expression if it's not a pure Jinja expression or if rendering fails.
    """
    if not _is_pure_jinja_expression(expression):
        return expression

    try:
        # Extract inner expression (strip {{ and }})
        inner_expr = expression.strip()[2:-2].strip()
        display_expr = inner_expr

        # Substitute variable references (including dotted/bracket access paths and filters)
        # with their values.
        # Match var_name optionally followed by .field or [index] segments,
        # then optionally followed by a |filter_name.
        # Sort by key length (longest first) to avoid partial matches.
        for var_name in sorted(context_values.keys(), key=len, reverse=True):
            pattern = r"\b" + re.escape(var_name) + r"((?:\.[a-zA-Z_]\w*|\[\d+\])*)(\|[a-zA-Z_]\w*)?"

            def _replacer(match: re.Match, _var_name: str = var_name) -> str:
                access_path = match.group(1)  # the dotted/bracket part after var_name
                filter_expr = match.group(2)  # e.g., "|lower" or None
                var_value = context_values[_var_name]

                if access_path:
                    try:
                        var_value = _resolve_nested_path(var_value, access_path)
                    except LookupError:
                        # Path couldn't be resolved — return original text unchanged
                        return match.group(0)

                if filter_expr:
                    filter_name = filter_expr[1:]  # strip the leading |
                    filter_fn = _JINJA_DISPLAY_FILTERS.get(filter_name)
                    if filter_fn is not None:
                        try:
                            var_value = filter_fn(var_value)
                        except Exception:
                            # Filter application failed — show value with filter text
                            if isinstance(var_value, str):
                                return f'"{var_value}"{filter_expr}'
                            return f"{var_value}{filter_expr}"
                    else:
                        # Unknown filter — show value with filter text preserved
                        if isinstance(var_value, str):
                            return f'"{var_value}"{filter_expr}'
                        return f"{var_value}{filter_expr}"

                if isinstance(var_value, str):
                    return f'"{var_value}"'
                return str(var_value)

            display_expr = re.sub(pattern, _replacer, display_expr)

        return display_expr
    except Exception as exc:
        LOG.debug(
            "Failed to render Jinja expression for display",
            block_label=block_label,
            expression=expression,
            error=str(exc),
        )
        return expression


def _find_evaluations_array(output_value: dict[str, Any]) -> list[Any]:
    """
    Extract the evaluations array from LLM output.

    ExtractionBlock wraps output in 'extracted_information', so we check there first.
    Falls back to direct access if not found in the nested structure.

    Args:
        output_value: The raw output from ExtractionBlock

    Returns:
        List of evaluation objects from the LLM

    Raises:
        ValueError: If evaluations array is not found or has wrong type
    """
    # Try standard ExtractionBlock format: output_value.extracted_information.evaluations
    extracted_info = output_value.get("extracted_information")
    if isinstance(extracted_info, dict):
        raw_evaluations = extracted_info.get("evaluations")
    else:
        # Fallback: try direct access at output_value.evaluations
        raw_evaluations = output_value.get("evaluations")

    if not isinstance(raw_evaluations, list):
        raise ValueError(f"Expected array of evaluations, got: {type(raw_evaluations)}")

    return raw_evaluations


def _parse_single_evaluation(
    evaluation: Any,
    idx: int,
    fallback_rendered_expressions: list[str],
) -> tuple[bool, str]:
    """
    Parse a single evaluation from the LLM response.

    Handles two formats:
    - Dict format: {result: bool, reasoning: str}
    - Legacy format: just a boolean value

    The rendered expression always comes from the Jinja pre-rendering step (fallback),
    not from the LLM response, to avoid the LLM re-interpreting already-resolved values.

    Args:
        evaluation: Single evaluation object from LLM (dict or bool)
        idx: Index of this evaluation (for fallback lookup)
        fallback_rendered_expressions: Pre-rendered expressions from Jinja rendering

    Returns:
        Tuple of (boolean_result, rendered_expression_string)
    """
    rendered_expression = fallback_rendered_expressions[idx] if idx < len(fallback_rendered_expressions) else ""

    if isinstance(evaluation, dict):
        result = evaluation.get("result")
        if isinstance(result, bool):
            bool_result = result
        else:
            bool_result = _evaluate_truthy_string(str(result))
            LOG.warning(
                "Conditional branch evaluation returned non-boolean result",
                branch_index=idx,
                result=result,
                evaluated_result=bool_result,
            )

        return (bool_result, rendered_expression)
    else:
        # Legacy format: just a boolean
        if isinstance(evaluation, bool):
            bool_result = evaluation
        else:
            bool_result = _evaluate_truthy_string(str(evaluation))

        return (bool_result, rendered_expression)


# Number of times to evaluate the prompt-based conditional branches before giving up.
# The dominant failure is a transient malformed/under-returned LLM batch; one re-roll
# recovers most of them while still failing loudly when the model is persistently wrong.
MAX_PROMPT_BRANCH_EVAL_ATTEMPTS = 2


def _build_branch_evaluation_schema(num_branches: int) -> dict[str, Any]:
    """Strict JSON schema for the batched branch-evaluation LLM call.

    ``additionalProperties: false`` plus a required ``condition_index`` stop the model from
    injecting hallucinated fields and force one self-identifying result per condition.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "condition_index": {
                            "type": "integer",
                            "description": (
                                "The 1-based index of the condition this object evaluates "
                                "(Condition 1 -> 1, Condition 2 -> 2, ...)."
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Explanation of the reasoning behind evaluating the condition.",
                        },
                        "result": {
                            "type": "boolean",
                            "description": "TRUE if the condition is satisfied, FALSE otherwise.",
                        },
                    },
                    "required": ["condition_index", "reasoning", "result"],
                },
                "description": "Exactly one evaluation per condition, covering condition_index 1..N.",
                "minItems": num_branches,
                "maxItems": num_branches,
            }
        },
        "required": ["evaluations"],
    }


def _coerce_condition_index(raw: Any) -> int | None:
    """Read a 1-based ``condition_index`` the LLM may have typed loosely.

    Accepts ints, integral floats (``2.0``), and digit strings (``"2"``); returns ``None`` for
    bools (an int subclass that must not read as 0/1) and non-integral/garbage values. The schema
    is a prompt-level hint, not provider-enforced, so a model that under-returns can equally
    mistype the index; coercing keeps a loosely-typed index on the order-safe alignment path
    instead of misrouting via positional fallback.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _align_branch_evaluations(
    *,
    output_value: Any,
    branches: list[BranchCondition],
    rendered_expressions: list[str],
) -> tuple[list[bool], list[str], dict[str, Any]]:
    """Align the LLM evaluations to ``branches``.

    Returns ``(results, rendered_expressions, normalized_output)``, where ``normalized_output``
    is the evaluations wrapped in a dict for recording/UI. Prefers the LLM-provided 1-based
    ``condition_index`` (coerced from ints, integral floats, or digit strings): this is order-safe
    and immune to hallucinated extra entries that would shift positional alignment. Falls back to
    positional parsing only when no usable index is present and the count matches exactly. A
    wrong-length or mis-indexed batch is discarded wholesale (never gap-filled) by raising
    ``MalformedBranchEvaluationError`` so the caller can retry or fail loudly.
    """
    n = len(branches)

    if isinstance(output_value, list):
        output_value = {"evaluations": output_value}
    if not isinstance(output_value, dict):
        raise MalformedBranchEvaluationError(f"unexpected output format: {type(output_value)}")
    try:
        raw_evaluations = _find_evaluations_array(output_value)
    except ValueError as exc:
        raise MalformedBranchEvaluationError(str(exc)) from exc

    by_index: dict[int, Any] = {}
    saw_condition_index = False
    for evaluation in raw_evaluations:
        if not isinstance(evaluation, dict):
            continue
        raw_index = _coerce_condition_index(evaluation.get("condition_index"))
        if raw_index is None:
            continue
        saw_condition_index = True
        if not (1 <= raw_index <= n):
            continue
        if raw_index in by_index:
            raise MalformedBranchEvaluationError(f"duplicate condition_index {raw_index}")
        by_index[raw_index] = evaluation

    if saw_condition_index:
        if set(by_index.keys()) != set(range(1, n + 1)):
            raise MalformedBranchEvaluationError(f"condition_index set {sorted(by_index.keys())} does not cover 1..{n}")
        ordered: list[Any] = [by_index[i] for i in range(1, n + 1)]
    else:
        # Legacy path (no condition_index): the LLM occasionally appends reasoning=None
        # placeholder entries. Strip them and accept only when the remainder is exactly N.
        well_formed = [e for e in raw_evaluations if not (isinstance(e, dict) and e.get("reasoning") is None)]
        ordered = well_formed if len(well_formed) == n else list(raw_evaluations)
        if len(ordered) != n:
            raise MalformedBranchEvaluationError(f"returned {len(ordered)} results for {n} branches")

    results_array: list[bool] = []
    llm_rendered_expressions: list[str] = []
    for idx, evaluation in enumerate(ordered):
        bool_result, rendered_expr = _parse_single_evaluation(
            evaluation=evaluation,
            idx=idx,
            fallback_rendered_expressions=rendered_expressions,
        )
        results_array.append(bool_result)
        llm_rendered_expressions.append(rendered_expr)
    return results_array, llm_rendered_expressions, output_value


# Pattern to find Jinja template blocks like {{ variable_name }}
_JINJA_BLOCK_RE = re.compile(r"\{\{(.*?)\}\}")
# Marker inserted into rendered expressions when a Jinja variable resolved to
# an empty/whitespace-only value.  The LLM uses this to reason about emptiness.
_EMPTY_VALUE_MARKER = "(empty value)"


def _make_empty_params_explicit(
    original_expression: str,
    rendered_expression: str,
) -> tuple[str, bool]:
    """
    Detect Jinja template variables that resolved to empty values and replace
    the empty gaps with explicit ``(empty value)`` markers.

    When ``{{test_parameter}}`` resolves to ``""``, the rendered expression becomes
    malformed (e.g., ``"if  is not empty"``).  This function detects such cases by
    comparing the *original* expression (with ``{{ }}`` blocks) against the
    *rendered* expression and rebuilds it with clear markers so the LLM can
    evaluate the condition correctly.

    Returns:
        ``(patched_expression, was_patched)``
    """
    if not original_expression or "{{" not in original_expression:
        return rendered_expression, False

    # Split the original expression into alternating [static, var, static, var, ...] parts.
    parts = _JINJA_BLOCK_RE.split(original_expression)
    if len(parts) <= 1:
        return rendered_expression, False

    # Extract static parts (even indices) and build a regex that captures what
    # each Jinja block rendered to by using the static text as anchors.
    static_parts = [parts[i] for i in range(0, len(parts), 2)]
    num_vars = len(parts) // 2

    # When two Jinja variables are adjacent (e.g. "{{a}}{{b}}") the interior
    # static separator is an empty string and the non-greedy regex cannot
    # reliably attribute rendered text to the correct variable.  Bail out.
    if num_vars > 1 and any(static == "" for static in static_parts[1:-1]):
        return rendered_expression, False

    # NOTE: if a rendered value happens to contain the same text as a static
    # anchor the regex may split on the wrong occurrence.  This is extremely
    # unlikely in user-authored conditional expressions and the worst-case
    # outcome is an unnecessary "(empty value)" marker, which still beats the
    # invisible empty-string that caused SKY-8073.

    regex_fragments: list[str] = []
    for i, static in enumerate(static_parts):
        regex_fragments.append(re.escape(static))
        if i < num_vars:
            regex_fragments.append("(.*?)")

    match = re.match("^" + "".join(regex_fragments) + "$", rendered_expression, re.DOTALL)
    if not match:
        return rendered_expression, False

    rendered_values = match.groups()
    has_empty = any(not v.strip() for v in rendered_values)
    if not has_empty:
        return rendered_expression, False

    # Rebuild the expression, replacing empty rendered values with an explicit marker.
    result_parts: list[str] = []
    for i, static in enumerate(static_parts):
        result_parts.append(static)
        if i < len(rendered_values):
            if not rendered_values[i].strip():
                result_parts.append(_EMPTY_VALUE_MARKER)
            else:
                result_parts.append(rendered_values[i])

    return "".join(result_parts), True


# Per-field cap for DecisionBlock debug payload (rendered_expression, llm_response, llm_prompt).
# Same fields exist as branch_metadata debug surface for script-reviewer / UI display; their
# unbounded form has produced multi-hundred-MB output_parameter rows under recursive Jinja.
DECISION_BLOCK_FIELD_MAX_BYTES = 64 * 1024


def _cap_debug_field(value: Any, *, limit_bytes: int = DECISION_BLOCK_FIELD_MAX_BYTES) -> Any:
    """Cap a string at ``limit_bytes`` UTF-8 bytes (suffix included); non-strings pass through (SKY-9779)."""
    if not isinstance(value, str):
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value
    overflow_bytes = len(encoded) - limit_bytes
    suffix = f"…[truncated {overflow_bytes} bytes]"
    suffix_bytes = len(suffix.encode("utf-8"))
    head_budget = max(0, limit_bytes - suffix_bytes)
    return encoded[:head_budget].decode("utf-8", errors="ignore") + suffix


def _trim_branch_evaluations(branch_evaluations: list[dict] | None) -> list[dict] | None:
    """Drop ``rendered_expression`` on non-matched branches; cap the matched one (SKY-9779)."""
    if not branch_evaluations:
        return branch_evaluations
    trimmed: list[dict] = []
    for ev in branch_evaluations:
        if ev.get("is_matched"):
            ev = {**ev, "rendered_expression": _cap_debug_field(ev.get("rendered_expression"))}
        else:
            ev = {k: v for k, v in ev.items() if k != "rendered_expression"}
        trimmed.append(ev)
    return trimmed


class BranchCondition(BaseModel):
    """Represents a single conditional branch edge within a ConditionalBlock."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    criteria: BranchCriteriaTypeVar | None = None
    next_block_label: str | None = None
    description: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def validate_condition(self) -> BranchCondition:
        if isinstance(self.criteria, dict):
            criteria_type = self.criteria.get("criteria_type")
            if criteria_type is None:
                # Infer criteria type from expression format
                expression = self.criteria.get("expression", "")
                if _is_pure_jinja_expression(expression):
                    criteria_type = "jinja2_template"
                else:
                    criteria_type = "prompt"
            if criteria_type == "prompt":
                self.criteria = PromptBranchCriteria(**self.criteria)
            else:
                self.criteria = JinjaBranchCriteria(**self.criteria)
        if self.criteria is None and not self.is_default:
            raise ValueError("Branches without criteria must be marked as default.")
        if self.criteria is not None and self.is_default:
            raise ValueError("Default branches may not define criteria.")
        if self.criteria and isinstance(self.criteria, BranchCriteria):
            expression = self.criteria.expression
            criteria_dict = self.criteria.model_dump()
            if _is_pure_jinja_expression(expression):
                criteria_dict["criteria_type"] = "jinja2_template"
                self.criteria = JinjaBranchCriteria(**criteria_dict)
            else:
                criteria_dict["criteria_type"] = "prompt"
                self.criteria = PromptBranchCriteria(**criteria_dict)
        return self


async def _evaluate_prompt_branch_conditions_batch(
    *,
    log_label: str,
    branches: list[BranchCondition],
    evaluation_context: BranchEvaluationContext,
    workflow_run_id: str,
    workflow_run_block_id: str,
    organization_id: str | None,
    browser_session_id: str | None,
    workflow_id: str,
    extraction_description_suffix: str = "",
) -> tuple[list[bool], list[str], str | None, dict | None]:
    # Late import: block.py is the facade over this leaf module, so pull ExtractionBlock
    # only at call time to keep this module importable on its own.
    from skyvern.forge.sdk.workflow.models.block import ExtractionBlock

    if organization_id is None:
        raise ValueError("organization_id is required to evaluate natural language branches")

    if not branches:
        return ([], [], None, None)

    workflow_run_context = evaluation_context.workflow_run_context

    rendered_expressions: list[str] = []
    has_any_pure_natlang = False

    for idx, branch in enumerate(branches):
        expression = branch.criteria.expression if branch.criteria else ""
        has_jinja = "{{" in expression

        if has_jinja:
            try:
                rendered_expression = (
                    evaluation_context.template_renderer(expression)
                    if evaluation_context.template_renderer
                    else expression
                )
            except Exception as render_exc:
                LOG.error(
                    "Conditional branch expression rendering FAILED",
                    block_label=log_label,
                    branch_index=idx,
                    original_expression=expression,
                    error=str(render_exc),
                    exc_info=True,
                )
                rendered_expression = expression
                has_any_pure_natlang = True
            else:
                rendered_expression, was_patched = _make_empty_params_explicit(expression, rendered_expression)
                if was_patched:
                    LOG.info(
                        "Conditional branch expression patched for empty parameter(s)",
                        workflow_run_id=workflow_run_id,
                        block_label=log_label,
                        branch_index=idx,
                        original_expression=expression,
                        patched_expression=rendered_expression,
                    )
        else:
            rendered_expression = expression
            has_any_pure_natlang = True

        LOG.info(
            "Conditional branch expression rendering",
            block_label=log_label,
            branch_index=idx,
            original_expression=expression,
            rendered_expression=rendered_expression,
            has_jinja=has_jinja,
            expression_changed=expression != rendered_expression,
        )

        rendered_expressions.append(rendered_expression)

    if has_any_pure_natlang:
        context_snapshot = evaluation_context.build_llm_safe_context_snapshot()
        context_json = json.dumps(context_snapshot, default=str)
    else:
        context_json = None

    extraction_goal = prompt_engine.load_prompt(
        "conditional-prompt-branch-evaluation",
        conditions=rendered_expressions,
        context_json=context_json,
    )

    data_schema = _build_branch_evaluation_schema(len(branches))

    desc_suffix = extraction_description_suffix or f"{len(branches)} conditions"

    last_malformed: MalformedBranchEvaluationError | None = None
    for attempt in range(MAX_PROMPT_BRANCH_EVAL_ATTEMPTS):
        # Vary the goal on retries so the extraction cache key (which includes
        # data_extraction_goal) changes and we get a genuine re-roll instead of replaying
        # the malformed cached result that just failed validation.
        attempt_goal = extraction_goal
        if attempt > 0:
            attempt_goal = (
                f"{extraction_goal}\n\n"
                f"Re-evaluation attempt {attempt + 1}: return exactly {len(branches)} results, "
                f"one object per numbered condition, each tagged with its condition_index."
            )

        prompt_branch_eval_id = generate_random_string()
        output_param = OutputParameter(
            output_parameter_id=str(uuid.uuid4()),
            key=f"prompt_branch_eval_{prompt_branch_eval_id}",
            workflow_id=workflow_id,
            created_at=datetime.now(),
            modified_at=datetime.now(),
            parameter_type=ParameterType.OUTPUT,
            description=f"Conditional branch evaluation results ({desc_suffix})",
        )
        extraction_block = ExtractionBlock(
            label=f"prompt_branch_eval_{prompt_branch_eval_id}",
            data_extraction_goal=attempt_goal,
            data_schema=data_schema,
            output_parameter=output_param,
        )

        LOG.info(
            "Conditional branch ExtractionBlock created (batched)",
            block_label=log_label,
            prompt_branch_eval_id=prompt_branch_eval_id,
            num_conditions=len(branches),
            attempt=attempt,
            extraction_goal_preview=attempt_goal[:500] if attempt_goal else None,
            has_browser_session=browser_session_id is not None,
            has_any_pure_natlang=has_any_pure_natlang,
            has_context=context_json is not None,
        )

        extraction_result = await extraction_block.execute(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )

        if not extraction_result.success:
            # Extraction-level failures already retry at the step level inside the task;
            # surface them immediately rather than re-rolling the whole batch.
            LOG.error(
                "Conditional branch ExtractionBlock failed",
                block_label=log_label,
                failure_reason=extraction_result.failure_reason,
            )
            raise ConditionalBranchEvaluationError(
                f"Branch evaluation failed: "
                f"{extraction_result.failure_reason or 'Unknown error (no failure reason provided)'}"
            )

        raw_output_value = extraction_result.output_parameter_value
        try:
            results_array, llm_rendered_expressions, normalized_output = _align_branch_evaluations(
                output_value=raw_output_value,
                branches=branches,
                rendered_expressions=rendered_expressions,
            )
        except MalformedBranchEvaluationError as malformed:
            last_malformed = malformed
            LOG.warning(
                "Conditional branch evaluation output malformed",
                block_label=log_label,
                attempt=attempt,
                will_retry=attempt + 1 < MAX_PROMPT_BRANCH_EVAL_ATTEMPTS,
                error=str(malformed),
                raw_output=raw_output_value,
            )
            continue

        # Record the output parameter only for the attempt we actually accept, so a failed
        # attempt's payload never lands in the workflow context when a later attempt succeeds.
        if workflow_run_context:
            try:
                await extraction_block.record_output_parameter_value(
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    value=normalized_output,
                )
            except Exception:
                LOG.warning(
                    "Failed to record conditional branch evaluation output",
                    workflow_run_id=workflow_run_id,
                    block_label=log_label,
                    exc_info=True,
                )

        LOG.info(
            "Conditional branch evaluation results",
            block_label=log_label,
            results=results_array,
            llm_rendered_expressions=llm_rendered_expressions,
            attempt=attempt,
            raw_output=normalized_output,
        )
        return (results_array, llm_rendered_expressions, extraction_goal, normalized_output)

    LOG.error(
        "Conditional branch evaluation failed after retries",
        block_label=log_label,
        attempts=MAX_PROMPT_BRANCH_EVAL_ATTEMPTS,
        error=str(last_malformed),
    )
    raise ConditionalBranchEvaluationError(f"Conditional branch evaluation failed: {last_malformed}")


BranchCriteriaSubclasses = Union[JinjaBranchCriteria, PromptBranchCriteria]
BranchCriteriaTypeVar = Annotated[BranchCriteriaSubclasses, Field(discriminator="criteria_type")]
