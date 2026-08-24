import json
import logging
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from types import MappingProxyType

import pytest
import structlog
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge import log_redaction
from skyvern.forge.log_redaction import (
    REDACTED,
    is_proxy_observability_key,
    redact_bearer_tokens_in_text,
    redact_proxy_observability_value,
    redact_sensitive_fields,
    strip_artifact_url_query,
)
from skyvern.forge.sdk.copilot import secret_scrub
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import (
    compact_action_objects,
    redact_bearer_tokens,
    redact_registered_secrets,
    redact_sensitive_event_fields,
    setup_logger,
)
from skyvern.schemas.proxy_pinning import ProxyObservabilityField, RedactedProxyLogValue, redact_proxy_location
from skyvern.schemas.runs import GeoTarget

_FAKE_CREDENTIAL = "fake-pa55w0rd-7x9"
_REDACTED = "****"
_SYNTHETIC_PROXY_CREDENTIAL = "synthetic-proxy-secret"
_SYNTHETIC_PROXY_HOST = "internalproxy"
_SYNTHETIC_PROXY_URL = f"http://user:{_SYNTHETIC_PROXY_CREDENTIAL}@{_SYNTHETIC_PROXY_HOST}:8080"


def _nested_mapping(value: object, depth: int = 22) -> object:
    for _ in range(depth):
        value = {"child": value}
    return value


def _emit_native_json_log(
    capsys: pytest.CaptureFixture[str],
    context: SkyvernContext,
    **event_fields: object,
) -> str:
    root = logging.getLogger()
    saved_config = structlog.get_config()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        setup_logger()
        with skyvern_context.scoped(context):
            structlog.get_logger("skyvern.test.proxy_redaction").warning("synthetic proxy event", **event_fields)
        return capsys.readouterr().err
    finally:
        structlog.configure(**saved_config)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_proxy_observability_key_classifier_matches_values_not_metadata() -> None:
    for key in (
        "proxy_location",
        "profile_proxy_location",
        "input_proxy_location",
        "effective_proxy_location",
        "proxy_url",
        "proxy_host",
        "geo_target",
    ):
        assert is_proxy_observability_key(key)

    for key in ("proxy_location_type", "input_proxy_location_present", "proxy_session_id", 200):
        assert not is_proxy_observability_key(key)


def test_proxy_observability_renderer_preserves_marked_values() -> None:
    rendered = redact_proxy_location({"url": "http://user:synthetic-secret@token.proxy.example:8080"})

    assert redact_proxy_observability_value("proxy_location", rendered) is rendered


def test_proxy_observability_renderer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_renderer(field: ProxyObservabilityField, value: object) -> RedactedProxyLogValue:
        assert field is ProxyObservabilityField.PROXY_LOCATION
        del value
        raise RuntimeError("synthetic renderer failure")

    monkeypatch.setattr(log_redaction, "_proxy_observability_renderer", fail_renderer)

    rendered = redact_proxy_observability_value(
        "proxy_location", "http://user:synthetic-secret@token.proxy.example:8080"
    )

    assert rendered == REDACTED
    assert isinstance(rendered, RedactedProxyLogValue)


def test_proxy_observability_renderer_rejects_unmarked_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsafe_renderer(field: ProxyObservabilityField, value: object) -> str:
        del field
        del value
        return "http://user:synthetic-secret@token.proxy.example:8080"

    monkeypatch.setattr(log_redaction, "_proxy_observability_renderer", unsafe_renderer)

    rendered = redact_proxy_observability_value("proxy_location", "proxy_location")

    assert rendered == REDACTED
    assert isinstance(rendered, RedactedProxyLogValue)


