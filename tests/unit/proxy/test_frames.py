from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from skyvern.proxy.core.frames import (
    PROXY_CLIENT_KEY,
    CdpCommand,
    CdpEvent,
    CdpResponse,
    FrameDecodeError,
    RemapperFullError,
    RequestIdRemapper,
    decode_frame,
    encode_frame,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_decode_command_response_event() -> None:
    assert decode_frame('{"id": 1, "method": "Page.enable", "sessionId": "s1"}') == CdpCommand(
        id=1, method="Page.enable", session_id="s1"
    )
    assert decode_frame('{"id": 1, "result": {"ok": true}}') == CdpResponse(id=1, result={"ok": True})
    assert decode_frame('{"method": "Page.loadEventFired", "params": {"timestamp": 1}}') == CdpEvent(
        method="Page.loadEventFired", params={"timestamp": 1}
    )


@pytest.mark.parametrize(
    "raw",
    [r'{"id":1,"method":"\ud800"}', r'{"method":"Page.loadEventFired","sessionId":"\ud800"}'],
)
def test_decode_rejects_lone_utf16_surrogates(raw: str) -> None:
    with pytest.raises(FrameDecodeError):
        decode_frame(raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"id":1,"method":"Page.enable","params":{"value":1e400}}',
        '{"id":1,"method":"Page.enable","params":{"value":-1e400}}',
        '{"id":1,"result":{"value":1e400}}',
        '{"id":1,"result":{"value":-1e400}}',
    ],
)
def test_decode_rejects_float_overflow(raw: str) -> None:
    with pytest.raises(FrameDecodeError):
        decode_frame(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " \n",
        b"",
        b"\x80\x81",
        "not json",
        "[1]",
        "null",
        "true",
        "{}",
        '{"id": 1}',
        '{"id": null, "method": "Page.enable"}',
        '{"id": true, "method": "Page.enable"}',
        '{"id": "1", "method": "Page.enable"}',
        '{"id": 1.5, "method": "Page.enable"}',
        '{"id": 1, "method": ""}',
        '{"id": 1, "method": 7}',
        '{"id": 1, "method": "Page.enable", "params": null}',
        '{"id": 1, "method": "Page.enable", "params": []}',
        '{"id": 1, "method": "Page.enable", "sessionId": null}',
        '{"id": 1, "method": "Page.enable", "sessionId": ""}',
        '{"id": 1, "method": "Page.enable", "result": {}}',
        '{"id": 1, "result": null}',
        '{"id": 1, "result": []}',
        '{"id": 1, "result": {}, "error": {"code": -1, "message": "bad"}}',
        '{"id": 1, "error": {"code": "-1", "message": "bad"}}',
        '{"id": 1, "error": {"code": -1}}',
        '{"method": "Page.loadEventFired", "params": null}',
        '{"method": "Page.loadEventFired", "params": []}',
        '{"method": "Page.loadEventFired", "sessionId": 1}',
        '{"id": 1, "result": {}, "unexpected": true}',
        '{"id": 1, "method": "Page.enable", "id": 2}',
    ],
)
def test_decode_rejects_malformed_frames(raw: str | bytes) -> None:
    with pytest.raises(FrameDecodeError):
        decode_frame(raw)


@pytest.mark.parametrize("fixture_name", ["browser_client_a.jsonl", "browser_client_b.jsonl"])
def test_recorded_frames_round_trip_semantically(fixture_name: str) -> None:
    raw_frames = [line for line in (FIXTURES_DIR / fixture_name).read_text().splitlines() if line]

    assert raw_frames
    for raw in raw_frames:
        decoded = decode_frame(raw)
        encoded = encode_frame(decoded)
        encoded.encode("utf-8")
        assert decode_frame(encoded) == decoded


@pytest.mark.parametrize(
    "frame",
    [
        CdpCommand(id=1, method="Runtime.evaluate", params={"expression": "café"}),
        CdpResponse(id=1, result={"value": "café"}),
        CdpEvent(method="Runtime.consoleAPICalled", params={"text": "café"}),
    ],
)
def test_generated_valid_frames_round_trip(frame: CdpCommand | CdpResponse | CdpEvent) -> None:
    encoded = encode_frame(frame)
    encoded.encode("utf-8")
    assert decode_frame(encoded) == frame


