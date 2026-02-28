"""Unified HTTP/WebSocket server for browser serve command.

This server provides:
- CDP WebSocket proxy at /devtools/*
- CDP JSON API at /json/*
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from aiohttp import WSMsgType, web

LOG = structlog.get_logger()


@dataclass
class UnifiedServerConfig:
    """Configuration for the unified server."""

    port: int
    chrome_cdp_port: int
    api_key: str | None = None


class UnifiedServer:
    """Unified HTTP/WebSocket server that proxies CDP connections."""

    def __init__(self, config: UnifiedServerConfig) -> None:
        self.config = config
        self.app = web.Application(middlewares=[self._auth_middleware])
        self._setup_routes()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: web.RequestHandler) -> web.StreamResponse:
        """Check API key if configured."""
        if self.config.api_key:
            provided_key = request.headers.get("x-api-key")
            if provided_key != self.config.api_key:
                return web.json_response(
                    {"error": "Unauthorized", "message": "Invalid or missing x-api-key"},
                    status=401,
                )
        return await handler(request)

    def _setup_routes(self) -> None:
        """Set up all routes for the unified server."""
        # CDP JSON API routes (with and without trailing slashes)
        self.app.router.add_get("/json", self._handle_json)
        self.app.router.add_get("/json/", self._handle_json)
        self.app.router.add_get("/json/version", self._handle_json_version)
        self.app.router.add_get("/json/version/", self._handle_json_version)
        self.app.router.add_get("/json/list", self._handle_json_list)
        self.app.router.add_get("/json/list/", self._handle_json_list)
        self.app.router.add_get("/json/protocol", self._handle_json_protocol)
        self.app.router.add_get("/json/protocol/", self._handle_json_protocol)

        # CDP WebSocket proxy
        self.app.router.add_route("GET", "/devtools/{path:.*}", self._handle_devtools_ws)

        # Health check
        self.app.router.add_get("/health", self._handle_health)

    async def start(self) -> None:
        """Start the unified server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.config.port)
        await self._site.start()

    async def stop(self) -> None:
        """Stop the unified server."""
        if self._runner:
            await self._runner.cleanup()

    # -------------------------------------------------------------------------
    # CDP JSON API handlers
    # -------------------------------------------------------------------------

    async def _proxy_json_endpoint(self, request: web.Request, endpoint: str) -> web.Response:
        """Proxy a request to Chrome's CDP JSON API."""
        url = f"http://127.0.0.1:{self.config.chrome_cdp_port}{endpoint}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=10)
                data = response.json()

                # Get the external host and protocol from the request (e.g., ngrok URL)
                external_host = request.headers.get("Host", f"127.0.0.1:{self.config.port}")
                forwarded_proto = request.headers.get("X-Forwarded-Proto", "")

                # Rewrite WebSocket URLs to point to our proxy via the external host
                if isinstance(data, list):
                    for item in data:
                        self._rewrite_ws_urls(item, external_host, forwarded_proto)
                elif isinstance(data, dict):
                    self._rewrite_ws_urls(data, external_host, forwarded_proto)

                return web.json_response(data)
            except httpx.RequestError as e:
                return web.json_response(
                    {"error": "CDP connection failed", "message": str(e)},
                    status=502,
                )

    def _rewrite_ws_urls(self, data: dict, external_host: str, forwarded_proto: str = "") -> None:
        """Rewrite Chrome's CDP WebSocket URLs to point to our proxy via external host."""
        for key in ["webSocketDebuggerUrl", "devtoolsFrontendUrl"]:
            if key in data and data[key]:
                # Determine the WebSocket scheme based on whether we're behind HTTPS
                # Check X-Forwarded-Proto first (works with any HTTPS-terminating proxy),
                # then fall back to known tunnel domains (ngrok)
                if forwarded_proto == "https" or external_host.endswith((".ngrok-free.dev", ".ngrok.io")):
                    ws_scheme = "wss"
                else:
                    ws_scheme = "ws"

                # Replace Chrome's internal URL with our proxy URL
                # Original: ws://127.0.0.1:{chrome_port}/devtools/...
                # New: ws(s)://{external_host}/devtools/...
                data[key] = data[key].replace(
                    f"ws://127.0.0.1:{self.config.chrome_cdp_port}",
                    f"{ws_scheme}://{external_host}",
                )

    async def _handle_json(self, request: web.Request) -> web.Response:
        """Handle /json endpoint."""
        return await self._proxy_json_endpoint(request, "/json")

    async def _handle_json_version(self, request: web.Request) -> web.Response:
        """Handle /json/version endpoint."""
        return await self._proxy_json_endpoint(request, "/json/version")

    async def _handle_json_list(self, request: web.Request) -> web.Response:
        """Handle /json/list endpoint."""
        return await self._proxy_json_endpoint(request, "/json/list")

    async def _handle_json_protocol(self, request: web.Request) -> web.Response:
        """Handle /json/protocol endpoint."""
        return await self._proxy_json_endpoint(request, "/json/protocol")

    # -------------------------------------------------------------------------
    # CDP WebSocket proxy handler
    # -------------------------------------------------------------------------

    async def _handle_devtools_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Proxy WebSocket connections to Chrome's CDP."""
        import asyncio

        import aiohttp

        path = request.match_info["path"]
        chrome_ws_url = f"ws://127.0.0.1:{self.config.chrome_cdp_port}/devtools/{path}"

        LOG.info(
            "CDP WebSocket connection request",
            path=path,
            chrome_ws_url=chrome_ws_url,
            client_host=request.headers.get("Host", "unknown"),
        )

        # Set up client-facing WebSocket with heartbeat to keep connection alive
        ws_client = web.WebSocketResponse(heartbeat=30.0)
        await ws_client.prepare(request)

        # Connect to Chrome's CDP WebSocket with timeout and heartbeat
        timeout = aiohttp.ClientTimeout(total=60, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.ws_connect(chrome_ws_url, heartbeat=30.0) as ws_chrome:
                    LOG.info("CDP WebSocket connected to Chrome", path=path)

                    # Bidirectional message relay
                    async def relay_client_to_chrome() -> None:
                        async for msg in ws_client:
                            if msg.type == WSMsgType.TEXT:
                                await ws_chrome.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await ws_chrome.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break

                    async def relay_chrome_to_client() -> None:
                        async for msg in ws_chrome:
                            if msg.type == WSMsgType.TEXT:
                                await ws_client.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await ws_client.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break

                    # Run both relay tasks concurrently
                    await asyncio.gather(
                        relay_client_to_chrome(),
                        relay_chrome_to_client(),
                        return_exceptions=True,
                    )
                    LOG.info("CDP WebSocket connection closed normally", path=path)
            except aiohttp.ClientError as e:
                LOG.error("CDP WebSocket connection error", path=path, error=str(e))
                if not ws_client.closed:
                    await ws_client.close(code=1011, message=str(e).encode())

        return ws_client

    # -------------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})