def test_proxy_field_families_render_by_semantics() -> None:
    out = redact_sensitive_event_fields(
        None,
        "warning",
        {
            "proxy_location": "RESIDENTIAL",
            "proxy_host": _SYNTHETIC_PROXY_HOST,
            "proxy_url": _SYNTHETIC_PROXY_URL,
            "geo_target": GeoTarget(country="US", subdivision="CA", city="Chicago"),
        },
    )  # type: ignore[arg-type]

    assert out["proxy_location"] == "RESIDENTIAL"
    assert re.fullmatch(r"proxy_host:[0-9a-f]{12}", out["proxy_host"])
    assert re.fullmatch(r"proxy_url:[0-9a-f]{12}", out["proxy_url"])
    assert re.fullmatch(r"geo_target:US:[0-9a-f]{12}", out["geo_target"])
    assert _SYNTHETIC_PROXY_CREDENTIAL not in json.dumps(out)
    assert _SYNTHETIC_PROXY_HOST not in json.dumps(out)


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


def test_defense_in_depth_redactor_only_redacts_registered_long_and_short_credentials() -> None:
    """Redactor-only coverage, not parity proof for either CodeBlock engine's failure path."""
    credentials = (_FAKE_CREDENTIAL, "587")
    for credential in credentials:
        _register_credential(credential)
    event = {
        "event": f'CodeBlock failure contained "{_FAKE_CREDENTIAL}" and PIN "587"',
        "selector": "#password",
    }
    out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]
    assert all(credential not in out["event"] for credential in credentials)
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
    """Nested kwargs are serialized, so registered secrets must be redacted recursively."""
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


def test_redacts_authorization_bearer_header_value() -> None:
    event = {"headers_line": "Authorization: Bearer eyJhbGciOi.JIUzI1NiJ9.sig123"}
    out = redact_bearer_tokens(None, "error", event)  # type: ignore[arg-type]
    assert "eyJhbGciOi" not in out["headers_line"]
    assert out["headers_line"] == "Authorization: Bearer <redacted>"


def test_redacts_bare_bearer_credential_in_exception_string() -> None:
    event = {"event": "HTTPError 401 while calling api with Bearer sk-abc123DEF456ghi"}
    out = redact_bearer_tokens(None, "exception", event)  # type: ignore[arg-type]
    assert "sk-abc123DEF456ghi" not in out["event"]
    assert "Bearer <redacted>" in out["event"]


def test_bearer_prose_is_not_redacted() -> None:
    event = {"event": "Bearer authentication required"}
    out = redact_bearer_tokens(None, "warning", event)  # type: ignore[arg-type]
    assert out["event"] == "Bearer authentication required"


def test_masks_top_level_sensitive_kwarg() -> None:
    event = {"event": "auth failed", "authorization": "Bearer eyJabc.def.ghi"}
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["authorization"] == _REDACTED
    assert out["event"] == "auth failed"


def test_masks_authorization_header_inside_headers_kwarg() -> None:
    event = {
        "event": "webhook failed",
        "headers": {"Authorization": "Bearer secrettoken", "Content-Type": "application/json"},
    }
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["headers"]["Authorization"] == _REDACTED
    assert out["headers"]["Content-Type"] == "application/json"


def test_masks_extra_http_headers_in_task_payload() -> None:
    event = {
        "event": "Failed to send webhook",
        "task": {
            "task_id": "tsk_1",
            "url": "https://example.com",
            "extra_http_headers": {"X-Custom-Auth": "Bearer customsecret"},
        },
    }
    out = redact_sensitive_event_fields(None, "exception", event)  # type: ignore[arg-type]
    # Whole customer header dict is masked regardless of its inner (custom) key names.
    assert out["task"]["extra_http_headers"] == _REDACTED
    assert out["task"]["task_id"] == "tsk_1"
    assert out["task"]["url"] == "https://example.com"


def test_masks_deeply_nested_credentials() -> None:
    event = {"event": "x", "payload": {"user": {"name": "bob", "credentials": [{"token": "abc123"}]}}}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out["payload"]["user"]["credentials"][0]["token"] == _REDACTED
    assert out["payload"]["user"]["name"] == "bob"


def test_passes_through_non_sensitive_kwargs() -> None:
    event = {"event": "ok", "count": 3, "task_id": "tsk_1", "status": "failed"}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out == event


