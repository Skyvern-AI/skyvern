"""Tests for the copilot SSE terminal-frame invariant (SKY-9232)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from skyvern.forge.sdk.routes.workflow_copilot import _ensure_terminal_frame


class _FakeStream:
    def __init__(self, raise_on_send: BaseException | None = None) -> None:
        self.sent: list[Any] = []
        self._raise_on_send = raise_on_send

    async def send(self, message: Any) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(message)


@pytest.mark.asyncio
async def test_ensure_terminal_frame_noop_when_already_emitted() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=True)  # type: ignore[arg-type]
    assert stream.sent == []


@pytest.mark.asyncio
async def test_ensure_terminal_frame_sends_fallback_error_when_missing() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]
    assert len(stream.sent) == 1
    frame = stream.sent[0]
    assert getattr(frame, "error", "").startswith("The assistant didn't finish")


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_exception() -> None:
    stream = _FakeStream(raise_on_send=RuntimeError("client already gone"))
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_cancellation() -> None:
    stream = _FakeStream(raise_on_send=asyncio.CancelledError())
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]
