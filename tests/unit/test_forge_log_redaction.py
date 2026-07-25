import json
from collections.abc import Iterator

import pytest

from skyvern.forge.sdk.copilot import secret_scrub
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.forge_log import redact_bearer_tokens, redact_registered_secrets

_FAKE_CREDENTIAL = "fake-pa55w0rd-7x9"


@pytest.fixture(autouse=True)
def _isolate_session_scrub_registry() -> Iterator[None]:
    secret_scrub._SESSION_SCRUB_VALUES.clear()
    yield
    secret_scrub._SESSION_SCRUB_VALUES.clear()


def _register_credential(value: str) -> None:
    secret_scrub._SESSION_SCRUB_VALUES.setdefault("pbs_1", []).append(value)


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


def test_redacts_a_registered_credential_from_a_log_event() -> None:
    _register_credential(_FAKE_CREDENTIAL)
    event = {"event": f'filling #password with "{_FAKE_CREDENTIAL}"', "selector": "#password"}
    out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]
    assert _FAKE_CREDENTIAL not in out["event"]
    assert REDACTED_SECRET_PLACEHOLDER in out["event"]
    assert out["selector"] == "#password"


def test_redacts_a_registered_credential_from_every_string_field() -> None:
    _register_credential(_FAKE_CREDENTIAL)
    event = {"event": f"code: {_FAKE_CREDENTIAL}", "msg": f"error near {_FAKE_CREDENTIAL}"}
    out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]
    assert _FAKE_CREDENTIAL not in out["event"]
    assert _FAKE_CREDENTIAL not in out["msg"]


def test_credential_redaction_passes_through_when_nothing_is_registered() -> None:
    event = {"event": f"contains {_FAKE_CREDENTIAL} but nothing was registered"}
    assert redact_registered_secrets(None, "info", event) == event  # type: ignore[arg-type]


def test_credential_redaction_tolerates_non_string_values() -> None:
    _register_credential(_FAKE_CREDENTIAL)
    event = {"event": "no secret here", "count": 42, "tags": ["a", "b"]}
    assert redact_registered_secrets(None, "info", event) == event  # type: ignore[arg-type]


def test_redacts_a_credential_nested_inside_a_kwarg() -> None:
    """add_kv_pairs_to_msg folds nested kwargs into the rendered line, so recursion is required."""
    _register_credential(_FAKE_CREDENTIAL)
    event = {
        "event": "tool call",
        "arguments": {"fills": [{"selector": "#pass", "value": _FAKE_CREDENTIAL}]},
    }

    out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]

    assert _FAKE_CREDENTIAL not in json.dumps(out)
    assert out["arguments"]["fills"][0]["selector"] == "#pass"


def test_redacts_a_credential_inside_a_tuple_value() -> None:
    _register_credential(_FAKE_CREDENTIAL)
    event = {"event": "x", "pair": ("user", _FAKE_CREDENTIAL)}

    out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]

    assert out["pair"] == ("user", REDACTED_SECRET_PLACEHOLDER)
