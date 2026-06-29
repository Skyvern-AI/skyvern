from __future__ import annotations

import pytest

from skyvern.forge.sdk.routes.streaming.channels import vnc as vnc_module
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS,
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
