from typing import Any, Callable

from skyvern import RunContext, SkyvernPage


# Build a dummy workflow decorator
def workflow(
    title: str | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    webhook_url: str | None = None,
    max_steps: int | None = None,
) -> Callable:
    def wrapper(func: Callable) -> Callable:
        return func

    return wrapper


def task_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    engine: str | None = None,
    model: dict[str, Any] | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    max_steps: int | None = None,
    navigation_payload: str | None = None,
    webhook_url: str | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def login_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    engine: str | None = None,
    model: dict[str, Any] | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    max_steps: int | None = None,
    navigation_payload: str | None = None,
    webhook_url: str | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def navigation_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    engine: str | None = None,
    model: dict[str, Any] | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    max_steps: int | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def action_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    engine: str | None = None,
    model: dict[str, Any] | None = None,
    totp_url: str | None = None,
    totp_identifier: str | None = None,
    max_steps: int | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def extraction_block(
    title: str | None = None,
    data_extraction_goal: str | None = None,
    data_extraction_schema: dict[str, Any] | list | str | None = None,
    model: dict[str, Any] | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the data_extraction_goal as prompt in the context
            context.prompt = data_extraction_goal
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def url_block(
    title: str | None = None,
    url: str | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # No prompt to store for url_block
            context.prompt = None
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def file_download_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    max_steps: int | None = None,
    engine: str | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def email_block(prompt: str | None = None, title: str | None = None, url: str | None = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def wait_block(seconds: int, title: str | None = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # No prompt to store for wait_block
            context.prompt = None
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator


def text_prompt_block(
    prompt: str | None = None,
    title: str | None = None,
    json_schema: dict[str, Any] | list | str | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        async def wrapper(page: SkyvernPage, context: RunContext, *args: Any, **kwargs: Any) -> Any:
            # Store the prompt in the context
            context.prompt = prompt
            return await func(page, context, *args, **kwargs)

        return wrapper

    return decorator
