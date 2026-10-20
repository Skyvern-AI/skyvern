"""Which characters of a rendered block template came from a web page rather than the workflow author.

The primary render is never touched. A second render of the same raw template, with an immutable twin of the
same environment and the same ``template_data``, wraps every output expression that reads a page-derived name in
sentinels drawn fresh for that render, so no page value can contain them; it is accepted only when stripping the
sentinels gives back the primary string byte for byte, and otherwise the field falls back to whole-field treatment
(``unmarked``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from jinja2 import meta, nodes
from jinja2.runtime import Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment, SandboxedEnvironment

from skyvern.forge.sdk.workflow.models.parameter import ContextParameter, OutputParameter, WorkflowParameter

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext

# Prefixes of the per-render sentinels; each render appends its own random token.
OPEN = "\ue010"
CLOSE = "\ue011"
# Injected into the marked render's context only; the context key wins over any same-named template value.
_MARK = "skyvern_page_derived_mark"

RUN_META_ROOTS = frozenset(
    "workflow_title workflow_id workflow_permanent_id workflow_run_id current_date browser_session_id current_index".split()
)
RUN_OUTPUT_ROOTS = frozenset({"workflow_run_outputs", "workflow_run_summary"})
LOOP_VALUE_ROOTS = frozenset({"current_value", "current_item"})
_UNSUPPORTED_NODES = (
    nodes.Macro,
    nodes.CallBlock,
    nodes.Include,
    nodes.Import,
    nodes.FromImport,
    nodes.Extends,
    nodes.Block,
    nodes.FilterBlock,
    nodes.ScopedEvalContextModifier,
)
# Without taint propagation a name bound from a page value would render unmarked, so these fail closed.
_BINDING_NODES = (nodes.Assign, nodes.AssignBlock, nodes.For, nodes.With)

_IMMUTABLE_TWINS: dict[SandboxedEnvironment, ImmutableSandboxedEnvironment] = {}

RootClass = Literal["output_key", "block_label", "loop_value", "run_outputs", "unknown", "parent_run_parameter"]
# Roots whose origin is not known to be page or customer: the field gets the unverified-origin qualifier.
UNVERIFIED_ROOT_CLASSES: frozenset[RootClass] = frozenset({"unknown", "parent_run_parameter"})
Status = Literal["none", "marked", "sole", "unmarked"]
# Called after a successful primary render with (env, template_data, primary).
PageDerivedCapture = Callable[[SandboxedEnvironment, dict[str, Any], str], None]


@dataclass(frozen=True)
class PageDerivedRender:
    status: Status
    # ``(is_page, text)`` runs of a checked marked render, whose texts join to the primary string.
    segments: tuple[tuple[bool, str], ...] | None = None
    reason: str | None = None
    root_classes: dict[str, RootClass] = field(default_factory=dict)


# A workflow task goal that never went through the block capture (the script-run AI fallback, prompt-branch
# evaluation, code-block self-heal) may carry page text, so it fails closed.
NO_RENDER_RECORD = PageDerivedRender(status="unmarked", reason="no_render_record")


def _parameter_root_class(parameter: Any, ctx: WorkflowRunContext, in_page_loop: bool) -> RootClass | None:
    """None for a customer-configured parameter."""
    if isinstance(parameter, OutputParameter):
        return "output_key"
    if isinstance(parameter, ContextParameter):
        # A loop copies each iterated item into its children's ContextParameters whatever their source names,
        # and the value stays in the run context after the loop ends.
        if in_page_loop or parameter.key in ctx.page_derived_context_keys:
            return "loop_value"
        return _parameter_root_class(parameter.source, ctx, in_page_loop)
    # A triggered run's workflow parameters carry the parent's payload, which the parent may render from a page.
    if isinstance(parameter, WorkflowParameter) and ctx.parent_workflow_run_id:
        return "parent_run_parameter"
    return None


def classify_roots(
    names: set[str], ctx: WorkflowRunContext, block_label: str | None, env: SandboxedEnvironment
) -> dict[str, RootClass]:
    """The page-derived roots among ``names``, mirroring how ``template_data`` is assembled for a block."""
    metadata = ctx.get_block_metadata(block_label)
    in_page_loop = block_label is not None and block_label in ctx.page_derived_loop_labels
    page: dict[str, RootClass] = {}
    for name in names:
        if name in RUN_OUTPUT_ROOTS:
            page[name] = "run_outputs"
        elif name in LOOP_VALUE_ROOTS and name in metadata:
            if in_page_loop:
                page[name] = "loop_value"
        elif name == block_label:
            if in_page_loop:
                page[name] = "loop_value"
            elif name in ctx.values:
                page[name] = "block_label"
        # Before parameters: a finished block's output overwrites a same-named parameter's value in ctx.values.
        elif name in ctx.workflow_run_outputs or name in ctx.carried_block_labels:
            page[name] = "block_label"
        elif name in ctx.parameters:
            if (root_class := _parameter_root_class(ctx.parameters[name], ctx, in_page_loop)) is not None:
                page[name] = root_class
        elif name in ctx.values:
            page[name] = "output_key" if name.endswith("_output") else "block_label"
        elif name in RUN_META_ROOTS or name in env.globals:
            continue
        else:
            page[name] = "unknown"
    return page


def loop_source_is_page_derived(
    loop_over: Any,
    loop_variable_reference: str | None,
    ctx: WorkflowRunContext,
    loop_label: str,
    env: SandboxedEnvironment,
) -> bool:
    # Same precedence as ForLoopBlock.get_loop_over_parameter_values: the reference wins over loop_over.
    if loop_variable_reference:
        try:
            ast = env.parse("{{ " + loop_variable_reference.strip(" {}") + " }}")
        except Exception:
            return True
        return bool(classify_roots(meta.find_undeclared_variables(ast), ctx, loop_label, env))
    in_page_loop = loop_label in ctx.page_derived_loop_labels
    return loop_over is None or _parameter_root_class(loop_over, ctx, in_page_loop) is not None


def _spans(marked: str, open_: str, close: str) -> tuple[tuple[bool, str], ...]:
    """``(is_page, text)`` segments; raises ValueError unless every sentinel pair is balanced and flat."""
    segments: list[tuple[bool, str]] = []
    rest = marked
    while rest:
        open_at = rest.find(open_)
        close_at = rest.find(close)
        if open_at == -1:
            if close_at != -1:
                raise ValueError("unbalanced")
            segments.append((False, rest))
            break
        if close_at != -1 and close_at < open_at:
            raise ValueError("unbalanced")
        start = open_at + len(open_)
        end = rest.find(close, start)
        if end == -1 or open_ in rest[start:end]:
            raise ValueError("unbalanced or nested")
        if open_at:
            segments.append((False, rest[:open_at]))
        segments.append((True, rest[start:end]))
        rest = rest[end + len(close) :]
    return tuple(segments)


def _reads(node: nodes.Node, names: set[str]) -> bool:
    # find_all yields descendants only, so a bare `{{ name }}` has to be checked on the node itself.
    return any(isinstance(n, nodes.Name) and n.name in names for n in (node, *node.find_all(nodes.Name)))


def _bound_values(node: nodes.Node) -> list[nodes.Node]:
    """What a binding construct assigns from: a name bound from any of these carries its taint."""
    if isinstance(node, nodes.Assign):
        return [node.node]
    if isinstance(node, nodes.AssignBlock):
        return list(node.body)
    if isinstance(node, nodes.For):
        return [node.iter]
    return list(node.values)


def _instrument(ast: nodes.Template, page_roots: set[str]) -> None:
    for output in ast.find_all(nodes.Output):
        output.nodes = [
            child
            if isinstance(child, nodes.TemplateData) or not _reads(child, page_roots)
            else nodes.Call(
                nodes.Name(_MARK, "load", lineno=child.lineno), [child], [], None, None, lineno=child.lineno
            )
            for child in output.nodes
        ]


def _immutable_twin(env: SandboxedEnvironment) -> ImmutableSandboxedEnvironment:
    """``env``'s configuration (filters, globals, undefined, finalize) under a sandbox that refuses list/dict/set
    mutation, so the second render cannot repeat a side effect of the primary on the run's shared values."""
    twin = _IMMUTABLE_TWINS.get(env)
    if twin is None:
        twin = env.overlay(cache_size=0)
        twin.__class__ = ImmutableSandboxedEnvironment
        _IMMUTABLE_TWINS[env] = twin
    return twin


