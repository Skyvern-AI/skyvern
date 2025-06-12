import asyncio
from typing import Any, Sequence


async def collect(tasks: Sequence[asyncio.Task]) -> list[Any]:
    """
    An alternative to 'gather'.

    Waits for the first task to complete or fail, cancels others, and propagates
    the first exception.

    Returns the results of all tasks (if all tasks succeed).
    """

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    for p in pending:
        p.cancel()

    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if exc:
            raise exc

    return [task.result() for task in done]
