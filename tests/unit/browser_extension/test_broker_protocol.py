from __future__ import annotations

import asyncio
import struct

import pytest

from skyvern.browser_extension import broker_protocol
from skyvern.browser_extension.auth import (
    compute_broker_proof,
    compute_client_proof,
    hash_recovery_secret,
    verify_client_proof,
)
from skyvern.browser_extension.broker_protocol import (
    BROKER_GENERATION,
    BROKER_PROTOCOL_VERSION,
    decode_frame,
    encode_frame,
    new_nonce,
    peer_uid_from_transport,
    read_frame,
    redact,
    request_frame,
    response_frame,
)
from skyvern.browser_extension.errors import BrowserExtensionBrokerError


def test_length_prefixed_json_round_trip() -> None:
    frame = request_frame("request-1", "broker.status")

    encoded = encode_frame(frame)

    assert struct.unpack("!I", encoded[:4])[0] == len(encoded) - 4
    assert decode_frame(encoded[4:]) == frame


@pytest.mark.asyncio
async def test_declared_frame_budget_is_rejected_before_payload_read() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 9_000))

    with pytest.raises(BrowserExtensionBrokerError, match="FRAME_TOO_LARGE"):
        await read_frame(reader, max_size=8_192)


@pytest.mark.asyncio
async def test_declared_bytes_are_reserved_before_payload_read() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 100))

    with pytest.raises(BrowserExtensionBrokerError, match="RESOURCE_LIMIT"):
        await read_frame(reader, reserve=lambda _size: False)


@pytest.mark.asyncio
async def test_large_control_declaration_is_rejected_from_canonical_prefix_before_body_read() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 1024 * 1024) + b'{"v":1,"type":"pong","padding":')

    with pytest.raises(BrowserExtensionBrokerError, match="FRAME_TOO_LARGE"):
        await read_frame(
            reader,
            max_size=32 * 1024 * 1024,
            control_size=64 * 1024,
            large_request_op="extension.request",
        )


@pytest.mark.asyncio
async def test_large_extension_request_is_admitted_by_canonical_prefix() -> None:
    frame = request_frame("large-1", "extension.request", {"padding": "x" * (64 * 1024)})
    encoded = encode_frame(frame)
    reader = asyncio.StreamReader()
    reader.feed_data(encoded)

    decoded, size = await read_frame(
        reader,
        max_size=32 * 1024 * 1024,
        control_size=64 * 1024,
        large_request_op="extension.request",
    )

    assert decoded == frame
    assert size == len(encoded) - 4


@pytest.mark.asyncio
async def test_large_extension_response_is_admitted_only_for_pending_operation() -> None:
    frame = response_frame("large-1", {"padding": "x" * (64 * 1024)})
    encoded = encode_frame(frame)
    reader = asyncio.StreamReader()
    reader.feed_data(encoded)

    decoded, _size = await read_frame(
        reader,
        max_size=32 * 1024 * 1024,
        control_size=64 * 1024,
        large_response_ids={"large-1"},
        large_event="extension.event",
    )

    assert decoded == frame


@pytest.mark.asyncio
async def test_large_broker_heartbeat_is_rejected_before_body_read() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 1024 * 1024) + b'{"v":1,"type":"ping","padding":')

    with pytest.raises(BrowserExtensionBrokerError, match="FRAME_TOO_LARGE"):
        await read_frame(
            reader,
            max_size=32 * 1024 * 1024,
            control_size=64 * 1024,
            large_response_ids=set(),
            large_event="extension.event",
        )


def test_enrollment_proofs_use_distinct_mutual_contexts() -> None:
    secret = "recovery-secret-sentinel"
    server_nonce = new_nonce()
    client_nonce = new_nonce()
    client_id = "client-id"
    client_proof = compute_client_proof(secret, server_nonce, client_nonce, client_id, BROKER_GENERATION)
    broker_proof = compute_broker_proof(secret, client_nonce, server_nonce, client_id, BROKER_GENERATION)

    assert verify_client_proof(
        secret,
        server_nonce,
        client_nonce,
        client_id,
        BROKER_GENERATION,
        client_proof,
    )
    assert client_proof != broker_proof
    assert secret not in hash_recovery_secret(secret)


def test_redaction_removes_all_sensitive_material_recursively() -> None:
    sentinel = "must-not-survive"
    value = {
        "v": BROKER_PROTOCOL_VERSION,
        "token": sentinel,
        "pairingUrl": sentinel,
        "args": {"proof": sentinel, "params": {"url": sentinel}},
        "safe": "status",
    }

    redacted = redact(value)

    assert sentinel not in repr(redacted)
    assert isinstance(redacted, dict)
    assert redacted["safe"] == "status"


def test_peer_uid_supports_getpeereid_only_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    class TransportSocket:
        def fileno(self) -> int:
            return 41

    class RawSocket:
        def getpeereid(self) -> tuple[int, int]:
            return 1234, 5678

        def close(self) -> None:
            return None

    monkeypatch.delattr(broker_protocol.socket, "SO_PEERCRED", raising=False)
    monkeypatch.delattr(broker_protocol.socket, "LOCAL_PEERCRED", raising=False)
    monkeypatch.setattr(broker_protocol.os, "dup", lambda _fd: 42)
    monkeypatch.setattr(broker_protocol.socket, "socket", lambda *, fileno: RawSocket())

    assert peer_uid_from_transport(TransportSocket()) == 1234
