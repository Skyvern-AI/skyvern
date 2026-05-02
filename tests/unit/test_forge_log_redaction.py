from skyvern.forge.sdk.forge_log import redact_bearer_tokens


def test_redacts_url_encoded_bearer_token() -> None:
    event = {
        "event": "WebSocket /v1/stream/vnc/browser_session/pbs_xxx?token=Bearer%20eyJhbGciOiJSUzI1NiI&client_id=abc"
    }
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert "eyJhbGciOiJSUzI1NiI" not in out["event"]
    assert "token=<redacted>" in out["event"]
    assert "client_id=abc" in out["event"]


def test_redacts_raw_bearer_token() -> None:
    event = {"msg": "auth failed for token=Bearer abc.def.ghi"}
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert "abc.def.ghi" not in out["msg"]
    assert "token=<redacted>" in out["msg"]


def test_redacts_bare_token_without_bearer_prefix() -> None:
    event = {"event": "callback url ?token=eyJhbGciOiJSUzI1NiI&foo=bar"}
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert "eyJhbGciOiJSUzI1NiI" not in out["event"]
    assert "token=<redacted>" in out["event"]
    assert "foo=bar" in out["event"]


def test_passes_through_when_no_token() -> None:
    event = {"event": "GET /api/v1/heartbeat HTTP/1.1 200 OK"}
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert out == event


def test_handles_non_string_values() -> None:
    event = {"event": "no token here", "count": 42, "tags": ["a", "b"]}
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert out == event


def test_redacts_in_arbitrary_string_keys() -> None:
    event = {"event": "ok", "url": "https://x.y/z?token=Bearer%20abcXYZ-_."}
    out = redact_bearer_tokens(None, "info", event)  # type: ignore[arg-type]
    assert "abcXYZ" not in out["url"]
    assert out["url"].endswith("token=<redacted>")