def test_mutated_bytes_only_raise_frame_decode_error() -> None:
    randomizer = random.Random(12506)
    seeds = [
        b"\x80",
        b"{}",
        b'{"id": "one"}',
        b'{"method": null}',
        b'{"id": 1, "method": "Page.enable", "params": []}',
    ]
    mutations: list[bytes] = []

    for seed in seeds:
        for _ in range(32):
            mutated = bytearray(seed)
            index = randomizer.randrange(len(mutated))
            mutated[index] ^= randomizer.randrange(1, 256)
            mutations.append(bytes(mutated))

            cut = randomizer.randrange(len(seed) + 1)
            mutations.append(seed[:cut])
            insert_at = randomizer.randrange(len(seed) + 1)
            inserted = bytes((randomizer.randrange(256),))
            mutations.append(seed[:insert_at] + inserted + seed[insert_at:])

    base = b'{"id": 1, "method": "Runtime.enable", "params": {}}'
    mutations.extend(
        [
            base.replace(b'"id": 1', b'"id": null'),
            base.replace(b'"id": 1', b'"id": "1"'),
            base.replace(b'"method": "Runtime.enable"', b'"method": null'),
            base.replace(b'"params": {}', b'"params": []'),
            base.replace(b'"params": {}', b'"params": null'),
            base[:-1],
            base[:-1] + b",}",
            b'{"id":1,"result":{},"error":{"code":-1,"message":"bad"}}',
            b'{"id":1,"method":"Runtime.enable","method":"Page.enable"}',
            b'{"id":1,"method":"Runtime.enable","unexpected":true}',
            b'{"id":1}',
            b'{"method":""}',
        ]
    )

    for raw in mutations:
        try:
            decoded = decode_frame(raw)
        except FrameDecodeError:
            continue
        except Exception as exc:
            pytest.fail(f"unexpected exception for mutated frame {raw!r}: {exc!r}")
        assert isinstance(decoded, (CdpCommand, CdpResponse, CdpEvent))


def test_encode_roundtrip() -> None:
    for frame in (
        CdpCommand(id=3, method="Target.attachToTarget", params={"targetId": "t", "flatten": True}, session_id="s"),
        CdpResponse(id=3, result={"sessionId": "s2"}),
        CdpEvent(method="Target.attachedToTarget", params={}, session_id="s"),
    ):
        assert decode_frame(encode_frame(frame)) == frame


def test_error_response_omits_result() -> None:
    payload = json.loads(encode_frame(CdpResponse(id=4, error={"code": -32000, "message": "nope"})))
    assert payload == {"id": 4, "error": {"code": -32000, "message": "nope"}}


def test_request_id_remapper_roundtrip_and_unknown_ids() -> None:
    remapper = RequestIdRemapper()
    upstream_a = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    upstream_b = remapper.to_upstream("client-b", CdpCommand(id=1, method="Page.enable"))
    assert upstream_a.id != upstream_b.id

    mapped = remapper.to_client(CdpResponse(id=upstream_b.id, result={}))
    assert mapped is not None
    client_key, response = mapped
    assert client_key == "client-b"
    assert response.id == 1

    assert remapper.to_client(CdpResponse(id=upstream_b.id, result={})) is None
    assert remapper.to_client(CdpResponse(id=9999, result={})) is None


def test_request_id_remapper_cleanup_is_client_scoped() -> None:
    remapper = RequestIdRemapper()
    first = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    second = remapper.to_upstream("client-a", CdpCommand(id=2, method="Runtime.enable"))
    remapper.to_upstream("client-b", CdpCommand(id=1, method="Network.enable"))

    assert remapper.pending_count == 3
    assert remapper.clear_client("client-a") == 2
    assert remapper.pending_count == 1
    assert remapper.to_client(CdpResponse(id=first.id, result={})) is None
    assert remapper.to_client(CdpResponse(id=second.id, result={})) is None
    assert remapper.clear_client("client-a") == 0
    remapper.clear()
    assert remapper.pending_count == 0


def test_request_id_remapper_bounds_pending_mappings() -> None:
    # SKY-12500 (AC5): a command whose response never arrives must not pin an entry
    # forever. The oldest pending mapping is evicted once the cap is reached, so a
    # long-lived client cannot grow the table without bound.
    remapper = RequestIdRemapper(max_pending=3)
    oldest = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    for client_id in range(2, 6):
        remapper.to_upstream("client-a", CdpCommand(id=client_id, method="Page.enable"))

    assert remapper.pending_count == 3
    # The evicted command's late response is unmatched (dropped), never mis-routed.
    assert remapper.to_client(CdpResponse(id=oldest.id, result={})) is None


def test_a_client_at_the_cap_evicts_only_its_own_pending_request() -> None:
    # SKY-12500: the flooder pays its own tax. At the cap a client reclaims its own
    # oldest mapping — a co-tenant's admitted response is never the thing dropped.
    remapper = RequestIdRemapper(max_pending=2)
    victim = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    flooder_first = remapper.to_upstream("client-b", CdpCommand(id=1, method="Network.enable"))
    flooder_second = remapper.to_upstream("client-b", CdpCommand(id=2, method="Runtime.enable"))

    assert len({victim.id, flooder_first.id, flooder_second.id}) == 3
    # B's oldest went, not A's.
    assert remapper.to_client(CdpResponse(id=flooder_first.id, result={})) is None
    assert remapper.to_client(CdpResponse(id=victim.id, result={})) == ("client-a", CdpResponse(id=1, result={}))


def test_a_full_table_refuses_a_client_with_nothing_of_its_own_to_evict() -> None:
    # Rather than silently drop a co-tenant's live mapping, the newcomer is refused
    # (its caller closes it) — a lost response would desync the victim invisibly.
    remapper = RequestIdRemapper(max_pending=2)
    kept_a = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    kept_b = remapper.to_upstream("client-b", CdpCommand(id=1, method="Network.enable"))

    with pytest.raises(RemapperFullError):
        remapper.to_upstream("client-c", CdpCommand(id=1, method="Runtime.enable"))

    # Both incumbents still resolve to their own owners.
    assert remapper.to_client(CdpResponse(id=kept_a.id, result={})) == ("client-a", CdpResponse(id=1, result={}))
    assert remapper.to_client(CdpResponse(id=kept_b.id, result={})) == ("client-b", CdpResponse(id=1, result={}))


