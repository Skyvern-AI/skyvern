import asyncio
from functools import wraps
from typing import Any, Callable, Literal

from opentelemetry import trace

SpanRole = Literal["wrapper"]

# Context fields to auto-attach to every span. Deliberately minimal — each
# attribute is paid for in storage and index cardinality, so only IDs we
# actively query on during profiling / Milestone 2 aggregations belong here.
#
# - workflow_permanent_id: profile a customer's workflow across all runs
#   (stable identity — survives workflow edits)
# - workflow_id: mutable version ID — answer "did a workflow edit regress
#   latency?" by grouping per-version within a single workflow_permanent_id
# - workflow_run_id: scope a single run
# - organization_id: segment by customer / tier
# - task_id: drill down to a specific slow task
# - step_id: identify which step of a task dominates
#
# Intentionally excluded (add back only with a specific query use case):
#   - request_id: unique per HTTP request, high-cardinality noise
#   - run_id, task_v2_id, root_workflow_run_id: redundant with above in practice
#   - browser_session_id: sessions-pool concerns are Milestone 4+
_CONTEXT_SPAN_ATTRS: tuple[str, ...] = (
    "workflow_permanent_id",
    "workflow_id",
    "workflow_run_id",
    "organization_id",
    "task_id",
    "step_id",
)


def apply_context_attrs(span: Any) -> None:
    """Copy non-None IDs from the active SkyvernContext onto the current span.

    Imported lazily to avoid an import cycle with any module that imports
    `@traced` during skyvern_context's own load path.
    """
    try:
        from skyvern.forge.sdk.core import skyvern_context

        ctx = skyvern_context.current()
    except Exception:
        # stdlib logging to avoid circular import with structlog (which may
        # import modules that use @traced during its own initialization).
        import logging

        logging.getLogger("skyvern.trace").debug("SkyvernContext unavailable for span attrs", exc_info=True)
        return
    if ctx is None:
        return
    for attr in _CONTEXT_SPAN_ATTRS:
        value = getattr(ctx, attr, None)
        if value:
            span.set_attribute(attr, str(value))


def traced(
    name: str | None = None,
    tags: list[str] | None = None,
    role: SpanRole | None = None,
) -> Callable:
    """Decorator that creates an OTEL span. No-op without SDK installed.

    Every span is tagged with:
    - `code.function` (Python qualname, e.g. `ForgeAgent.agent_step`) and
      `code.namespace` (module, e.g. `skyvern.forge.agent`) so the underlying
      code location stays queryable even when the span's human-readable
      `name` diverges from the method it measures. See OTEL semantic
      conventions: https://opentelemetry.io/docs/specs/semconv/code/.
    - Selected non-None IDs from the active `SkyvernContext`:
      `workflow_permanent_id`, `workflow_id`, `workflow_run_id`,
      `organization_id`, `task_id`, and `step_id`. This makes every span
      queryable by workflow/task/org without per-call-site work.

    Args:
        name: Span name. If not provided, uses func.__qualname__.
        tags: Tags to add as a span attribute.
        role: Optional span role. Set to "wrapper" on spans whose duration is
            dominated by the work of their children (e.g. `agent.step`,
            `workflow.execute`). Dashboards filter these out with
            `skyvern.span.role != 'wrapper'` so leaf-time composition (pie,
            stacked bar) isn't double-counted via nesting.
    """

    def decorator(func: Callable) -> Callable:
        span_name = name or func.__qualname__
        code_function = func.__qualname__
        code_namespace = func.__module__

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kw: Any) -> Any:
                with trace.get_tracer("skyvern").start_as_current_span(span_name) as span:
                    span.set_attribute("code.function", code_function)
                    span.set_attribute("code.namespace", code_namespace)
                    if role is not None:
                        span.set_attribute("skyvern.span.role", role)
                    apply_context_attrs(span)
                    if tags:
                        span.set_attribute("tags", tags)
                    try:
                        return await func(*args, **kw)
                    except Exception as e:
                        span.record_exception(e)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                        raise

            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args: Any, **kw: Any) -> Any:
                with trace.get_tracer("skyvern").start_as_current_span(span_name) as span:
                    span.set_attribute("code.function", code_function)
                    span.set_attribute("code.namespace", code_namespace)
                    if role is not None:
                        span.set_attribute("skyvern.span.role", role)
                    apply_context_attrs(span)
                    if tags:
                        span.set_attribute("tags", tags)
                    try:
                        return func(*args, **kw)
                    except Exception as e:
                        span.record_exception(e)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                        raise

            return sync_wrapper

    return decorator
