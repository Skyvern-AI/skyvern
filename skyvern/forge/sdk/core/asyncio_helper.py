import asyncio


def is_aio_task_running(aio_task: asyncio.Task) -> bool:
    return not aio_task.done() and not aio_task.cancelled()
