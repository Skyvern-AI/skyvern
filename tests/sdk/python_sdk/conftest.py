import asyncio
import socket
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from skyvern import Skyvern


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async session fixtures."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


class QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler that suppresses log messages."""

    def log_message(self, format, *args):
        """Override to suppress HTTP request logging."""


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    """Wait for the server to be ready by attempting to connect."""
    start_time = time.time()
    deadline = start_time + timeout

    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)

    return False


@pytest.fixture(scope="session")
async def web_server():
    """
    Start a local HTTP server on port 9009 serving from the 'web' directory.

    This is an async fixture that properly waits for the server to be ready.
    """
    # Get the directory where this conftest file is located
    test_dir = Path(__file__).parent
    web_dir = test_dir.parent / "web"

    # Create web directory if it doesn't exist
    web_dir.mkdir(exist_ok=True)

    # Create a handler class that serves from the specific directory
    def handler_factory(*args, **kwargs):
        return QuietHTTPRequestHandler(*args, directory=str(web_dir), **kwargs)

    # Create and start the HTTP server
    server = HTTPServer(("localhost", 9009), handler_factory)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Wait for the server to be ready (async-friendly)
    await asyncio.sleep(0.1)

    # Verify server is responding
    if not _wait_for_server("localhost", 9009):
        # Don't wait forever - just close the socket and let daemon thread die
        server.server_close()
        raise RuntimeError("Server failed to start")

    base_url = "http://localhost:9009"
    yield base_url

    # Cleanup: Don't call shutdown() as it can block forever waiting for active connections
    # Instead, just close the socket and let the daemon thread die
    server.server_close()


@pytest.fixture(scope="session")
async def skyvern_browser():
    """
    Launch a local browser once for the entire test session and reuse it across tests.

    This ensures all tests use the same browser instance, avoiding connection issues.
    """
    skyvern = Skyvern.local()
    browser = await skyvern.launch_local_browser(headless=False)

    yield browser

    # Cleanup: close the browser after all tests complete
    try:
        await browser.close()
    except Exception:
        pass  # Ignore cleanup errors