def test_leaves_plain_string_values_untouched() -> None:
    # Plain string kwargs are handled by the bearer / registered-secret redactors,
    # not this one; it must not rewrite them (e.g. strip artifact-URL queries).
    event = {"event": "GET /v1/artifacts/a1/content?sig=xyz", "note": "Bearer abc123DEF"}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out == event


def test_proxy_values_are_redacted_at_the_structlog_boundary() -> None:
    custom_proxy = {"url": "http://user:synthetic-secret@token.proxy.example:8080"}
    event = {
        "event": "synthetic proxy event",
        "proxy_location": custom_proxy,
        "profile_proxy_location": custom_proxy,
        "proxy_host": "token.proxy.example",
        "proxy_location_type": "dict",
        "input_proxy_location_present": True,
        "payload": {"effective_proxy_location": custom_proxy},
    }

    out = redact_sensitive_event_fields(None, "warning", event)  # type: ignore[arg-type]
    dumped = json.dumps(out)

    assert "synthetic-secret" not in dumped
    assert "token.proxy.example" not in dumped
    assert re.fullmatch(r"custom_url:[0-9a-f]{12}", out["proxy_location"])
    assert out["profile_proxy_location"] == out["proxy_location"]
    assert re.fullmatch(r"proxy_host:[0-9a-f]{12}", out["proxy_host"])
    assert out["payload"]["effective_proxy_location"] == out["proxy_location"]
    assert out["proxy_location_type"] == "dict"
    assert out["input_proxy_location_present"] is True


def test_proxy_boundary_does_not_render_a_marked_value_twice() -> None:
    rendered = redact_proxy_location({"url": "http://user:synthetic-secret@token.proxy.example:8080"})

    out = redact_sensitive_event_fields(None, "info", {"proxy_location": rendered})  # type: ignore[arg-type]

    assert out["proxy_location"] is rendered


def test_native_pipeline_redacts_proxy_output_and_context_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    context = SkyvernContext()

    rendered = _emit_native_json_log(
        capsys,
        context,
        proxy_host=_SYNTHETIC_PROXY_HOST,
        proxy_url=_SYNTHETIC_PROXY_URL,
    )
    persisted = json.dumps(context.log)

    for output in (rendered, persisted):
        assert _SYNTHETIC_PROXY_CREDENTIAL not in output
        assert _SYNTHETIC_PROXY_HOST not in output
    assert re.fullmatch(r"proxy_host:[0-9a-f]{12}", context.log[0]["proxy_host"])


def test_native_pipeline_fails_closed_beyond_the_depth_cap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    context = SkyvernContext()
    payload = _nested_mapping({"proxy_url": _SYNTHETIC_PROXY_URL})

    rendered = _emit_native_json_log(capsys, context, payload=payload)
    persisted = json.dumps(context.log)

    for output in (rendered, persisted):
        assert _SYNTHETIC_PROXY_CREDENTIAL not in output
        assert _SYNTHETIC_PROXY_HOST not in output
    assert REDACTED in persisted


def test_webhook_failure_event_is_fully_redacted_through_processors() -> None:
    # Composed in the exact order setup_logger installs them: redact_bearer_tokens
    # first (top-level strings only), then redact_sensitive_event_fields (recurses
    # into nested containers). ``x_trace`` holds a bearer under a NON-sensitive key
    # name, so nothing but the field redactor's nested-string handling can catch it —
    # the case a reverse-order / sensitive-key-only test would have missed.
    event = {
        "event": "Failed to send webhook",
        "headers": {
            "Authorization": "Bearer eyJhbGci.payload.sig",
            "x_trace": "retried with Bearer benignkey1234tok",
        },
        "payload": {
            "navigation_goal": "log in",
            "extra_http_headers": {"Authorization": "Bearer topsecrettoken123"},
        },
        "raw": "POST failed, sent header Authorization: Bearer leakedtoken12345",
    }
    out = redact_bearer_tokens(None, "exception", event)  # type: ignore[arg-type]
    out = redact_sensitive_event_fields(None, "exception", out)  # type: ignore[arg-type]
    dumped = json.dumps(out)
    assert "eyJhbGci" not in dumped
    assert "topsecrettoken123" not in dumped
    assert "leakedtoken12345" not in dumped
    assert "benignkey1234tok" not in dumped
    assert out["headers"]["Authorization"] == _REDACTED
    assert out["headers"]["x_trace"] == "retried with Bearer <redacted>"
    assert out["payload"]["extra_http_headers"] == _REDACTED
    assert "Bearer <redacted>" in out["raw"]


