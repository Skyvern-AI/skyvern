from typing import Any, Callable

from skyvern import RunContext, SkyvernPage
from skyvern.core.script_generations.script_run_context_manager import script_run_context_manager


# Build a dummy workflow decorator
def workflow(
    title: str | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    webhook_url: str | None = None,
    max_steps: int | None = None,
) -> Callable:
    def wrapper(func: Callable) -> Callable:
        # TODO: create a workflow run object
        return func

    return wrapper


def cached(cache_key: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        script_run_context_manager.set_cached_fn(cache_key, func)

        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the function in context.cached_fns
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator
