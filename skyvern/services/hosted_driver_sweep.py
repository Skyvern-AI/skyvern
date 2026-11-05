"""Idle sweep of hosted MCP browser drivers. Only the API starts it, so workers never import the MCP client."""

import asyncio

import structlog

from skyvern.cli.core.client import shutdown_hosted_drivers, sweep_idle_hosted_drivers
from skyvern.cli.core.session_manager import is_stateless_http_mode
from skyvern.config import settings

LOG = structlog.get_logger()


async def _hosted_driver_sweep_loop() -> None:
    while True:
        await asyncio.sleep(settings.HOSTED_MCP_DRIVER_SWEEP_INTERVAL_SECONDS)
        try:
            await sweep_idle_hosted_drivers()
        except Exception:
            LOG.exception("Hosted MCP driver sweep failed")


_hosted_driver_sweep_task: asyncio.Task | None = None


def start_hosted_driver_sweep() -> asyncio.Task | None:
    """Start the idle sweep of hosted MCP drivers (idempotent; only where the API serves stateless hosted MCP).

    It stops drivers by owner, never by process name or age, so it runs without the cleanup cron's
    "no active runs anywhere" gate, which would keep it from ever running in a busy deployment.
    """
    global _hosted_driver_sweep_task

    if not is_stateless_http_mode():
        return None
    if settings.HOSTED_MCP_DRIVER_SWEEP_INTERVAL_SECONDS <= 0 or settings.HOSTED_MCP_DRIVER_IDLE_SECONDS <= 0:
        LOG.info(
            "Hosted MCP driver sweep disabled",
            interval_seconds=settings.HOSTED_MCP_DRIVER_SWEEP_INTERVAL_SECONDS,
            idle_seconds=settings.HOSTED_MCP_DRIVER_IDLE_SECONDS,
        )
        return None
    if _hosted_driver_sweep_task is not None and not _hosted_driver_sweep_task.done():
        return _hosted_driver_sweep_task

    _hosted_driver_sweep_task = asyncio.create_task(_hosted_driver_sweep_loop())
    LOG.info(
        "Started hosted MCP driver sweep",
        interval_seconds=settings.HOSTED_MCP_DRIVER_SWEEP_INTERVAL_SECONDS,
        idle_seconds=settings.HOSTED_MCP_DRIVER_IDLE_SECONDS,
    )
    return _hosted_driver_sweep_task


async def stop_hosted_driver_sweep() -> None:
    """Stop the sweep, then every hosted MCP driver, as the API shuts down."""
    global _hosted_driver_sweep_task

    task, _hosted_driver_sweep_task = _hosted_driver_sweep_task, None
    try:
        if task is not None and not task.done():
            task.cancel()
            # asyncio.wait, unlike awaiting the task, lets the caller's own cancellation propagate.
            await asyncio.wait({task})
    finally:
        await shutdown_hosted_drivers()
