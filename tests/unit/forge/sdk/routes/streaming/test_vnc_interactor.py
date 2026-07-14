from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from skyvern.forge.sdk.routes.streaming.channels import message as message_module
from skyvern.forge.sdk.routes.streaming.channels import vnc as vnc_module
from skyvern.forge.sdk.routes.streaming.channels.message import MessageChannel, MessageKind, loop_stream_messages
from skyvern.forge.sdk.routes.streaming.channels.vnc import Interactor, VncChannel
from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession


def _browser_session(interactor: str | None) -> AddressablePersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return AddressablePersistentBrowserSession(
        persistent_browser_session_id="pbs_test",
        organization_id="org_test",
        status="running",
        browser_address="",
        vnc_port=6087,
        interactor=interactor,
        created_at=now,
        modified_at=now,
    )


def _app_with_repository() -> tuple[SimpleNamespace, AsyncMock]:
    update = AsyncMock()
    browser_sessions = SimpleNamespace(update_persistent_browser_session=update)
    app = SimpleNamespace(DATABASE=SimpleNamespace(browser_sessions=browser_sessions))
    return app, update


def _vnc_channel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored_interactor: str | None,
    initial_interactor: Interactor,
) -> tuple[VncChannel, AsyncMock, MagicMock]:
    app, update = _app_with_repository()
    log = MagicMock()
    monkeypatch.setattr(vnc_module, "app", app, raising=False)
    monkeypatch.setattr(vnc_module, "LOG", log)
    monkeypatch.setattr(vnc_module, "add_vnc_channel", MagicMock())
    channel = VncChannel(
        client_id="client_test",
        organization_id="org_test",
        vnc_port=6080,
        x_api_key="",
        websocket=SimpleNamespace(client_state=WebSocketState.CONNECTED),
        initial_interactor=initial_interactor,
        browser_session=_browser_session(stored_interactor),
    )
    return channel, update, log


@pytest.mark.parametrize(
    ("stored_interactor", "initial_interactor", "expected_interactor"),
    [
        ("user", "agent", "user"),
        ("agent", "user", "agent"),
        (None, "user", "user"),
    ],
)
def test_channel_restores_valid_persisted_interactor_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    stored_interactor: str | None,
    initial_interactor: Interactor,
    expected_interactor: Interactor,
) -> None:
    channel, update, log = _vnc_channel(
        monkeypatch,
        stored_interactor=stored_interactor,
        initial_interactor=initial_interactor,
    )

    assert channel.interactor == expected_interactor
    update.assert_not_awaited()
    log.warning.assert_not_called()


def test_channel_falls_back_and_warns_for_invalid_persisted_interactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, update, log = _vnc_channel(
        monkeypatch,
        stored_interactor="operator",
        initial_interactor="agent",
    )

    assert channel.interactor == "agent"
    update.assert_not_awaited()
    log.warning.assert_called_once_with(
        "VncChannel Invalid persisted interactor; using requested initial interactor.",
        persisted_interactor="operator",
        organization_id="org_test",
        browser_session_id="pbs_test",
    )


def _message_websocket(*messages: dict[str, str]) -> SimpleNamespace:
    websocket = SimpleNamespace(client_state=WebSocketState.CONNECTED)
    websocket.receive_json = AsyncMock(side_effect=[*messages, WebSocketDisconnect()])

    async def close(*, code: int, reason: str | None) -> None:
        websocket.client_state = WebSocketState.DISCONNECTED

    websocket.close = AsyncMock(side_effect=close)
    websocket.send_json = AsyncMock()
    return websocket


async def _run_control_messages(
    monkeypatch: pytest.MonkeyPatch,
    channel: VncChannel,
    *kinds: MessageKind,
) -> SimpleNamespace:
    websocket = _message_websocket(*(dict(kind=kind.value) for kind in kinds))
    monkeypatch.setattr(message_module, "LOG", MagicMock())
    monkeypatch.setattr(message_module, "add_message_channel", MagicMock())
    monkeypatch.setattr(message_module, "del_message_channel", MagicMock())
    message_channel = MessageChannel(
        client_id=channel.client_id,
        organization_id=channel.organization_id,
        websocket=websocket,
        browser_session=channel.browser_session,
    )
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: channel)

    await loop_stream_messages(message_channel)
    return websocket


@pytest.mark.parametrize(
    ("kind", "expected_interactor"),
    [
        (MessageKind.TAKE_CONTROL, "user"),
        (MessageKind.CEDE_CONTROL, "agent"),
    ],
)
@pytest.mark.asyncio
async def test_control_message_changes_state_before_persisting(
    monkeypatch: pytest.MonkeyPatch,
    kind: MessageKind,
    expected_interactor: Interactor,
) -> None:
    channel, update, _ = _vnc_channel(
        monkeypatch,
        stored_interactor=None,
        initial_interactor="agent" if expected_interactor == "user" else "user",
    )

    async def assert_state_before_write(*args: object, **kwargs: object) -> None:
        assert channel.interactor == expected_interactor

    update.side_effect = assert_state_before_write

    await _run_control_messages(monkeypatch, channel, kind)

    assert channel.interactor == expected_interactor
    update.assert_awaited_once_with(
        "pbs_test",
        organization_id="org_test",
        interactor=expected_interactor,
    )


@pytest.mark.asyncio
async def test_persistence_failure_does_not_break_later_control_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    channel, update, log = _vnc_channel(
        monkeypatch,
        stored_interactor=None,
        initial_interactor="agent",
    )
    update.side_effect = RuntimeError("database unavailable")

    websocket = await _run_control_messages(
        monkeypatch,
        channel,
        MessageKind.TAKE_CONTROL,
        MessageKind.CEDE_CONTROL,
    )

    assert channel.interactor == "agent"
    assert update.await_count == 2
    assert websocket.receive_json.await_count == 3
    assert log.exception.call_count == 2
