from __future__ import annotations

import pytest

from skyvern.forge.sdk.routes.streaming.channels import vnc as vnc_module
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS,
    KeyState,
    MessageType,
    VncChannel,
)


def make_vnc_channel() -> VncChannel:
    channel = object.__new__(VncChannel)
    channel.remote_clipboard_synced_at = None
    return channel


def test_client_cut_text_message_type_matches_rfb_protocol() -> None:
    assert MessageType.ClientCutText.value == 6


def test_remote_clipboard_recently_synced_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = make_vnc_channel()

    monkeypatch.setattr(vnc_module.time, "monotonic", lambda: 10.0)
    channel.mark_remote_clipboard_synced()

    monkeypatch.setattr(
        vnc_module.time,
        "monotonic",
        lambda: 10.0 + REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS - 0.1,
    )

    assert channel.remote_clipboard_was_recently_synced() is True


def test_remote_clipboard_sync_guard_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = make_vnc_channel()

    monkeypatch.setattr(vnc_module.time, "monotonic", lambda: 10.0)
    channel.mark_remote_clipboard_synced()

    monkeypatch.setattr(
        vnc_module.time,
        "monotonic",
        lambda: 10.0 + REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS + 0.1,
    )

    assert channel.remote_clipboard_was_recently_synced() is False


def test_remote_clipboard_sync_guard_defaults_to_false() -> None:
    channel = make_vnc_channel()

    assert channel.remote_clipboard_was_recently_synced() is False


def rfb_key(keysym: int, down: bool) -> bytes:
    return bytes([4, int(down), 0, 0]) + keysym.to_bytes(4, "big")


# Current frontends send macOS Cmd as Super_L; older ones sent left Cmd as Alt_L.
@pytest.mark.parametrize("cmd_keysym", [0xFFEB, 0xFFE9])
def test_cmd_c_is_copy_for_either_cmd_keysym(cmd_keysym: int) -> None:
    channel = make_vnc_channel()
    channel.key_state = KeyState()
    c_down = rfb_key(ord("c"), True)

    channel.update_key_state(rfb_key(cmd_keysym, True))
    assert channel.key_state.is_copy(c_down) is True

    channel.update_key_state(rfb_key(cmd_keysym, False))
    assert channel.key_state.is_copy(c_down) is False