def test_logging_works_when_starlette_is_absent() -> None:
    """Core `pip install skyvern` has no starlette — the field redactor must not need it."""
    script = textwrap.dedent(
        """
        import sys

        class _BlockStarlette:
            def find_spec(self, name, path=None, target=None):
                if name == "starlette" or name.startswith("starlette."):
                    raise ModuleNotFoundError(f"No module named '{name}'")
                return None

        sys.meta_path.insert(0, _BlockStarlette())

        import structlog
        from skyvern.forge.sdk.forge_log import setup_logger

        setup_logger()
        structlog.get_logger().error("boom", headers={"Authorization": "Bearer test-token-123"})
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "boom" in output, f"log line was never emitted: {output}"
    assert "test-token-123" not in output


def test_tolerates_non_string_dict_keys() -> None:
    event = {"event": "x", "status_counts": {200: 5}, "nested": {"by_code": {404: 1}}}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out["status_counts"] == {200: 5}
    assert out["nested"]["by_code"] == {404: 1}


def test_masks_cookie_and_x_api_key_alongside_authorization() -> None:
    event = {
        "event": "api.raw_request",
        "headers": {"Authorization": "Bearer t", "Cookie": "session=abc123", "X-Api-Key": "key-abc123"},
    }
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["headers"] == {"Authorization": _REDACTED, "Cookie": _REDACTED, "X-Api-Key": _REDACTED}


def test_masks_cdp_connect_headers_and_cached_totp() -> None:
    event = {
        "event": "Cached TOTP has expired during multi-field sequence",
        "cached_totp": "123456",
        "cdp_connect_headers": {"X-Provider-Auth": "test-token-123"},
    }
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["cached_totp"] == _REDACTED
    assert out["cdp_connect_headers"] == _REDACTED


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Standard-base64 (`+ / =`) and opaque (`~`) tokens used to truncate the match at
        # the first such character, leaving the token TAIL in the line — and because the
        # match consumed the literal `Bearer`, the credential regex found nothing to clean.
        ("wss://h/v1/stream?token=Bearer%20abcd+efgh/ijkl==", "wss://h/v1/stream?token=<redacted>"),
        ("connect failed ?token=Bearer sk~opaque+tail/here==", "connect failed ?token=<redacted>"),
        ("?token=Bearer%20eyJhbGciOi.JIUzI1NiJ9.sig-_123&client_id=abc", "?token=<redacted>&client_id=abc"),
    ],
)
def test_redacts_whole_bearer_token_in_query_string(url: str, expected: str) -> None:
    out = redact_bearer_tokens(None, "error", {"event": url})  # type: ignore[arg-type]
    assert out["event"] == expected


class _FakeTaskModel(BaseModel):
    task_id: str
    extra_http_headers: dict[str, str] | None = None


class _UndumpableProxyModel(BaseModel):
    proxy_url: str

    def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("synthetic model_dump failure")


def test_masks_sensitive_fields_inside_a_model_kwarg() -> None:
    """A model kwarg is rendered in full by the formatter, so it has to be redacted too."""
    event = {"event": "x", "task": _FakeTaskModel(task_id="tsk_1", extra_http_headers={"X-Auth": "test-token-123"})}
    out = redact_sensitive_event_fields(None, "exception", event)  # type: ignore[arg-type]
    assert "test-token-123" not in json.dumps(out, default=str)
    assert out["task"]["extra_http_headers"] == _REDACTED
    assert out["task"]["task_id"] == "tsk_1"


def test_model_dump_failure_fails_closed() -> None:
    model = _UndumpableProxyModel(proxy_url=_SYNTHETIC_PROXY_URL)

    assert redact_sensitive_fields(model) == REDACTED


def test_depth_cap_redacts_remaining_containers_and_json_shaped_strings() -> None:
    raw = {"proxy_url": _SYNTHETIC_PROXY_URL}

    assert redact_sensitive_fields(raw, 21) == REDACTED
    assert redact_sensitive_fields([raw], 21) == REDACTED
    assert redact_sensitive_fields(json.dumps(raw), 21) == REDACTED
    assert redact_sensitive_fields("ordinary text", 21) == "ordinary text"


def test_masks_sensitive_fields_inside_tuple_and_set_kwargs() -> None:
    event = {"event": "x", "pair": ({"token": "test-token-123"},), "names": {"alpha", "beta"}}
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["pair"] == ({"token": _REDACTED},)
    assert out["names"] == {"alpha", "beta"}


def test_artifact_url_query_stripping_stays_linear_on_long_runs() -> None:
    """The old optional `scheme://` prefix backtracked at every start position (~5 s here)."""
    payload = "A" * 100_000
    start = time.perf_counter()
    assert strip_artifact_url_query(payload) == payload
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://cdn.test/v1/artifacts/a1/content?exp=1&sig=z tail", "https://cdn.test/v1/artifacts/a1/content tail"),
        ("/v1/artifacts/a1/content/?kid=k", "/v1/artifacts/a1/content/"),
        ("see <https://h/v1/artifacts/a1/content?sig=x>", "see <https://h/v1/artifacts/a1/content>"),
        ("no artifact url here ?sig=x", "no artifact url here ?sig=x"),
    ],
)
def test_artifact_url_query_stripping_behavior_is_unchanged(value: str, expected: str) -> None:
    assert strip_artifact_url_query(value) == expected


class _FakeAction(BaseModel):
    action_id: str
    action_type: str
    element_id: str
    reasoning: str


def test_action_compaction_runs_before_field_redaction() -> None:
    """Redaction expands models into full dicts, so compaction (a volume control) must run first."""
    action = _FakeAction(action_id="act_1", action_type="click", element_id="el_1", reasoning="x" * 500)
    event: dict = {"event": "executing action", "action": action}

    out = redact_sensitive_event_fields(None, "info", compact_action_objects(None, "info", event))  # type: ignore[arg-type]

    assert out["action"] == {"id": "act_1", "type": "click", "element_id": "el_1"}


def test_setup_logger_pins_redactor_processor_order() -> None:
    """The console/JSON chains repeat the same redactors with no comment. Pin their
    relative order so a reorder — e.g. running the field redactor before compaction,
    or dropping bearer redaction — fails loudly rather than silently leaking."""
    root = logging.getLogger()
    saved_config = structlog.get_config()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        setup_logger()
        names = [getattr(p, "__name__", type(p).__name__) for p in structlog.get_config()["processors"]]
        assert "redact_bearer_tokens" in names
        assert "compact_action_objects" in names
        assert "redact_sensitive_event_fields" in names
        # Bearer redaction runs on top-level strings; the field redactor recurses into
        # nested containers. Both must precede the field redactor for the chain to be total.
        assert names.index("redact_bearer_tokens") < names.index("redact_sensitive_event_fields")
        assert names.index("compact_action_objects") < names.index("redact_sensitive_event_fields")
    finally:
        structlog.configure(**saved_config)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_input_text_action_otp_value_is_masked_through_field_redactor() -> None:
    """model_dump bypasses InputTextAction.__repr__ OTP masking, so previous_action=
    kwargs would render the live code / identifier in the clear without re-applying it."""
    from skyvern.webeye.actions.actions import InputTextAction

    action = InputTextAction(
        element_id="el_1",
        text="483920",
        totp_code_required=True,
        totp_identifier="user@example.com",
        totp_url="https://otp.example.com/code",
    )
    out = redact_sensitive_fields(action)
    dumped = json.dumps(out, default=str)
    assert "483920" not in dumped
    assert "user@example.com" not in dumped
    assert "otp.example.com" not in dumped
    assert out["text"] == "<redacted otp value>"
    assert out["totp_identifier"] == _REDACTED
    assert out["totp_url"] == _REDACTED


def test_verification_code_field_is_masked() -> None:
    """handler.py logs verification_code=action.verification_code at INFO."""
    event = {"event": "Setting verification code in skyvern context", "verification_code": "998877"}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out["verification_code"] == _REDACTED


def test_nested_bearer_under_non_sensitive_key_is_redacted() -> None:
    """A bearer inside a string under a benign key name is caught only by the field
    redactor's nested-string handling — the middleware would not classify the key."""
    event = {"event": "http request", "headers": {"X-Trace-Note": "sent Bearer sk-abc123DEF456ghiJKL"}}
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert "sk-abc123DEF456ghiJKL" not in json.dumps(out)
    assert out["headers"]["X-Trace-Note"] == "sent Bearer <redacted>"


def test_proxy_authorization_and_set_cookie_header_keys_are_masked() -> None:
    event = {
        "event": "Executing HTTP request",
        "headers": {"Proxy-Authorization": "Bearer sk-proxytoken", "Set-Cookie": "session=secret; HttpOnly"},
    }
    out = redact_sensitive_event_fields(None, "info", event)  # type: ignore[arg-type]
    assert out["headers"]["Proxy-Authorization"] == _REDACTED
    assert out["headers"]["Set-Cookie"] == _REDACTED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # all-alpha token in an Authorization header context — no non-alpha char, so the
        # bare-Bearer prose heuristic skips it, but the header context redacts it anyway.
        ("Authorization: Bearer abcdefghijklmnopqrst", "Authorization: Bearer <redacted>"),
        # below the 8-char minimum the bare heuristic also skips it; header context does not.
        ("authorization: Bearer short12", "authorization: Bearer <redacted>"),
        # dict-repr header shape.
        ("'proxy-authorization': 'Bearer plaintokenvalue'", "'proxy-authorization': 'Bearer <redacted>'"),
    ],
)
def test_authorization_header_bearer_redacted_regardless_of_token_shape(text: str, expected: str) -> None:
    assert redact_bearer_tokens_in_text(text) == expected


