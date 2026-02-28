"""Unified HTTP/WebSocket server for browser serve command.

This server provides:
- CDP WebSocket proxy at /devtools/*
- CDP JSON API at /json/*
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from aiohttp import WSMsgType, web


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

    async def _proxy_json_endpoint(self, endpoint: str) -> web.Response:
        """Proxy a request to Chrome's CDP JSON API."""
        url = f"http://127.0.0.1:{self.config.chrome_cdp_port}{endpoint}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=10)
                data = response.json()

                # Rewrite WebSocket URLs to point to our proxy
                if isinstance(data, list):
                    for item in data:
                        self._rewrite_ws_urls(item)
                elif isinstance(data, dict):
                    self._rewrite_ws_urls(data)

                return web.json_response(data)
            except httpx.RequestError as e:
                return web.json_response(
                    {"error": "CDP connection failed", "message": str(e)},
                    status=502,
                )

    def _rewrite_ws_urls(self, data: dict) -> None:
        """Rewrite Chrome's CDP WebSocket URLs to point to our proxy."""
        for key in ["webSocketDebuggerUrl", "devtoolsFrontendUrl"]:
            if key in data and data[key]:
                # Replace Chrome's internal port with our proxy port
                data[key] = data[key].replace(
                    f"127.0.0.1:{self.config.chrome_cdp_port}",
                    f"127.0.0.1:{self.config.port}",
                )

    async def _handle_json(self, request: web.Request) -> web.Response:
        """Handle /json endpoint."""
        return await self._proxy_json_endpoint("/json")

    async def _handle_json_version(self, request: web.Request) -> web.Response:
        """Handle /json/version endpoint."""
        return await self._proxy_json_endpoint("/json/version")

    async def _handle_json_list(self, request: web.Request) -> web.Response:
        """Handle /json/list endpoint."""
        return await self._proxy_json_endpoint("/json/list")

    async def _handle_json_protocol(self, request: web.Request) -> web.Response:
        """Handle /json/protocol endpoint."""
        return await self._proxy_json_endpoint("/json/protocol")

    # -------------------------------------------------------------------------
    # CDP WebSocket proxy handler
    # -------------------------------------------------------------------------

    async def _handle_devtools_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Proxy WebSocket connections to Chrome's CDP."""
        import asyncio

        import aiohttp

        path = request.match_info["path"]
        chrome_ws_url = f"ws://127.0.0.1:{self.config.chrome_cdp_port}/devtools/{path}"

        # Set up client-facing WebSocket
        ws_client = web.WebSocketResponse()
        await ws_client.prepare(request)

        # Connect to Chrome's CDP WebSocket
        async with aiohttp.ClientSession() as session:
            try:
                async with session.ws_connect(chrome_ws_url) as ws_chrome:
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
            except aiohttp.ClientError as e:
                if not ws_client.closed:
                    await ws_client.close(code=1011, message=str(e).encode())

        return ws_client

    # -------------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok"})
