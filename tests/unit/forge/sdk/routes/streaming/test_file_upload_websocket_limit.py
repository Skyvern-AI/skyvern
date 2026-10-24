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