def test_bare_all_alpha_bearer_prose_is_preserved() -> None:
    """With no header context the prose heuristic must still leave 'Bearer <word>' alone."""
    assert redact_bearer_tokens_in_text("please use Bearer authentication") == "please use Bearer authentication"


def test_non_dict_mapping_is_redacted() -> None:
    """isinstance(obj, dict) missed httpx/starlette Headers, MappingProxyType, CIMultiDict."""
    mapping = MappingProxyType({"Authorization": "Bearer secrettok", "trace_id": "t1"})
    out = redact_sensitive_fields(mapping)
    assert out["Authorization"] == _REDACTED
    assert out["trace_id"] == "t1"


def test_cyclic_container_redaction_is_bounded() -> None:
    """Two self-references used to fan out to O(breadth^21) rebuilds (~6.5 s); the id()
    memo keeps the walk linear."""
    node: dict = {"token": "leaked-secret", "name": "outer"}
    node["self"] = node
    node["also_self"] = node
    start = time.perf_counter()
    out = redact_sensitive_fields(node)
    assert time.perf_counter() - start < 1.0
    assert out["token"] == _REDACTED
    assert out["name"] == "outer"


def test_field_redactor_fails_closed_when_a_container_raises() -> None:
    """A caller-supplied container whose iteration raises must not take down the log
    call; the kwarg fails closed to the redaction placeholder instead."""

    class _ExplodingMapping(dict):
        def items(self):  # type: ignore[override]
            raise RuntimeError("boom")

    event = {"event": "x", "payload": _ExplodingMapping({"token": "secret"}), "keep": "ok"}
    out = redact_sensitive_event_fields(None, "error", event)  # type: ignore[arg-type]
    assert out["payload"] == _REDACTED
    assert out["keep"] == "ok"
