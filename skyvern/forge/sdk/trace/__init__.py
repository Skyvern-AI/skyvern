import asyncio
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Literal

from opentelemetry import trace

SpanRole = Literal["wrapper"]

VerificationTrigger = Literal["periodic_after_step", "complete_action_forced"]


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


def record_span_exception(span: Any, exc: BaseException, *, set_error_status: bool = True) -> None:
    """Record an exception on a span with credential values scrubbed from its text.

    Prefer this over ``span.record_exception`` everywhere: span events are exported with no
    logging processors in front of them, so ``str(exc)`` and the traceback reach the collector
    verbatim.
    """
    # Lazy: this module is imported far earlier in boot than the copilot package.
    from skyvern.forge.sdk.copilot.secret_scrub import scrub_all_registered_from_text  # noqa: PLC0415
    from skyvern.forge.sdk.forge_log import current_codeblock_log_redactor  # noqa: PLC0415

    try:
        exc_type = type(exc)
        module = type.__getattribute__(exc_type, "__module__")
        qualname = type.__getattribute__(exc_type, "__qualname__")
        if type(module) is not str or type(qualname) is not str:
            raise TypeError
        exception_type = f"{module}.{qualname}" if module and module != "builtins" else qualname
        message = str(exc)
        traceback_value = BaseException.__getattribute__(exc, "__traceback__")
        frames = "".join(traceback.format_tb(traceback_value))
        stacktrace = f"Traceback (most recent call last):\n{frames}{exception_type}: {message}" if frames else message
    except BaseException:
        exception_type, message, stacktrace = "Exception", "", ""
    redacted = [scrub_all_registered_from_text(value) for value in (exception_type, message, stacktrace)]
    redactor = current_codeblock_log_redactor()
    if redactor is not None:
        try:
            candidate = redactor(redacted)
            redacted = candidate if isinstance(candidate, list) and len(candidate) == 3 else ["", "", ""]
        except BaseException:
            redacted = ["", "", ""]
    exception_type, message, stacktrace = redacted
    span.add_event(
        "exception",
        attributes={
            "exception.type": exception_type,
            "exception.message": message,
            "exception.stacktrace": stacktrace,
            "exception.escaped": "False",
        },
    )
    if set_error_status:
        # Same shape OTel's use_span would have written, minus the credential.
        span.set_status(trace.Status(trace.StatusCode.ERROR, f"{exception_type}: {message}"))


@contextmanager
def traced_span(tracer: Any, name: str, **kwargs: Any) -> Iterator[Any]:
    """Start a span that cannot record an unscrubbed exception.

    OTel's ``use_span`` records the exception and derives the status from ``str(exc)`` on the way
    out, both unredacted, whenever one propagates out of the ``with`` block. Scrubbing at the
    ``record_exception`` call alone is not enough — ``use_span`` then appends a *second*, raw
    exception event and overwrites the scrubbed status. Turning both off and recording here makes
    this the only recorder.
    """
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False, **kwargs) as span:
        try:
            yield span
        except Exception as exc:
            record_span_exception(span, exc)
            raise


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
                with traced_span(trace.get_tracer("skyvern"), span_name) as span:
                    span.set_attribute("code.function", code_function)
                    span.set_attribute("code.namespace", code_namespace)
                    if role is not None:
                        span.set_attribute("skyvern.span.role", role)
                    apply_context_attrs(span)
                    if tags:
                        span.set_attribute("tags", tags)
                    return await func(*args, **kw)

            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args: Any, **kw: Any) -> Any:
                with traced_span(trace.get_tracer("skyvern"), span_name) as span:
                    span.set_attribute("code.function", code_function)
                    span.set_attribute("code.namespace", code_namespace)
                    if role is not None:
                        span.set_attribute("skyvern.span.role", role)
                    apply_context_attrs(span)
                    if tags:
                        span.set_attribute("tags", tags)
                    return func(*args, **kw)

            return sync_wrapper

    return decorator
