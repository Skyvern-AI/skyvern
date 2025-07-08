from typing import Any, Callable


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
        return func

    return decorator


def file_download_block(
    prompt: str | None = None,
    title: str | None = None,
    url: str | None = None,
    max_steps: int | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        return func

    return decorator


def email_block(prompt: str | None = None, title: str | None = None, url: str | None = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        return func

    return decorator


def wait_block(seconds: int) -> Callable:
    def decorator(func: Callable) -> Callable:
        return func

    return decorator
