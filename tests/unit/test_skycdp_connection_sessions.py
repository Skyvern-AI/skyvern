"""Session bookkeeping when one target carries more than one CDP session.

CDP domain state (Fetch patterns, paused requests, enabled domains) is per *session*, not per
target. Playwright's ``new_cdp_session`` therefore attaches a fresh session so the caller's domain
use cannot collide with the driver's own. This file pins the same contract onto ``CdpConnection``:
a supplementary session coexists with the page's primary session without displacing it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession
from skyvern.webeye.skycdp.facade.browser import BrowserContext, CdpSessionFacade
from skyvern.webeye.skycdp.transport import CdpTransport
from tests.unit.test_skycdp_transport import FakeSocket

pytestmark = pytest.mark.asyncio


class ChromeLikeResponder:
    """Answer every command frame, emitting ``Target.attachedToTarget`` BEFORE the command result
    for attach commands — the order the real browser uses, and the order that trips a connection
    which registers attach results only in the response path."""

    def __init__(self, socket: FakeSocket, next_session_id: str) -> None:
        self._socket = socket
        self._next_session_id = next_session_id
        self._answered: set[int] = set()
        self.attach_frames: list[dict] = []

    async def run_until(self, predicate: asyncio.Future) -> None:
        while not predicate.done():
            for frame in list(self._socket.sent):
                frame_id = frame.get("id")
                if frame_id is None or frame_id in self._answered:
                    continue
                self._answered.add(frame_id)
                if frame["method"] == "Target.attachToTarget":
                    self.attach_frames.append(frame)
                    self._socket.push(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {
                                "sessionId": self._next_session_id,
                                "targetInfo": {
                                    "targetId": frame["params"]["targetId"],
                                    "type": "page",
                                    "url": "https://example.com",
                                },
                            },
                        }
                    )
                    self._socket.push({"id": frame_id, "result": {"sessionId": self._next_session_id}})
                else:
                    self._socket.push({"id": frame_id, "result": {}})
            await asyncio.sleep(0)


async def _connection_with_primary_page() -> tuple[CdpConnection, FakeSocket, CdpSession, list[CdpSession]]:
    socket = FakeSocket()
    transport = CdpTransport(socket)
    await transport.start()
    connection = CdpConnection(transport)

    announced: list[CdpSession] = []
    connection.on_page_session(announced.append)

    started = asyncio.ensure_future(connection.start())
    responder = ChromeLikeResponder(socket, next_session_id="unused")
    await responder.run_until(started)
    await started

    socket.push(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "S-primary",
                "targetInfo": {"targetId": "T1", "type": "page", "url": "https://example.com"},
            },
        }
    )
    settle = asyncio.ensure_future(asyncio.sleep(0.05))
    await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
    await settle

    primary = connection.sessions["S-primary"]
    return connection, socket, primary, announced


async def test_supplementary_attach_returns_a_distinct_session() -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        assert supplementary.session_id == "S-supp"
        assert supplementary is not primary
    finally:
        await connection.close()


async def test_supplementary_attach_does_not_displace_the_primary_session() -> None:
    connection, socket, primary, announced = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        await pending

        # The page's own session must stay the one attach() resolves, or every later caller
        # (frames, evaluation, teardown) silently migrates onto the supplementary session.
        resolved = asyncio.ensure_future(connection.attach("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-wrong").run_until(resolved)
        assert (await resolved) is primary

        # And a supplementary session is raw access, not a page: announcing it would make the
        # context build a duplicate Page facade for a target that already has one.
        assert announced == [primary]
    finally:
        await connection.close()


async def test_dropping_the_supplementary_session_keeps_the_primary_mapping() -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        socket.push({"method": "Target.detachedFromTarget", "params": {"sessionId": "S-supp"}})
        settle = asyncio.ensure_future(asyncio.sleep(0.05))
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
        await settle

        assert supplementary.detached
        assert not primary.detached
        resolved = asyncio.ensure_future(connection.attach("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-wrong").run_until(resolved)
        assert (await resolved) is primary
    finally:
        await connection.close()


async def test_new_cdp_session_attaches_a_dedicated_session_for_the_caller() -> None:
    # The call-site pin for the SKY-14066 fix itself: without it, every other test here stays green
    # while new_cdp_session hands back the page's own session and the engine's route dispatcher
    # settles the caller's paused Fetch requests. Browser-free on purpose — the conformance test
    # that also covers this needs a real Chromium the gating CI arm does not have.
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        context = BrowserContext(browser=SimpleNamespace(connection=connection), browser_context_id=None)
        page_stub = SimpleNamespace(session=primary)
        pending = asyncio.ensure_future(context.new_cdp_session(page_stub))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        facade = await pending

        assert any(
            frame["method"] == "Target.attachToTarget" and frame["params"]["targetId"] == "T1" for frame in socket.sent
        )
        assert facade._session is not primary
        assert facade._session.session_id == "S-supp"
    finally:
        await connection.close()


async def test_target_destruction_reaps_supplementary_sessions_too() -> None:
    # _target_sessions holds only the primary, so reaping by the mapping alone leaves a
    # supplementary session alive in bookkeeping and on the transport's subscriber index after
    # its target is gone.
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        socket.push({"method": "Target.targetDestroyed", "params": {"targetId": "T1"}})
        settle = asyncio.ensure_future(asyncio.sleep(0.05))
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
        await settle

        assert primary.detached
        assert supplementary.detached
        assert "S-supp" not in connection.sessions
    finally:
        await connection.close()


async def test_session_facade_detach_releases_the_supplementary_session() -> None:
    connection, socket, _, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        facade = CdpSessionFacade(connection, session=supplementary)
        detached = asyncio.ensure_future(facade.detach())
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(detached)
        await detached

        assert any(
            frame["method"] == "Target.detachFromTarget" and frame["params"] == {"sessionId": "S-supp"}
            for frame in socket.sent
        )
    finally:
        await connection.close()
