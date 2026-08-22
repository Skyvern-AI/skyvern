from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming import screenshot
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class _DisconnectedWebSocket:
    """A client that is already gone but whose sends still succeed.

    That is the production shape: ASGI delivered ``websocket.disconnect``, but a send-only handler
    never reads it, so Starlette keeps accepting sends and asyncio writes them into a dead socket.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._disconnect_delivered = False

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive(self) -> dict:
        if not self._disconnect_delivered:
            self._disconnect_delivered = True
            return {"type": "websocket.disconnect", "code": 1006}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_workflow_run_streaming_stops_sending_once_the_client_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SKY-14645: this loop used to keep send_json-ing every 2s until the run finalized, emitting one
    # stdlib "socket.send() raised exception." per write for the whole remaining run.
    websocket = _DisconnectedWebSocket()
    workflow_run = SimpleNamespace(
        status=WorkflowRunStatus.running,
        organization_id="o_1",
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run)),
    )

    monkeypatch.setattr(screenshot.settings, "BROWSER_STREAMING_MODE", "vnc")
    monkeypatch.setattr(screenshot, "get_current_org", AsyncMock(return_value=SimpleNamespace(organization_id="o_1")))
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_streaming_file=AsyncMock(return_value=b"jpeg")))
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(mark_streaming_viewer_active=AsyncMock()))

    await asyncio.wait_for(
        screenshot.workflow_run_streaming(
            websocket=cast(WebSocket, websocket),
            workflow_run_id="wr_1",
            apikey="key",
        ),
        timeout=10,
    )

    assert len(websocket.sent) <= 1
