"""Non-mutating validation for workflow drafts shown by the editor."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any, Literal, get_args

import yaml
from jinja2 import TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ValidationError

from skyvern.forge.sdk.copilot.workflow_yaml import workflow_to_copilot_yaml
from skyvern.forge.sdk.workflow.models._jinja import mask_jinja_in_python_comments
from skyvern.forge.sdk.workflow.models.block import BlockSubclasses
from skyvern.forge.sdk.workflow.models.parameter import RESERVED_PARAMETER_KEYS, is_sensitive_workflow_parameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest


class WorkflowProblem(BaseModel):
    block_label: str | None = None
    path: str
    field: str | None = None
    code: str
    severity: Literal["error", "warning"] = "error"
    message: str


class WorkflowValidationResult(BaseModel):
    valid: bool
    problems: list[WorkflowProblem]


# Bounds the recursive diagnostic walk and stored-workflow conversion.
MAX_WORKFLOW_VALIDATION_LOOP_DEPTH = 6
MAX_WORKFLOW_VALIDATION_BLOCK_COUNT = 500
MAX_WORKFLOW_VALIDATION_PROBLEMS = 100
# shortcut: 10,000 AST visits/bindings per template; raise if valid workflows need more work.
MAX_TEMPLATE_VALIDATION_VISITS = 10_000


class WorkflowValidationTooComplex(ValueError):
    pass


class TemplateValidationTooComplex(ValueError):
    pass


_JINJA_ENV = SandboxedEnvironment()
_BARE_REFERENCE = re.compile(r"([A-Za-z_]\w*)(?:\.\w+|\[\d+\])*")
_LOOP_CONTEXT_VARIABLES = frozenset({"current_item", "current_value", "current_index"})
_BRANCH_CRITERIA_VARIABLES = frozenset({"env", "environment", "llm", "outputs", "params"})
_REQUIRED_GOAL_FIELDS = {
    "navigation": frozenset({"navigation_goal"}),
    "extraction": frozenset({"data_extraction_goal"}),
    "file_download": frozenset({"navigation_goal"}),
}
_BLOCK_TEMPLATABLE_FIELDS = {
    block.model_fields["block_type"].default.value: block.templatable_fields() for block in get_args(BlockSubclasses)
}
# Ordinary block template context plus Jinja globals; branch-only aliases are handled separately below.
_RUNTIME_VARIABLES = (
    (frozenset(RESERVED_PARAMETER_KEYS) - _LOOP_CONTEXT_VARIABLES)
    | frozenset(_JINJA_ENV.globals)
    | frozenset(
        {
            "browser_session_id",
            "false",
            "none",
            "true",
        }
    )
)


def _undeclared_names(template: nodes.Template) -> set[str]:
    # jinja2.meta.find_undeclared_variables runs the compiler's constant folding, which is cubic in
    # expression depth (a 300-term sum takes seconds); walking the parsed names is linear.
    undeclared: set[str] = set()
    macros = {macro.name: macro for macro in template.find_all(nodes.Macro)}
    active_macros: set[str] = set()
    visits = 0

    def charge() -> None:
        nonlocal visits
        visits += 1
        if visits > MAX_TEMPLATE_VALIDATION_VISITS:
            raise TemplateValidationTooComplex

    def stored_names(target: nodes.Node) -> set[str]:
        names: set[str] = set()
        pending = [target]
        while pending:
            node = pending.pop()
            charge()
            if isinstance(node, nodes.Name):
                names.add(node.name)
            else:
                pending.extend(node.iter_child_nodes())
        return names

    def conditional_scopes(node: nodes.If, scope: set[str]) -> list[set[str]]:
        visit(node.test, set(scope))
        body_scope = set(scope)
        for child in node.body:
            visit(child, body_scope)
        scopes = [body_scope]
        for branch in node.elif_:
            scopes.extend(conditional_scopes(branch, scope))
        return scopes

    def visit(node: nodes.Node, scope: set[str]) -> None:
        charge()
        if isinstance(node, nodes.Name):
            if node.ctx == "load":
                if node.name not in scope:
                    undeclared.add(node.name)
            else:
                scope.add(node.name)
        elif isinstance(node, nodes.For):
            visit(node.iter, scope)
            target_names = stored_names(node.target)
            if node.test is not None:
                visit(node.test, scope | target_names | {"loop"})
            body_scope = scope | target_names | {"loop"}
            for child in node.body:
                visit(child, body_scope)
            else_scope = set(scope)
            for child in node.else_:
                visit(child, else_scope)
        elif isinstance(node, nodes.Assign):
            visit(node.node, scope)
            visit(node.target, scope)
        elif isinstance(node, nodes.AssignBlock):
            for child in node.body:
                visit(child, set(scope))
            visit(node.target, scope)
        elif isinstance(node, nodes.If):
            branch_scopes = conditional_scopes(node, scope)
            if node.else_:
                else_scope = set(scope)
                for child in node.else_:
                    visit(child, else_scope)
                branch_scopes.append(else_scope)
            else:
                branch_scopes.append(set(scope))
            scope.update(set.intersection(*branch_scopes))
        elif isinstance(node, nodes.Macro):
            # Jinja resolves a macro's free names when called, using the caller's current scope.
            scope.add(node.name)
        elif isinstance(node, nodes.Call):
            visit(node.node, scope)
            for child in (*node.args, *node.kwargs, node.dyn_args, node.dyn_kwargs):
                if child is not None:
                    visit(child, scope)
            if isinstance(node.node, nodes.Name) and node.node.name in macros:
                macro = macros[node.node.name]
                if macro.name not in active_macros:
                    inner = set(scope)
                    for arg in macro.args:
                        charge()
                        inner.add(arg.name)
                    active_macros.add(macro.name)
                    try:
                        for default in macro.defaults:
                            visit(default, inner)
                        for child in macro.body:
                            visit(child, inner)
                    finally:
                        active_macros.remove(macro.name)
        elif isinstance(node, nodes.CallBlock):
            visit(node.call, scope)
            inner = set(scope)
            for arg in node.args:
                charge()
                inner.add(arg.name)
            inner.update({"caller", "varargs", "kwargs"})
            for child in node.body:
                visit(child, inner)
        elif isinstance(node, nodes.With):
            inner = set(scope)
            for value in node.values:
                visit(value, scope)
            for target in node.targets:
                inner.update(stored_names(target))
            for child in node.body:
                visit(child, inner)
        else:
            for child in node.iter_child_nodes():
                visit(child, scope)

    visit(template, set())
    return undeclared


def _result(problems: list[WorkflowProblem]) -> WorkflowValidationResult:
    if len(problems) > MAX_WORKFLOW_VALIDATION_PROBLEMS:
        problems = problems[: MAX_WORKFLOW_VALIDATION_PROBLEMS - 1] + [
            WorkflowProblem(
                path="workflow_definition",
                code="too_many_problems",
                severity="error",
                message="The workflow has more validation problems than can be displayed.",
            )
        ]
    return WorkflowValidationResult(
        valid=not any(problem.severity == "error" for problem in problems),
        problems=problems,
    )


def ensure_workflow_validation_limits(raw: dict[str, Any] | Workflow) -> None:
    blocks: Sequence[object] | None
    if isinstance(raw, Workflow):
        blocks = raw.workflow_definition.blocks
    else:
        definition = raw.get("workflow_definition")
        blocks = definition.get("blocks") if isinstance(definition, dict) else None
    if not isinstance(blocks, list):
        return

    pending: list[tuple[Sequence[object], int]] = [(blocks, 0)]
    count = 0
    while pending:
        items, depth = pending.pop()
        for block in items:
            count += 1
            if count > MAX_WORKFLOW_VALIDATION_BLOCK_COUNT:
                raise WorkflowValidationTooComplex(
                    f"Workflow validation supports at most {MAX_WORKFLOW_VALIDATION_BLOCK_COUNT} blocks."
                )
            nested = block.get("loop_blocks") if isinstance(block, dict) else getattr(block, "loop_blocks", None)
            if isinstance(nested, list):
                nested_depth = depth + 1
                if nested_depth > MAX_WORKFLOW_VALIDATION_LOOP_DEPTH:
                    raise WorkflowValidationTooComplex(
                        "Workflow validation supports nested block lists up to "
                        f"{MAX_WORKFLOW_VALIDATION_LOOP_DEPTH} levels."
                    )
                pending.append((nested, nested_depth))


def _walk_strings(value: object, path: str = "") -> Iterator[tuple[str, str, str | None]]:
    if isinstance(value, str):
        field = path.rsplit(".", 1)[-1] if path else None
        yield path, value, field
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk_strings(item, child_path)


def _block_template_strings(block: dict[str, Any], path: str) -> Iterator[tuple[str, str, str | None]]:
    block_type = block.get("block_type")
    fields = _BLOCK_TEMPLATABLE_FIELDS.get(block_type, ()) if isinstance(block_type, str) else ()
    for field in fields:
        if field not in block:
            continue
        value = block[field]
        if block_type == "code" and field == "code" and isinstance(value, str):
            value, _ = mask_jinja_in_python_comments(value)
        yield from _walk_strings(value, f"{path}.{field}")

    for field in ("notes", "trailing_notes"):
        if field in block:
            yield from _walk_strings(block[field], f"{path}.{field}")

    if block_type == "conditional" and isinstance(branches := block.get("branch_conditions"), list):
        for index, branch in enumerate(branches):
            criteria = branch.get("criteria") if isinstance(branch, dict) else None
            expression = criteria.get("expression") if isinstance(criteria, dict) else None
            if isinstance(expression, str):
                yield from _walk_strings(
                    expression,
                    f"{path}.branch_conditions[{index}].criteria.expression",
                )
    if block_type == "while_loop" and isinstance(condition := block.get("condition"), dict):
        expression = condition.get("expression")
        if isinstance(expression, str):
            yield from _walk_strings(expression, f"{path}.condition.expression")


def _block_label_for_path(definition: dict[str, Any], path: str) -> str | None:
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return None
    # Pydantic locations use dotted indexes (blocks.0.x); walked paths use blocks[0].x.
    rest = re.sub(r"\.(\d+)(?=\.|$)", r"[\1]", path)
    label: str | None = None
    # Pydantic puts the union tag between levels: blocks[0].for_loop.loop_blocks[1].
    key = "blocks"
    while (match := re.match(rf"{key}\[(\d+)\]", rest)) and int(match.group(1)) < len(blocks):
        block = blocks[int(match.group(1))]
        if not isinstance(block, dict):
            break
        label = block.get("label") if isinstance(block.get("label"), str) else None
        rest = rest[match.end() :].removeprefix(".")
        blocks = block.get("loop_blocks")
        if not isinstance(blocks, list):
            break
        key = r"(?:\w+\.)?loop_blocks"
    return label


def _branch_reach(items: list[object]) -> dict[str, tuple[list[set[str]], set[str]]]:
    """For each conditional in one block list, the labels each branch reaches before the merge, and every
    label that runs after the conditional (branches and what follows the merge).
    Mirrors labels_only_on_untaken_branches: a nested conditional reaches all of its targets."""
    by_label = {
        block["label"]: block for block in items if isinstance(block, dict) and isinstance(block.get("label"), str)
    }

    def branch_targets(block: dict[str, Any]) -> list[str | None]:
        branches = block.get("branch_conditions")
        return [
            branch.get("next_block_label") if isinstance(branch.get("next_block_label"), str) else None
            for branch in (branches if isinstance(branches, list) else [])
            if isinstance(branch, dict)
        ]

    def reach(start: str | None, stop: str | None) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            label = pending.pop()
            if label is None or label == stop or label in seen or label not in by_label:
                continue
            seen.add(label)
            block = by_label[label]
            # A loop's children keep their last output after it, so a branch that runs the loop produces them too.
            if isinstance(nested := block.get("loop_blocks"), list):
                seen.update(_all_labels(nested))
            if block.get("block_type") == "conditional":
                pending.extend(branch_targets(block))
            following = block.get("next_block_label")
            pending.append(following if isinstance(following, str) and following else None)
        return seen

    result: dict[str, tuple[list[set[str]], set[str]]] = {}
    for label, block in by_label.items():
        if block.get("block_type") != "conditional":
            continue
        successor = block.get("next_block_label") if isinstance(block.get("next_block_label"), str) else None
        reached = [reach(target or successor, successor) for target in branch_targets(block)]
        if len(reached) >= 2:
            result[label] = (reached, set().union(*reached) | reach(successor, None))
    return result


def _all_labels(blocks: list[object]) -> set[str]:
    labels: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if isinstance(label := block.get("label"), str):
            labels.add(label)
        nested_blocks = block.get("loop_blocks")
        if isinstance(nested_blocks, list):
            labels.update(_all_labels(nested_blocks))
    return labels


def _schema_problems(raw: dict[str, Any]) -> list[WorkflowProblem]:
    try:
        WorkflowCreateYAMLRequest.model_validate(raw)
    except ValidationError as exc:
        problems: list[WorkflowProblem] = []
        for error in exc.errors():
            if "credential" in error["msg"].lower() and "note" in error["msg"].lower():
                continue
            location = ".".join(str(part) for part in error["loc"])
            definition = raw.get("workflow_definition")
            block_label = (
                _block_label_for_path(definition, location.removeprefix("workflow_definition."))
                if isinstance(definition, dict)
                else None
            )
            problems.append(
                WorkflowProblem(
                    block_label=block_label,
                    path=location,
                    field=str(error["loc"][-1]) if error["loc"] else None,
                    code="schema_error",
                    message=error["msg"],
                )
            )
        return problems
    except Exception as exc:  # noqa: BLE001 - some Pydantic before validators leak input errors directly.
        return [
            WorkflowProblem(
                path="workflow_definition",
                code="schema_error",
                message=str(exc),
            )
        ]
    return []


def _bare_loop_source_problem(
    source: str, scope: set[str], labels: set[str]
) -> tuple[str, Literal["error", "warning"], str] | None:
    # A bare source the run context cannot resolve is sent to an extraction step as an instruction (block.py).
    match = _BARE_REFERENCE.fullmatch(source.strip())
    if not match:
        return None
    root = match.group(1)
    if root in scope or root in _RUNTIME_VARIABLES:
        return None
    if root.endswith("_output") and root.removesuffix("_output") in labels:
        return "forward_reference", "error", f"{root} is produced by a later step."
    return (
        "unknown_loop_source",
        "warning",
        f"{root} is not an input or an earlier step's output, so the loop will treat it as an instruction.",
    )


def validate_workflow_definition(raw: dict[str, Any]) -> WorkflowValidationResult:
    """Return editor-friendly diagnostics without changing the supplied draft."""
    ensure_workflow_validation_limits(raw)
    problems = _schema_problems(raw)
    definition = raw.get("workflow_definition")
    if not isinstance(definition, dict):
        return _result(problems)

    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return _result(problems)
    parameters = definition.get("parameters")
    parameter_items = parameters if isinstance(parameters, list) else []
    parameter_keys = {
        str(item["key"]) for item in parameter_items if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    credential_keys = {
        str(item["key"])
        for item in parameter_items
        if isinstance(item, dict) and isinstance(item.get("key"), str) and is_sensitive_workflow_parameter(item)
    }
    seen_keys: set[str] = set()
    for index, item in enumerate(parameter_items):
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str):
            continue
        if key in seen_keys:
            problems.append(
                WorkflowProblem(
                    path=f"workflow_definition.parameters[{index}].key",
                    field="key",
                    code="duplicate_parameter_key",
                    message=f"Another input already uses the key {key}.",
                )
            )
        seen_keys.add(key)
    labels = _all_labels(blocks)
    available = set(parameter_keys)

    def check_template(
        path: str,
        value: str,
        field: str | None,
        label: str | None,
        scope: set[str],
        reaches: list[tuple[str | None, dict[str, tuple[list[set[str]], set[str]]]]],
        criteria: bool = False,
    ) -> None:
        if "{{" not in value and "{%" not in value:
            return
        try:
            names = _undeclared_names(_JINJA_ENV.parse(value))
        except RecursionError:
            problems.append(
                WorkflowProblem(
                    block_label=label,
                    path=path,
                    field=field,
                    code="template_too_complex",
                    message="This expression is nested too deeply to check.",
                )
            )
            return
        except TemplateValidationTooComplex:
            problems.append(
                WorkflowProblem(
                    block_label=label,
                    path=path,
                    field=field,
                    code="validation_limit",
                    message="This template is too complex to validate.",
                )
            )
            return
        except TemplateSyntaxError as exc:
            if field in {"notes", "trailing_notes"}:
                return
            problems.append(
                WorkflowProblem(
                    block_label=label,
                    path=path,
                    field=field,
                    code="template_syntax",
                    message=str(exc),
                )
            )
            return
        for name in sorted(names):
            if name in credential_keys and field in {"notes", "trailing_notes"}:
                code, message = "credential_in_note", "Notes can't use a credential input."
            # BranchEvaluationContext alone exposes these aliases.
            elif (
                field in {"notes", "trailing_notes"}
                or name in _RUNTIME_VARIABLES
                or (name in _BRANCH_CRITERIA_VARIABLES and (criteria or ".branch_conditions[" in path))
            ):
                continue
            elif name in scope:
                source = name.removesuffix("_output") if name.endswith("_output") else name
                # Inside a loop, the step sits wherever its enclosing loop sits in the outer list.
                conditional = next(
                    (
                        conditional_label
                        for position, branch_reach in reaches
                        for conditional_label, (reached, downstream) in branch_reach.items()
                        if position in downstream
                        and any(source in branch for branch in reached)
                        and any(source not in branch for branch in ([b for b in reached if position in b] or reached))
                    ),
                    None,
                )
                if conditional is None:
                    continue
                code, message = (
                    "branch_only_reference",
                    f"{name} is only produced on some branches of {conditional}, so it may be empty here.",
                )
            elif name in labels or (name.endswith("_output") and name.removesuffix("_output") in labels):
                code, message = "forward_reference", f"{name} is produced by a later step."
            else:
                code, message = "unknown_reference", f"Unknown reference: {name}."
            problems.append(
                WorkflowProblem(
                    block_label=label,
                    path=path,
                    field=field,
                    code=code,
                    severity="warning" if code in {"credential_in_note", "branch_only_reference"} else "error",
                    message=message,
                )
            )

    workflow_system_prompt = definition.get("workflow_system_prompt")
    if isinstance(workflow_system_prompt, str):
        check_template(
            "workflow_definition.workflow_system_prompt",
            workflow_system_prompt,
            "workflow_system_prompt",
            None,
            available,
            [],
        )

    for field in ("browser_profile_key", "sequential_key"):
        value = raw.get(field)
        if isinstance(value, str):
            check_template(field, value, field, None, available, [])

    def validate_blocks(
        items: list[object],
        path_prefix: str,
        scope: set[str],
        outer_reaches: list[tuple[str | None, dict[str, tuple[list[set[str]], set[str]]]]],
    ) -> None:
        branch_reach = _branch_reach(items)
        for index, block in enumerate(items):
            if not isinstance(block, dict):
                continue
            label = block.get("label") if isinstance(block.get("label"), str) else None
            block_path = f"{path_prefix}[{index}]"
            # A block's own label carries its loop metadata, such as outer_loop.current_value.
            own_scope = scope | ({label} if label else set())
            # The schema check reports a non-string type; it must not reach the hashed lookups below.
            block_type = block.get("block_type") if isinstance(block.get("block_type"), str) else None
            if block_type == "while_loop":
                own_scope |= _LOOP_CONTEXT_VARIABLES
            condition_path = f"{block_path}.condition.expression"
            # The condition runs before each iteration, so after the first it reads the previous iteration's body.
            body = block.get("loop_blocks")
            condition_scope = set(own_scope)
            if block_type == "while_loop" and isinstance(body, list):
                for nested_label in _all_labels(body):
                    condition_scope.update({nested_label, f"{nested_label}_output"})
            for path, value, field in _block_template_strings(
                block,
                block_path.removeprefix("workflow_definition."),
            ):
                template_path = f"workflow_definition.{path}"
                check_template(
                    template_path,
                    value,
                    field,
                    label,
                    condition_scope if template_path == condition_path else own_scope,
                    [(label, branch_reach), *outer_reaches],
                    criteria=template_path == condition_path,
                )
            for field in ("parameter_keys", "loop_over_parameter_key"):
                references = block.get(field)
                keys = references if isinstance(references, list) else [references]
                for reference_index, key in enumerate(keys):
                    if not isinstance(key, str) or not key:
                        continue
                    if key in parameter_keys or (key.endswith("_output") and key in scope):
                        continue
                    suffix = f"[{reference_index}]" if field == "parameter_keys" else ""
                    problems.append(
                        WorkflowProblem(
                            block_label=label,
                            path=f"{block_path}.{field}{suffix}",
                            field=field,
                            code="unknown_reference",
                            message=f"Unknown parameter: {key}.",
                        )
                    )
            required_goal_fields = _REQUIRED_GOAL_FIELDS.get(block_type, ()) if isinstance(block_type, str) else ()
            for field in required_goal_fields:
                if field in block and isinstance(block[field], str) and not block[field].strip():
                    problems.append(
                        WorkflowProblem(
                            block_label=label,
                            path=f"{block_path}.{field}",
                            field=field,
                            code="empty_goal",
                            message="This goal cannot be empty.",
                        )
                    )
            target = block.get("next_block_label")
            if isinstance(target, str) and target and target not in labels:
                problems.append(
                    WorkflowProblem(
                        block_label=label,
                        path=f"{block_path}.next_block_label",
                        field="next_block_label",
                        code="bad_jump_target",
                        message=f"Jump target {target} does not exist.",
                    )
                )
            branches = block.get("branch_conditions")
            # A schema-invalid draft can carry any value here; the schema check already reported it.
            for branch_index, branch in enumerate(branches if isinstance(branches, list) else []):
                if not isinstance(branch, dict):
                    continue
                criteria = branch.get("criteria")
                if isinstance(criteria, dict) and not str(criteria.get("expression", "")).strip():
                    problems.append(
                        WorkflowProblem(
                            block_label=label,
                            path=f"{block_path}.branch_conditions[{branch_index}].criteria.expression",
                            field="expression",
                            code="empty_condition",
                            message="A branch condition cannot be empty.",
                        )
                    )
                branch_target = branch.get("next_block_label")
                if isinstance(branch_target, str) and branch_target and branch_target not in labels:
                    problems.append(
                        WorkflowProblem(
                            block_label=label,
                            path=f"{block_path}.branch_conditions[{branch_index}].next_block_label",
                            field="next_block_label",
                            code="bad_jump_target",
                            message=f"Jump target {branch_target} does not exist.",
                        )
                    )
            loop_source = block.get("loop_variable_reference")
            if isinstance(loop_source, str) and "{{" not in loop_source:
                problem = _bare_loop_source_problem(loop_source, scope, labels)
                if problem:
                    code, severity, message = problem
                    problems.append(
                        WorkflowProblem(
                            block_label=label,
                            path=f"{block_path}.loop_variable_reference",
                            field="loop_variable_reference",
                            code=code,
                            severity=severity,
                            message=message,
                        )
                    )
            nested_blocks = block.get("loop_blocks")
            if isinstance(nested_blocks, list):
                # Children read the item through current_item/current_value/current_index, never through the source.
                loop_scope = (
                    own_scope | _LOOP_CONTEXT_VARIABLES if block_type in {"for_loop", "while_loop"} else own_scope
                )
                validate_blocks(
                    nested_blocks,
                    f"{block_path}.loop_blocks",
                    set(loop_scope),
                    [(label, branch_reach), *outer_reaches],
                )
                # The run context keeps each loop child's last output, so later steps may read it.
                for nested_label in _all_labels(nested_blocks):
                    scope.update({nested_label, f"{nested_label}_output"})
            # A loop's own output exists only once the loop has finished, not inside its body. The run
            # registers each output under the bare label too (register_block_reference_variable_from_output_parameter).
            if label:
                scope.update({label, f"{label}_output"})

    validate_blocks(blocks, "workflow_definition.blocks", available, [])

    return _result(problems)


def validate_stored_workflow(workflow: Workflow) -> WorkflowValidationResult:
    """Validate a stored workflow in the same YAML shape the editor and Copilot author."""
    ensure_workflow_validation_limits(workflow)
    authored = yaml.safe_load(workflow_to_copilot_yaml(workflow))
    return validate_workflow_definition(authored if isinstance(authored, dict) else {})
