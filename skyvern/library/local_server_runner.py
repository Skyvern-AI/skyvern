import asyncio
import atexit
import logging
import socket

import structlog
import uvicorn

from skyvern.config import settings
from skyvern.forge.forge_uvicorn import create_uvicorn_config

LOG = structlog.get_logger()

# Global server tracker for cleanup
_server: uvicorn.Server | None = None
_server_task: asyncio.Task | None = None


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("localhost", port))
            return False
        except OSError:
            return True


def _cleanup_on_exit() -> None:
    """Synchronous cleanup handler for atexit."""
    global _server, _server_task

    if _server is None:
        return

    LOG.info("Shutting down local Skyvern server (atexit)...")

    # Signal server to exit
    _server.should_exit = True

    # If there's a running event loop, try to cancel the task
    if _server_task is not None and not _server_task.done():
        _server_task.cancel()

    _server = None
    _server_task = None


async def _wait_for_server(port: int, timeout: float = 10.0, interval: float = 0.5) -> bool:
    """Wait for the server to become available on the specified port."""
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        if _is_port_in_use(port):
            return True
        await asyncio.sleep(interval)
    return False


async def ensure_local_server_running(port: int | None = None) -> None:
    """Ensure a local Skyvern server is running on the specified port.

    If the server is not running, starts it in the current event loop as a background task.
    The server will automatically stop when stop_local_server() is called.

    Args:
        port: The port number the server should run on. Defaults to settings.PORT.
    """
    global _server, _server_task

    if port is None:
        port = settings.PORT

    # Check if server is already running
    if _is_port_in_use(port):
        LOG.info(f"Local Skyvern server already running on port {port}")
        return

    # Check if we already have a server instance
    if _server is not None:
        LOG.info("Local Skyvern server already started by this process")
        return

    # Server not running, start it in the current event loop
    LOG.info(f"Starting local Skyvern server on port {port}...")

    # Import here to avoid circular imports
    from skyvern.forge.api_app import app  # noqa: PLC0415

    # Suppress CancelledError logs from uvicorn during shutdown
    # When asyncio.run() closes the event loop, it cancels all remaining tasks.
    # This includes uvicorn's lifespan receive queue, which logs a CancelledError.
    # This error is expected and harmless - the server is shutting down cleanly.
    # We can't catch this with try-except because it happens inside uvicorn's internals.
    class _SuppressCancelledErrorFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return not (record.exc_info and isinstance(record.exc_info[1], asyncio.CancelledError))

    logging.getLogger("uvicorn.error").addFilter(_SuppressCancelledErrorFilter())

    # Create uvicorn server configuration (disable reload in programmatic mode)
    config = create_uvicorn_config(app, port=port, reload=False)

    _server = uvicorn.Server(config)

    # Wrap server in a task that handles cancellation gracefully
    async def _run_server() -> None:
        """Run server and handle cancellation during event loop shutdown."""
        try:
            await _server.serve()
        except asyncio.CancelledError:
            # Expected when event loop closes - suppress the error
            LOG.debug("Server task cancelled during shutdown")
            pass

    # Start server as a background task
    _server_task = asyncio.create_task(_run_server())

    # Register atexit handler as final fallback
    atexit.register(_cleanup_on_exit)

    # Wait for server to start
    if await _wait_for_server(port, timeout=10.0):
        LOG.info("Local Skyvern server started successfully")
    else:
        LOG.error("Failed to start local Skyvern server (timeout)")
        await stop_local_server()
        raise RuntimeError(f"Local Skyvern server failed to start on port {port}")


async def stop_local_server() -> None:
    """Stop the local server if it was started by this process."""
    global _server, _server_task

    if _server is not None:
        LOG.info("Shutting down local Skyvern server...")
        _server.should_exit = True
        if _server_task is not None:
            try:
                await asyncio.wait_for(_server_task, timeout=5.0)
            except asyncio.TimeoutError:
                LOG.warning("Server did not stop gracefully within timeout")
                _server_task.cancel()
                try:
                    await _server_task
                except asyncio.CancelledError:
                    pass
            _server_task = None
        _server = None
        LOG.info("Local Skyvern server stopped")
