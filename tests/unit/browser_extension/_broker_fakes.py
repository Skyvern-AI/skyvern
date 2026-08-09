from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import Callable
from contextlib import suppress

import aiohttp

from skyvern.browser_extension.auth import compute_ext_proof

EXTENSION_ORIGIN = "chrome-extension://fake-extension-id"

Responder = Callable[[str, dict], dict]


class FakeExtension:
    """Minimal stand-in for the Chrome extension's side of /extension/v1.

    Speaks the real HMAC handshake and the real request/response frames so broker tests exercise
    the production relay rather than a mock of it.
    """

    def __init__(self, port: int, token: str, responder: Responder | None = None) -> None:
        self._port = port
        self._token = token
        self._responder = responder or _default_responder()
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self.create_response_gate: asyncio.Event | None = None
        self.requests: list[tuple[str, dict]] = []

    async def connect(self, scoped_tabs: list[dict] | None = None) -> None:
        session = aiohttp.ClientSession()
        websocket = await session.ws_connect(
            f"ws://127.0.0.1:{self._port}/extension/v1",
            headers={"Origin": EXTENSION_ORIGIN},
        )
        challenge = json.loads((await websocket.receive()).data)
        assert challenge["type"] == "auth.challenge"
        client_nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        await websocket.send_json(
            {
                "v": 1,
                "type": "auth.proof",
                "clientNonce": client_nonce,
                "proof": compute_ext_proof(self._token, challenge["serverNonce"], client_nonce),
            }
        )
        auth_ok = json.loads((await websocket.receive()).data)
        assert auth_ok["type"] == "auth.ok"

        self._session = session
        self._websocket = websocket
        self._reader = asyncio.create_task(self._run_reader(websocket))
        await self.send_event("extension.hello", {"scopedTabs": scoped_tabs or []})

    async def close(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        if self._websocket is not None and not self._websocket.closed:
            await self._websocket.close()
        if self._session is not None:
            await self._session.close()

    async def send_event(self, event: str, params: dict) -> None:
        websocket = self._websocket
        assert websocket is not None
        await websocket.send_json({"v": 1, "type": "event", "event": event, "params": params})

    async def _run_reader(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for message in websocket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if frame.get("type") == "ping":
                await websocket.send_json({"v": 1, "type": "pong"})
                continue
            if frame.get("type") != "request":
                continue
            operation = frame["op"]
            arguments = frame["args"]
            self.requests.append((operation, arguments))
            try:
                result = self._responder(operation, arguments)
            except _ExtensionFailure as failure:
                await websocket.send_json(
                    {
                        "v": 1,
                        "type": "response",
                        "id": frame["id"],
                        "ok": False,
                        "error": {"code": failure.code, "message": failure.message},
                    }
                )
                continue
            if operation == "tabs.create":
                # The real extension announces the scoped tab before its create handler returns.
                await self.send_event(
                    "scope.tabAdded",
                    {"tabId": result["tabId"], "url": arguments.get("url", "about:blank"), "title": ""},
                )
                if self.create_response_gate is not None:
                    await self.create_response_gate.wait()
            await websocket.send_json({"v": 1, "type": "response", "id": frame["id"], "ok": True, "result": result})


class _ExtensionFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _default_responder() -> Responder:
    next_tab_id = iter(range(101, 1000))

    def respond(operation: str, arguments: dict) -> dict:
        if operation == "tabs.create":
            return {"tabId": next(next_tab_id)}
        if operation == "tabs.list":
            return {"tabs": []}
        if operation == "debugger.send":
            return {"result": {"method": arguments.get("method")}}
        return {}

    return respond


async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")