def test_a_flooding_client_never_evicts_the_proxy_lane() -> None:
    # The proxy's own in-flight command must survive any client's flood; losing it
    # would strand proxy state on a response that never correlates.
    remapper = RequestIdRemapper(max_pending=2)
    proxy = remapper.to_upstream_as_proxy(CdpCommand(id=1, method="Target.setAutoAttach"))
    remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    for client_id in range(2, 8):
        remapper.to_upstream("client-a", CdpCommand(id=client_id, method="Page.enable"))

    mapped = remapper.to_client(CdpResponse(id=proxy.id, result={}))
    assert mapped is not None and mapped[0] == PROXY_CLIENT_KEY


def test_the_proxy_lane_is_refused_rather_than_evicting_a_client() -> None:
    remapper = RequestIdRemapper(max_pending=1)
    kept = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))

    with pytest.raises(RemapperFullError):
        remapper.to_upstream_as_proxy(CdpCommand(id=1, method="Target.setAutoAttach"))

    assert remapper.to_client(CdpResponse(id=kept.id, result={})) == ("client-a", CdpResponse(id=1, result={}))


def test_discard_frees_a_pending_mapping_whose_response_is_suppressed() -> None:
    # SKY-12500 (AC5): a response the pipeline swallows must still free its mapping.
    remapper = RequestIdRemapper()
    upstream = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    assert remapper.pending_count == 1

    remapper.discard(upstream.id)
    assert remapper.pending_count == 0
    assert remapper.to_client(CdpResponse(id=upstream.id, result={})) is None
    remapper.discard(upstream.id)  # idempotent


def test_proxy_lane_is_reserved_and_never_claimed_by_a_client() -> None:
    # SKY-12500 (AC2): proxy-issued commands ride a lane no client can claim or observe.
    remapper = RequestIdRemapper()
    with pytest.raises(ValueError):
        remapper.to_upstream(PROXY_CLIENT_KEY, CdpCommand(id=1, method="Target.setAutoAttach"))

    upstream = remapper.to_upstream_as_proxy(CdpCommand(id=1, method="Target.setAutoAttach"))
    mapped = remapper.to_client(CdpResponse(id=upstream.id, result={}))
    assert mapped is not None
    assert mapped[0] == PROXY_CLIENT_KEY


def test_proxy_and_client_commands_share_one_upstream_id_space() -> None:
    # A proxy command must never collide with a client's in-flight id: a collision
    # would hand the proxy's response to a client (or swallow the client's).
    remapper = RequestIdRemapper()
    client = remapper.to_upstream("client-a", CdpCommand(id=1, method="Page.enable"))
    proxy = remapper.to_upstream_as_proxy(CdpCommand(id=1, method="Target.setAutoAttach"))
    assert client.id != proxy.id

    assert remapper.to_client(CdpResponse(id=client.id, result={})) == (
        "client-a",
        CdpResponse(id=1, result={}),
    )
    proxy_mapped = remapper.to_client(CdpResponse(id=proxy.id, result={}))
    assert proxy_mapped is not None and proxy_mapped[0] == PROXY_CLIENT_KEY


def test_clear_client_cannot_evict_the_proxy_lane() -> None:
    remapper = RequestIdRemapper()
    proxy = remapper.to_upstream_as_proxy(CdpCommand(id=1, method="Target.setAutoAttach"))
    with pytest.raises(ValueError):
        remapper.clear_client(PROXY_CLIENT_KEY)

    mapped = remapper.to_client(CdpResponse(id=proxy.id, result={}))
    assert mapped is not None and mapped[0] == PROXY_CLIENT_KEY


def test_request_id_remapper_seeded_multi_client_property() -> None:
    randomizer = random.Random(12506)
    remapper = RequestIdRemapper()
    pending: dict[int, tuple[str, int]] = {}

    for client_index in range(24):
        client_key = f"client-{client_index}"
        for _ in range(19):
            original_id = randomizer.randint(1, 7)
            command = CdpCommand(id=original_id, method="Runtime.enable")
            upstream = remapper.to_upstream(client_key, command)
            assert upstream.id not in pending
            pending[upstream.id] = (client_key, original_id)

    assert remapper.pending_count == len(pending)
    upstream_ids = list(pending)
    randomizer.shuffle(upstream_ids)
    for upstream_id in upstream_ids:
        client_key, original_id = pending[upstream_id]
        assert remapper.to_client(CdpResponse(id=upstream_id, result={"request": upstream_id})) == (
            client_key,
            CdpResponse(id=original_id, result={"request": upstream_id}),
        )

    assert remapper.pending_count == 0
    assert len(remapper) == 0
    assert remapper.to_client(CdpResponse(id=upstream_ids[0], result={})) is None