def render_page_derived(
    raw: str,
    primary: str,
    env: SandboxedEnvironment,
    template_data: dict[str, Any],
    ctx: WorkflowRunContext,
    block_label: str | None,
) -> PageDerivedRender:
    """Classify ``raw``'s roots and, when any is page-derived, produce the checked marked render."""
    ast = env.parse(raw)
    root_classes = classify_roots(meta.find_undeclared_variables(ast), ctx, block_label, env)
    if not root_classes:
        return PageDerivedRender(status="none")

    def unmarked(reason: str) -> PageDerivedRender:
        return PageDerivedRender(status="unmarked", reason=reason, root_classes=root_classes)

    # A subclass may override sandbox policy that the immutable twin would silently drop.
    if type(env) is not SandboxedEnvironment:
        return unmarked("unsupported_environment")
    page_roots = set(root_classes)
    if any(True for _ in ast.find_all(_UNSUPPORTED_NODES)) or any(
        _reads(bound, page_roots) for node in ast.find_all(_BINDING_NODES) for bound in _bound_values(node)
    ):
        return unmarked("unsupported_construct")
    # Unpredictable per render, so no page value can carry a sentinel and push its field to the unmarked presentation.
    token = secrets.token_hex(16)
    open_, close = OPEN + token, CLOSE + token
    forged = open_ in primary or close in primary

    def mark(value: Any) -> str:
        nonlocal forged
        if value is None or isinstance(value, (bool, int, float, Undefined)):
            return str(value)
        text = str(value)
        if open_ in text or close in text:
            forged = True
        return open_ + text + close if text else ""

    _instrument(ast, page_roots)
    twin = _immutable_twin(env)
    ast.set_environment(twin)
    try:
        # A mutating call raises SecurityError here, which fails closed below.
        marked = twin.from_string(ast).render({**template_data, _MARK: mark})
    except Exception:
        return unmarked("marked_render_error")
    if forged:
        return unmarked("forged_sentinel")
    try:
        segments = _spans(marked, open_, close)
    except ValueError:
        return unmarked("render_mismatch")
    if "".join(text for _, text in segments) != primary:
        return unmarked("render_mismatch")
    sole = any(is_page for is_page, _ in segments) and all(is_page or not text.strip() for is_page, text in segments)
    return PageDerivedRender(status="sole" if sole else "marked", segments=segments, root_classes=root_classes)
