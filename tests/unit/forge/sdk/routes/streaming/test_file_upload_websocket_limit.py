import asyncio
import base64
import json
import socket

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect

from skyvern.config import settings
from skyvern.forge.payload_limits import max_file_upload_ws_message_size
from skyvern.forge.sdk.routes.streaming.cdp_input import _dispatch_file_upload_event


async def _receive_message_size(websocket: WebSocket) -> None:
    await websocket.accept()
    message = await websocket.receive_text()
    await websocket.send_text(str(len(message.encode("utf-8"))))


@pytest.mark.asyncio
async def test_uvicorn_sansio_accepts_25mb_file_upload_frame() -> None:
    app = Starlette(routes=[WebSocketRoute("/", _receive_message_size)])
    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ws="websockets-sansio",
            ws_max_size=max_file_upload_ws_message_size(settings.MAX_UPLOAD_FILE_SIZE),
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))

    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)

            file_content = base64.b64encode(bytes(25 * 1024 * 1024)).decode("ascii")
            message = json.dumps(
                {
                    "kind": "fileUpload",
                    "fileName": "upload.bin",
                    "mimeType": "application/octet-stream",
                    "content": file_content,
                },
                separators=(",", ":"),
            )

            async with connect(f"ws://127.0.0.1:{port}/") as websocket:
                await websocket.send(message)
                assert await websocket.recv() == str(len(message.encode("utf-8")))
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        finally:
            server_socket.close()


class _FileInputPage:
    def __init__(self) -> None:
        self.uploaded: list[dict] = []
        outer = self

        class _Locator:
            async def count(self) -> int:
                return 1

            async def set_input_files(self, payload: dict) -> None:
                outer.uploaded.append(payload)

        self._locator = _Locator()

    def locator(self, _selector: str) -> object:
        return self._locator


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "expected_kind"),
    [
        (settings.MAX_UPLOAD_FILE_SIZE, "file-upload-complete"),
        (settings.MAX_UPLOAD_FILE_SIZE + 1, "file-upload-error"),
    ],
)
async def test_live_session_file_upload_accepts_up_to_the_server_upload_cap(size: int, expected_kind: str) -> None:
    page, websocket = _FileInputPage(), _RecordingWebSocket()
    message = {
        "fileName": "upload.bin",
        "mimeType": "application/octet-stream",
        "content": base64.b64encode(bytes(size)).decode("ascii"),
    }

    await _dispatch_file_upload_event(page, message, websocket)  # type: ignore[arg-type]

    assert websocket.sent[-1]["kind"] == expected_kind
    assert bool(page.uploaded) is (expected_kind == "file-upload-complete")
