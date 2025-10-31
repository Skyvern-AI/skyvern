import asyncio
import atexit
import socket
import threading

import structlog
import uvicorn

from skyvern.config import settings

LOG = structlog.get_logger()

# Global server tracker for cleanup
_server: uvicorn.Server | None = None
_server_thread: threading.Thread | None = None


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
    global _server, _server_thread

    if _server is None:
        return

    LOG.info("Shutting down local Skyvern server (atexit)...")

    # Signal server to exit
    _server.should_exit = True

    # Wait for server thread to finish
    if _server_thread is not None and _server_thread.is_alive():
        _server_thread.join(timeout=5.0)

    _server = None
    _server_thread = None


async def _wait_for_server(port: int, timeout: float = 10.0, interval: float = 0.5) -> bool:
    """Wait for the server to become available on the specified port."""
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < timeout:
        if _is_port_in_use(port):
            return True
        await asyncio.sleep(interval)
    return False


async def ensure_local_server_running() -> None:
    """Ensure a local Skyvern server is running on the specified port.

    If the server is not running, starts it in a separate thread with its own event loop.
    The server will automatically stop when the process exits.
    """
    global _server, _server_thread

    port = settings.PORT

    # Check if server is already running
    if _is_port_in_use(port):
        LOG.info(f"Local Skyvern server already running on port {port}")
        return

    # Check if we already have a server instance
    if _server is not None:
        LOG.info("Local Skyvern server already started by this process")
        return

    # Server not running, start it in a separate thread
    LOG.info(f"Starting local Skyvern server on port {port}...")

    # Import here to avoid circular imports
    from skyvern.forge.api_app import app  # noqa: PLC0415

    # Create uvicorn server configuration (disable reload in programmatic mode)
    uvicorn_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        reload=False,
        access_log=False,
    )

    _server = uvicorn.Server(uvicorn_config)

    # Run server in a separate thread with its own event loop
    def _run_server_in_thread() -> None:
        """Run the server in a separate thread with its own event loop."""
        asyncio.run(_server.serve())

    _server_thread = threading.Thread(target=_run_server_in_thread, daemon=True, name="skyvern-server")
    _server_thread.start()

    # Register atexit handler to ensure cleanup
    atexit.register(_cleanup_on_exit)

    # Wait for server to start
    if await _wait_for_server(port, timeout=10.0):
        LOG.info("Local Skyvern server started successfully")
    else:
        LOG.error("Failed to start local Skyvern server (timeout)")
        await _stop_local_server()
        raise RuntimeError(f"Local Skyvern server failed to start on port {port}")


async def _stop_local_server() -> None:
    """Stop the local server if it was started by this process."""
    global _server, _server_thread

    if _server is not None:
        LOG.info("Shutting down local Skyvern server...")
        _server.should_exit = True

        # Wait for server thread to finish (in a thread pool to avoid blocking event loop)
        if _server_thread is not None and _server_thread.is_alive():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _server_thread.join, 5.0)

        _server_thread = None
        _server = None
        LOG.info("Local Skyvern server stopped")
