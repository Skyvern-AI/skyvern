from __future__ import annotations

import json

import pytest

from skyvern.browser_extension.auth import compute_ext_proof, compute_server_proof
from skyvern.browser_extension.broker.protocol import (
    BROKER_FRAME_VERSION,
    build_broker_challenge,
    build_broker_nonce,
    build_error_frame,
    build_event_frame,
    build_request_frame,
    build_response_frame,
    build_state_frame,
    compute_broker_client_proof,
    compute_broker_server_proof,
    is_valid_broker_nonce,
    parse_broker_frame,
    verify_broker_client_proof,
)
from skyvern.browser_extension.errors import BrowserExtensionError

TOKEN = "broker-protocol-test-token"


def test_broker_proofs_do_not_interchange_with_extension_proofs() -> None:
    server_nonce = build_broker_nonce()
    client_nonce = build_broker_nonce()

    broker_proof = compute_broker_client_proof(TOKEN, server_nonce, client_nonce)
    extension_proof = compute_ext_proof(TOKEN, server_nonce, client_nonce)

    assert broker_proof != extension_proof
    assert verify_broker_client_proof(TOKEN, server_nonce, client_nonce, broker_proof)
    assert not verify_broker_client_proof(TOKEN, server_nonce, client_nonce, extension_proof)
    assert compute_broker_server_proof(TOKEN, client_nonce, server_nonce) != compute_server_proof(
        TOKEN, client_nonce, server_nonce
    )


def test_broker_client_proof_is_bound_to_both_nonces_and_the_token() -> None:
    server_nonce = build_broker_nonce()
    client_nonce = build_broker_nonce()
    proof = compute_broker_client_proof(TOKEN, server_nonce, client_nonce)

    assert not verify_broker_client_proof("other-token", server_nonce, client_nonce, proof)
    assert not verify_broker_client_proof(TOKEN, build_broker_nonce(), client_nonce, proof)
    assert not verify_broker_client_proof(TOKEN, server_nonce, build_broker_nonce(), proof)


@pytest.mark.parametrize(
    "nonce",
    ["", "not base64!", "c2hvcnQ", build_broker_nonce() + "=", build_broker_nonce()[:-1]],
)
def test_only_a_full_length_urlsafe_nonce_is_accepted(nonce: str) -> None:
    assert not is_valid_broker_nonce(nonce)


def test_a_generated_nonce_is_accepted() -> None:
    assert is_valid_broker_nonce(build_broker_nonce())


def test_challenge_carries_a_fresh_nonce_every_time() -> None:
    first_nonce, first_frame = build_broker_challenge()
    second_nonce, _ = build_broker_challenge()

    assert first_nonce != second_nonce
    assert first_frame == {"v": BROKER_FRAME_VERSION, "type": "auth.challenge", "serverNonce": first_nonce}


def test_request_and_response_frames_round_trip() -> None:
    request = parse_broker_frame(
        json.dumps(build_request_frame("r-1", "tabs.create", {"url": "https://example.test"}, 12.5)),
        from_client=True,
    )
    assert request.request_id == "r-1"
    assert request.op == "tabs.create"
    assert request.args == {"url": "https://example.test"}
    assert request.timeout_seconds == pytest.approx(12.5)

    ok = parse_broker_frame(json.dumps(build_response_frame("r-1", {"tabId": 7})), from_client=False)
    assert ok.ok is True
    assert ok.result == {"tabId": 7}

    failed = parse_broker_frame(json.dumps(build_error_frame("r-1", "TAB_NOT_SCOPED", "nope")), from_client=False)
    assert failed.ok is False
    assert failed.error_code == "TAB_NOT_SCOPED"
    assert failed.error_message == "nope"


def test_event_and_state_frames_round_trip() -> None:
    event = parse_broker_frame(json.dumps(build_event_frame("scope.tabAdded", {"tabId": 5})), from_client=False)
    assert event.event == "scope.tabAdded"
    assert event.params == {"tabId": 5}

    state = parse_broker_frame(
        json.dumps(build_state_frame(True, [{"tabId": 5, "url": "", "title": ""}, {"tabId": "bad"}])),
        from_client=False,
    )
    assert state.extension_connected is True
    assert state.scoped_tabs == [{"tabId": 5, "url": "", "title": ""}]


def test_each_direction_only_accepts_its_own_frame_kinds() -> None:
    with pytest.raises(BrowserExtensionError, match="Unknown broker frame type"):
        parse_broker_frame(json.dumps(build_response_frame("r-1", {})), from_client=True)
    with pytest.raises(BrowserExtensionError, match="Unknown broker frame type"):
        parse_broker_frame(json.dumps(build_request_frame("r-1", "tabs.list", {}, 1.0)), from_client=False)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([1, 2, 3]),
        json.dumps({"v": 2, "type": "ping"}),
        json.dumps({"v": 1, "type": "unknown"}),
        json.dumps({"v": 1, "type": "request", "id": "", "op": "tabs.list", "args": {}, "timeoutMs": 1}),
        json.dumps({"v": 1, "type": "request", "id": "r", "op": "tabs.list", "args": {}, "timeoutMs": 0}),
        json.dumps({"v": 1, "type": "request", "id": "r", "op": "tabs.list", "args": [], "timeoutMs": 1}),
        json.dumps({"v": 1, "type": "client.hello", "protocol": "1", "pid": 1}),
    ],
)
def test_malformed_frames_are_rejected(raw: str) -> None:
    with pytest.raises(BrowserExtensionError):
        parse_broker_frame(raw, from_client=True)
