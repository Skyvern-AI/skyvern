import asyncio
from functools import wraps
from typing import Any, Callable

from opentelemetry import trace


def traced(name: str | None = None, tags: list[str] | None = None) -> Callable:
    """Decorator that creates an OTEL span. No-op without SDK installed.

    Args:
        name: Span name. If not provided, uses func.__qualname__.
        tags: Tags to add as a span attribute.
    """

    def decorator(func: Callable) -> Callable:
        span_name = name or func.__qualname__

        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kw: Any) -> Any:
                with trace.get_tracer("testcharmvision").start_as_current_span(span_name) as span:
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
                with trace.get_tracer("testcharmvision").start_as_current_span(span_name) as span:
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
