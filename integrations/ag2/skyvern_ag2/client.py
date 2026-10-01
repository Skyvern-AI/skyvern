"""AG2 tool functions for Skyvern cloud API.

Provides run_task_cloud, dispatch_task_cloud, and get_task_cloud as functions
that can be registered with AG2 agents via the decorator pattern.

Requires SKYVERN_API_KEY environment variable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any, TypeVar

from skyvern_ag2.settings import settings

from skyvern import Skyvern
from skyvern.client import SkyvernEnvironment


def _get_cloud_client(
    api_key: str | None = None,
    base_url: str | None = None,
) -> Skyvern:
    return Skyvern(
        environment=SkyvernEnvironment.CLOUD,
        base_url=base_url or settings.base_url,
        api_key=api_key or settings.api_key,
    )


T = TypeVar("T")


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from a sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def run_task_cloud(
    user_prompt: Annotated[str, "Natural language description of the browser task to execute"],
    url: Annotated[str | None, "Optional starting URL for the task"] = None,
) -> str:
    """Run a browser automation task using the Skyvern cloud API.

    Executes the task synchronously -- blocks until the task completes and
    returns the result. Requires SKYVERN_API_KEY environment variable.
    """
    client = _get_cloud_client()

    async def _execute() -> str:
        result = await client.run_task(
            prompt=user_prompt,
            url=url,
            engine=settings.engine,
            timeout=settings.run_task_timeout_seconds,
            wait_for_completion=True,
        )
        return str(result)

    return _run_async(_execute())


def dispatch_task_cloud(
    user_prompt: Annotated[str, "Natural language description of the browser task to execute"],
    url: Annotated[str | None, "Optional starting URL for the task"] = None,
) -> str:
    """Dispatch a browser automation task to the Skyvern cloud API.

    Returns immediately with a task ID. The task runs in the background.
    Use get_task_cloud() to poll for completion.
    Requires SKYVERN_API_KEY environment variable.
    """
    client = _get_cloud_client()

    async def _execute() -> str:
        result = await client.run_task(
            prompt=user_prompt,
            url=url,
            engine=settings.engine,
            timeout=settings.run_task_timeout_seconds,
            wait_for_completion=False,
        )
        return str(result)

    return _run_async(_execute())


def get_task_cloud(
    task_id: Annotated[str, "The task ID returned by dispatch_task"],
) -> str:
    """Get the status and result of a task from the Skyvern cloud API."""
    client = _get_cloud_client()

    async def _execute() -> str:
        result = await client.get_run(run_id=task_id)
        return str(result)

    return _run_async(_execute())
