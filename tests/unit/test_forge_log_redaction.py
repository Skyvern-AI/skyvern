import asyncio
import copy
import enum
import io
import json
import logging
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from logging.handlers import BufferingHandler
from types import MappingProxyType
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
import structlog
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge import app, log_redaction
from skyvern.forge.log_redaction import (
    REDACTED,
    is_proxy_observability_key,
    redact_bearer_tokens_in_text,
    redact_proxy_observability_value,
    redact_sensitive_fields,
    strip_artifact_url_query,
)
from skyvern.forge.sdk import log_artifacts
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import secret_scrub
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import (
    CODEBLOCK_LOG_REDACTED,
    _GeneratedLogValue,
    add_filename_section,
    add_log_context,
    codeblock_parameter_log_redaction,
    compact_action_objects,
    redact_bearer_tokens,
    redact_codeblock_parameters,
    redact_registered_log_payload,
    redact_registered_secrets,
    redact_sensitive_event_fields,
    render_bounded_json,
    setup_logger,
)
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.skyvern_json_encoder import SkyvernJSONLogEncoder
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


@pytest.fixture
def registered_log_stream(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[io.StringIO]:
    monkeypatch.setattr(settings, "JSON_LOGGING", getattr(request, "param", True))
    root = logging.getLogger()
    saved_config = structlog.get_config()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    stream = io.StringIO()
    try:
        setup_logger()
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(stream)
        yield stream
    finally:
        structlog.configure(**saved_config)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


class _DiagnosticModel(BaseModel):
    detail: str
    number: int


class _RenderedDiagnostic:
    def __init__(self, detail: str) -> None:
        self.detail = detail

    def __structlog__(self) -> dict[str, str]:
        return {"detail": self.detail}


class _ProtocolNamedDiagnostic(BaseModel):
    event: str
    msg: str
    level: str
    warning: str


@pytest.mark.parametrize("registered_log_stream", [True, False], indirect=True)
@pytest.mark.parametrize("route", ["native", "stdlib", "downstream_renamer"])
@pytest.mark.parametrize("secret", ["event", "msg", "level", "warning", "warn"])
def test_registered_protocol_words_preserve_emitted_messages_and_severity(
    registered_log_stream: io.StringIO, route: str, secret: str
) -> None:
    context = SkyvernContext(runtime_secret_values={secret})
    payload = dict.fromkeys(("event", "msg", "level", "warning"), secret)
    original = payload.copy()
    model = _ProtocolNamedDiagnostic(**payload)
    fields = {
        "payload": payload,
        "model": model,
        f"user:{secret}": secret,
        "url": f"https://example.invalid/verify?value={secret}",
    }
    logger = structlog.get_logger("skyvern.test.protocol")
    if route == "downstream_renamer":
        # Reuse the native chain, then replace its final formatter seam with an
        # EventRenamer/JSON renderer. This models a downstream consumer without
        # importing cloud or assuming that lack of an exception proves success.
        config = structlog.get_config()
        processors = [
            processor
            for processor in config["processors"][:-1]
            if not isinstance(processor, structlog.processors.CallsiteParameterAdder)
            and processor is not add_filename_section
        ]
        if not settings.JSON_LOGGING:
            processors.append(structlog.processors.EventRenamer("msg"))
        processors.append(render_bounded_json)
        logger = structlog.wrap_logger(
            structlog.PrintLogger(registered_log_stream),
            processors=processors,
            wrapper_class=config["wrapper_class"],
        )

    def emit(message: str, exc_info: bool = False) -> None:
        if route == "stdlib":
            logging.getLogger("skyvern.test.protocol").warning(message, extra=fields, exc_info=exc_info)
        else:
            logger.warning(message, **fields, exc_info=exc_info)

    with skyvern_context.scoped(context):
        emit("Diagnostic emission")
        try:
            raise ValueError(secret)
        except ValueError:
            emit(f"Diagnostic emission: {secret}", exc_info=True)
    context.runtime_secret_values.clear()
    assert payload == original
    assert model.model_dump() == original
    assert fields[f"user:{secret}"] == secret
    payload["late"] = secret
    model.event = "changed after capture"

    masked_payload = {key.replace(secret, REDACTED_SECRET_PLACEHOLDER): REDACTED_SECRET_PLACEHOLDER for key in original}
    expected_messages = ["Diagnostic emission", f"Diagnostic emission: {REDACTED_SECRET_PLACEHOLDER}"]
    emitted = re.sub(r"\x1b\[[0-9;]*m", "", registered_log_stream.getvalue())
    record_groups = []
    if settings.JSON_LOGGING or route == "downstream_renamer":
        record_groups.append([json.loads(line) for line in emitted.splitlines()])
    else:
        for message in expected_messages:
            assert message in emitted
        assert len(re.findall(r"\[warning\s*\]", emitted)) == 2
        assert f"payload={masked_payload!r}" in emitted
        assert f"model={masked_payload!r}" in emitted
        assert f"user:{REDACTED_SECRET_PLACEHOLDER}={REDACTED_SECRET_PLACEHOLDER}" in emitted
        assert f"url=https://example.invalid/verify?value={REDACTED_SECRET_PLACEHOLDER}" in emitted
        assert f"ValueError: {REDACTED_SECRET_PLACEHOLDER}" in emitted
        assert f"Diagnostic emission: {secret}" not in emitted
        assert f"ValueError: {secret}" not in emitted
    if route != "stdlib":
        record_groups.append(json.loads(json.dumps(context.log, cls=SkyvernJSONLogEncoder)))
    for records in record_groups:
        assert len(records) == 2
        assert [record.get("msg", record.get("event")) for record in records] == expected_messages
        for record in records:
            assert record["level"] == "warning"
            assert record["payload"] == masked_payload
            assert record["model"] == masked_payload
            assert record[f"user:{REDACTED_SECRET_PLACEHOLDER}"] == REDACTED_SECRET_PLACEHOLDER
            assert record["url"] == f"https://example.invalid/verify?value={REDACTED_SECRET_PLACEHOLDER}"
        assert records[1]["exception"].endswith(f"ValueError: {REDACTED_SECRET_PLACEHOLDER}")


@pytest.mark.parametrize("method", ["debug", "info", "warning", "warn", "error", "exception", "critical"])
def test_registered_processor_preserves_only_method_derived_level(method: str) -> None:
    logger = logging.getLogger("skyvern.test.protocol")
    level = structlog.stdlib.add_log_level(logger, method, {})["level"]
    context = SkyvernContext(runtime_secret_values={"event", "msg", "level", level})
    event = {
        "event": "Diagnostic emission: event",
        "msg": "Diagnostic emission: msg",
        "level": level,
        level: "caller field",
        "payload": {"event": "event", "msg": "msg", "level": level},
    }
    original = copy.deepcopy(event)
    with skyvern_context.scoped(context):
        out = redact_registered_secrets(logger, method, event)
        # A familiar level under the wrong method is still data. So is a value
        # from a custom method, whose provenance this processor cannot establish.
        unexpected = redact_registered_secrets(logger, "info" if level != "info" else "warning", event)
        custom = redact_registered_secrets(logger, "custom", event)
    assert event == original
    assert out["event"] == f"Diagnostic emission: {REDACTED_SECRET_PLACEHOLDER}"
    assert out["msg"] == f"Diagnostic emission: {REDACTED_SECRET_PLACEHOLDER}"
    assert out["level"] == level
    assert out[REDACTED_SECRET_PLACEHOLDER] == "caller field"
    assert out["payload"] == {REDACTED_SECRET_PLACEHOLDER: REDACTED_SECRET_PLACEHOLDER}
    assert unexpected["level"] == REDACTED_SECRET_PLACEHOLDER
    assert custom["level"] == REDACTED_SECRET_PLACEHOLDER


@pytest.mark.parametrize("registered_log_stream", [True, False], indirect=True)
@pytest.mark.parametrize(("global_mask", "workflow_mask"), [(False, True), (True, False), (False, False)])
def test_registered_secrets_protect_output_and_log_artifacts_after_teardown(
    monkeypatch: pytest.MonkeyPatch,
    registered_log_stream: io.StringIO,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    global_mask: bool,
    workflow_mask: bool,
) -> None:
    workflow_value = 'synthetic-workflow-credential<& /"\\\n'
    runtime_value = "synthetic-runtime-credential"
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", global_mask)
    manager = workflow_context_manager_factory(
        workflow_run_id="wr_diagnostic",
        mask_secrets=workflow_mask,
        secrets={"long": workflow_value, "short": "q7", "pin": "587", "number": "483920", "empty": ""},
    )
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", manager)
    context = SkyvernContext(workflow_run_id="wr_diagnostic")
    context.register_secret_value(runtime_value)
    _register_credential(_FAKE_CREDENTIAL)
    model = _DiagnosticModel(detail=runtime_value, number=483920)
    opaque = _RenderedDiagnostic(runtime_value)
    variants = secret_scrub.encoded_secret_variants(workflow_value)
    response = {
        f"key:{workflow_value}": {"models": [model]},
        "forms": variants,
        "numbers": [483920, 483920.0, Decimal("483920"), 587],
        "short": "q7",
        "safe": [42, 1.25, True, False, None],
        "copilot": _FAKE_CREDENTIAL,
    }
    original = copy.deepcopy(response)
    fields = {
        f"field:{workflow_value}": "safe diagnostic",
        "response": response,
        "details": opaque,
        "reason": ValueError(runtime_value),
        "url": f"https://example.invalid/verify?value={variants[-1]}",
    }
    with skyvern_context.scoped(context):
        # Diagnostic protection must not change the existing artifact opt-out.
        assert manager.get_secret_values_for_run(context.workflow_run_id) == set()
        try:
            raise ValueError(f"Malformed response: {workflow_value} {runtime_value}")
        except ValueError:
            structlog.get_logger("skyvern.test.registered").warning("rejected codes", exc_info=True, **fields)
            logging.getLogger("skyvern.test.foreign_registered").warning(
                "rejected codes %s", runtime_value, exc_info=True, extra=fields
            )
        assert response == original
        assert model.detail == runtime_value
        assert opaque.detail == runtime_value
        assert manager.get_secret_values_for_run(context.workflow_run_id) == set()

    manager.workflow_run_contexts.clear()
    context.runtime_secret_values.clear()
    secret_scrub._SESSION_SCRUB_VALUES.clear()
    # Later caller mutations must not change the captured event or its artifact.
    response["late"] = runtime_value
    model.detail = "changed after capture"
    opaque.detail = "changed after capture"
    persisted = json.dumps(context.log, cls=SkyvernJSONLogEncoder)
    emitted = registered_log_stream.getvalue()
    for output in (emitted, persisted):
        assert "synthetic-workflow-credential" not in output
        assert runtime_value not in output
        assert _FAKE_CREDENTIAL not in output
        assert "changed after capture" not in output
        assert "rejected codes" in output
        assert "Malformed response:" in output
        assert REDACTED_SECRET_PLACEHOLDER in output

    captured = next(entry for entry in context.log if "response" in entry)
    records = [captured]
    if settings.JSON_LOGGING:
        records.extend(json.loads(line) for line in emitted.splitlines() if '"response"' in line)
        assert len(records) == 3  # One captured native event and both emitted events.
    else:
        assert "skyvern.test.foreign_registered" in emitted
    for record in records:
        assert record[f"field:{REDACTED_SECRET_PLACEHOLDER}"] == "safe diagnostic"
        redacted = record["response"]
        assert redacted[f"key:{REDACTED_SECRET_PLACEHOLDER}"]["models"] == [
            {"detail": REDACTED_SECRET_PLACEHOLDER, "number": REDACTED_SECRET_PLACEHOLDER}
        ]
        assert redacted["forms"] == [REDACTED_SECRET_PLACEHOLDER] * len(variants)
        assert redacted["numbers"] == [REDACTED_SECRET_PLACEHOLDER] * 4
        assert redacted["short"] == REDACTED_SECRET_PLACEHOLDER
        assert redacted["safe"] == [42, 1.25, True, False, None]
        assert "late" not in redacted
        assert record["details"] == {"detail": REDACTED_SECRET_PLACEHOLDER}


def test_registered_processor_copies_keys_containers_and_preserves_unmatched_scalars() -> None:
    context = SkyvernContext()
    for value in ("q7", "587", "483920", "synthetic-long-value", "synthetic-long"):
        context.register_secret_value(value)
    nested = {"key:q7": ["synthetic-long-value", 483920], 587: "ordinary"}
    event = {
        "event": "diagnostic",
        "mapping": MappingProxyType(nested),
        "tuple": ("q7", 8),
        "set": {"q7", "ordinary"},
        "frozen": frozenset({"q7"}),
        "safe": [9, 0.5, Decimal("0.125"), True, False, None],
    }
    with skyvern_context.scoped(context):
        out = redact_registered_secrets(None, "info", event)  # type: ignore[arg-type]
    assert out is not event
    assert out["mapping"] == {
        f"key:{REDACTED_SECRET_PLACEHOLDER}": [REDACTED_SECRET_PLACEHOLDER, REDACTED_SECRET_PLACEHOLDER],
        REDACTED_SECRET_PLACEHOLDER: "ordinary",
    }
    assert out["tuple"] == (REDACTED_SECRET_PLACEHOLDER, 8)
    assert out["set"] == {REDACTED_SECRET_PLACEHOLDER, "ordinary"}
    assert out["frozen"] == frozenset({REDACTED_SECRET_PLACEHOLDER})
    assert out["safe"] == event["safe"]
    assert out["safe"] is not event["safe"]
    assert nested == {"key:q7": ["synthetic-long-value", 483920], 587: "ordinary"}
    assert event["tuple"] == ("q7", 8)
    assert event["set"] == {"q7", "ordinary"}


@pytest.mark.parametrize("encoded", [False, True])
def test_context_suffix_scrubs_complete_caller_values_without_mutating_input(encoded: bool) -> None:
    secret = "<" * 100 + "X" if encoded else "s" * 256 + "X"
    emitted = quote(secret, safe="") if encoded else secret
    task_id = "tsk_" + "g" * 300
    context = SkyvernContext(task_id=task_id, runtime_secret_values={secret, "g"})
    event = {"msg": "notice", "organization_name": emitted, "browser_session_id": emitted, "workflow_id": "u" * 300}
    original = copy.deepcopy(event)
    with skyvern_context.scoped(context):
        result = add_log_context(logging.getLogger(), "warning", event)
        result = redact_registered_secrets(logging.getLogger(), "warning", result)
    assert event == original
    assert emitted[:256] not in repr(result)
    assert result["or[REDACTED_SECRET]anization_name"] == REDACTED_SECRET_PLACEHOLDER
    assert result["browser_session_id"] == REDACTED_SECRET_PLACEHOLDER
    assert result["task_id"] == task_id
    assert f"task_id={task_id[:256]}" in result["msg"]
    assert f"workflow_id={'u' * 256}" in result["msg"]
    assert "g" * 257 not in result["msg"] and "u" * 257 not in result["msg"]


def test_export_payload_scrubs_copied_provenance_and_preserves_sliced_generated_parts() -> None:
    context = SkyvernContext(task_id="tsk_123", runtime_secret_values={"123", "id"})
    with skyvern_context.scoped(context):
        event = add_log_context(None, "warning", {"msg": "caller 123"})  # type: ignore[arg-type]
        message = event["msg"]
        assert type(message) is _GeneratedLogValue
        attributes = {
            "task_id": event["task_id"],
            "payload": dict(event),
            "copied": event["task_id"],
            "forged_id": "tsk_123",
        }
        original = copy.deepcopy(attributes)
        # OTel slices attributes even when the configured limit exceeds their length.
        body, result = redact_registered_log_payload(message[:4096], attributes)
        truncated, _ = redact_registered_log_payload(message[:-1], {})
    assert body == "caller [REDACTED_SECRET] | task_id=tsk_123"
    assert truncated == "caller [REDACTED_SECRET] | task_id=tsk_12"
    assert type(body) is str and type(result["task_id"]) is str
    assert result["task_id"] == "tsk_123"
    assert result["copied"] == "tsk_[REDACTED_SECRET]"
    assert result["forged_[REDACTED_SECRET]"] == "tsk_[REDACTED_SECRET]"
    assert "123" not in json.dumps(result["payload"])
    assert attributes == original and message.startswith("caller 123")


@pytest.mark.asyncio
@pytest.mark.parametrize("registered_log_stream", [True, False], indirect=True)
async def test_generated_metadata_survives_short_secrets_and_actual_artifact_selection(
    monkeypatch: pytest.MonkeyPatch,
    registered_log_stream: io.StringIO,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    ids = {
        "organization_id": "o_2012309",
        "workflow_run_id": "wr_2012309",
        "task_id": "tsk_2012309",
        "step_id": "stp_2012309",
    }
    manager = workflow_context_manager_factory(
        workflow_run_id=ids["workflow_run_id"], mask_secrets=False, secrets={"month": "09", "pin": "123"}
    )
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    monkeypatch.setattr(settings, "ENABLE_LOG_ARTIFACTS", True)
    context = SkyvernContext(
        organization_id=ids["organization_id"],
        workflow_run_id=ids["workflow_run_id"],
        task_id=ids["task_id"],
        step_id=ids["step_id"],
        organization_name="09",
        copilot_session_id="123",
        browser_session_id="pbs_09",
        runtime_secret_values={"20"},
    )
    payload = {**ids, "timestamp": "2020-09-20T09:12:30Z", "number": 123}
    original = payload.copy()
    fields = {"payload": payload, "workflow_id": "spoof-09", "task_id": "spoof-123", "timestamp": "spoof-20"}
    masked = REDACTED_SECRET_PLACEHOLDER
    expected_payload = {
        key: value.replace("123", masked).replace("09", masked).replace("20", masked)
        if isinstance(value, str)
        else masked
        for key, value in original.items()
    }
    expected_message = f"correlated {masked} {masked} {masked}"
    root = logging.getLogger()
    buffer = BufferingHandler(10)
    root.addHandler(buffer)
    formatter = root.handlers[0].formatter
    assert formatter is not None
    static_stream = io.StringIO()
    config = structlog.get_config()
    processors = [
        processor
        for processor in config["processors"][:-1]
        if not isinstance(processor, structlog.processors.CallsiteParameterAdder)
        and processor is not add_filename_section
    ]
    if not settings.JSON_LOGGING:
        # Match the static-script logger's console-to-JSON renderer seam.
        processors.extend((structlog.processors.EventRenamer("msg"), add_log_context))
    processors.append(render_bounded_json)
    static_logger = structlog.wrap_logger(
        structlog.PrintLogger(static_stream), processors=processors, wrapper_class=config["wrapper_class"]
    )
    with skyvern_context.scoped(context), skyvern_context.workflow_log_attempt(ids["workflow_run_id"], 123):
        structlog.get_logger("skyvern.test.correlation.native").warning("correlated 09 123 20", **fields)
        logging.getLogger("skyvern.test.correlation.foreign").warning("correlated 09 123 20", extra=fields)
        static_logger.warning("correlated 09 123 20", **fields)
    assert payload == original
    payload["late"] = "09"
    manager.workflow_run_contexts.clear()
    context.runtime_secret_values.clear()
    # A later formatter pass must preserve the original generated IDs even when
    # the originating context is gone and another scrub source still matches.
    for value in ("09", "123", "20"):
        _register_credential(value)
    native_record = next(record for record in buffer.buffer if record.name.endswith(".native"))
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_other")):
        deferred = formatter.format(native_record)

    def check_records(records: list[dict]) -> None:
        for record in records:
            assert {key: record[key] for key in ids} == ids
            assert record["payload"] == expected_payload
            assert record["workflow_id"] == f"spoof-{masked}"
            assert record["organization_name"] == masked
            assert record["copilot_session_id"] == masked
            assert record["browser_session_id"] == f"pbs_{masked}"
            assert record["level"] == "warning"
            assert "20" in record["timestamp"]
            assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None
            message = record.get("msg", record.get("event"))
            assert message.split(" | ", 1)[0] == expected_message
            if " | " in message:
                for key, value in ids.items():
                    assert f"{key}={value}" in message

    assert len(context.log) == 2
    assert all(entry["workflow_run_attempt_number"] == 123 for entry in context.log)
    check_records(context.log)
    check_records([json.loads(static_stream.getvalue())])
    emitted = registered_log_stream.getvalue()
    if settings.JSON_LOGGING:
        records = [json.loads(line) for line in emitted.splitlines()]
        assert len(records) == 2
        check_records([*records, json.loads(deferred)])
    else:
        for output in (emitted, deferred):
            output = re.sub(r"\x1b\[[0-9;]*m", "", output)
            assert expected_message in output
            assert f"payload={expected_payload!r}" in output
            for key, value in ids.items():
                assert f"{key}={value}" in output
            assert "spoof-09" not in output
            assert "spoof-123" not in output
            assert "pbs_09" not in output

    # Keep the real selectors, attempt filter, and raw/human artifact encoders.
    # Stub only the storage I/O beneath them, after the emitting context is gone.
    create_artifact = AsyncMock()
    save_workflow_artifact = AsyncMock()
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifact_by_entity_id", AsyncMock(return_value=None))
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "create_log_artifact", create_artifact)
    monkeypatch.setattr(log_artifacts, "_save_workflow_log_artifact", save_workflow_artifact)
    monkeypatch.setattr(log_artifacts, "resolve_current_attempt", AsyncMock(return_value=(123, False)))
    cleanup = SkyvernContext(organization_id=ids["organization_id"], log=list(context.log))
    cleanup.log.append(
        {"workflow_run_id": "wr_other", "task_id": "tsk_other", "step_id": "stp_other", "event": "other"}
    )
    with skyvern_context.scoped(cleanup), skyvern_context.workflow_log_attempt(ids["workflow_run_id"], 123):
        await log_artifacts.save_workflow_run_logs(ids["workflow_run_id"])
        await log_artifacts.save_task_logs(ids["task_id"])
        await log_artifacts.save_step_logs(ids["step_id"])
    assert save_workflow_artifact.await_count == 2
    assert create_artifact.await_count == 4
    artifacts = [(call.args[-2], call.args[-1]) for call in save_workflow_artifact.await_args_list]
    artifacts.extend((call.kwargs["artifact_type"], call.kwargs["data"]) for call in create_artifact.await_args_list)
    for artifact_type, data in artifacts:
        if artifact_type == ArtifactType.SKYVERN_LOG_RAW:
            records = json.loads(data)
            assert len(records) == 2
            check_records(records)
            assert all(record["workflow_run_attempt_number"] == 123 for record in records)
        else:
            assert data.decode().count(expected_message) == 2
            assert "spoof-09" not in data.decode()
            assert "pbs_09" not in data.decode()


@pytest.mark.parametrize("with_context", [False, True])
def test_plain_metadata_names_and_copied_generated_values_are_still_caller_data(
    monkeypatch: pytest.MonkeyPatch, registered_log_stream: io.StringIO, with_context: bool
) -> None:
    _register_credential("123")
    _register_credential("id")
    context = SkyvernContext(workflow_run_id="wr_123", task_id="tsk_123") if with_context else None
    monkeypatch.setattr(skyvern_context, "current", lambda: context)
    metadata = {
        "workflow_run_id": "wr_123",
        "task_id": "tsk_123",
        "timestamp": "123",
        "copilot_session_id": "123",
    }
    event = {"msg": "123", **metadata, "payload": metadata}
    original = copy.deepcopy(event)
    result = redact_registered_secrets(None, "warning", event)  # type: ignore[arg-type]
    masked = REDACTED_SECRET_PLACEHOLDER
    expected = {key.replace("id", masked): value.replace("123", masked) for key, value in metadata.items()}
    assert result == {"msg": masked, **expected, "payload": expected}
    assert event == original
    structlog.get_logger("skyvern.test.metadata").warning("123", payload=metadata, **metadata)
    logging.getLogger("skyvern.test.metadata.foreign").warning("123", extra={"payload": metadata, **metadata})
    records = [json.loads(line) for line in registered_log_stream.getvalue().splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["payload"] == expected
        assert record["msg"].split(" | ", 1)[0] == masked
        assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None
        assert record[f"copilot_session_{masked}"] == masked
        for key in ("workflow_run_id", "task_id"):
            if with_context:
                assert record[key] == metadata[key]
            else:
                assert record[key.replace("id", masked)] == expected[key.replace("id", masked)]
    if with_context:
        enriched = add_log_context(None, "warning", {"msg": "123"})  # type: ignore[arg-type]
        result = redact_registered_secrets(
            None, "warning", {**enriched, "payload": dict(enriched), "foreign": enriched["workflow_run_id"]}
        )  # type: ignore[arg-type]
        assert result["workflow_run_id"] == "wr_123"
        assert result["task_id"] == "tsk_123"
        assert "123" not in json.dumps(result["payload"])
        assert result["foreign"] == f"wr_{masked}"
        assert result["msg"] == f"{masked} | task_id=tsk_123, workflow_run_id=wr_123"


@pytest.mark.parametrize("failure", ["collection", "iterator", "renderer", "node_limit"])
@pytest.mark.parametrize("message_keys", [("event",), ("msg",), ("event", "msg"), ()])
def test_registered_processor_fails_closed_with_usable_renderer_protocol(
    monkeypatch: pytest.MonkeyPatch, failure: str, message_keys: tuple[str, ...]
) -> None:
    class BrokenMapping(dict):
        def items(self):  # type: ignore[override]
            raise RuntimeError(_FAKE_CREDENTIAL)

    class BrokenRenderer:
        def __structlog__(self) -> object:
            raise RuntimeError(_FAKE_CREDENTIAL)

    payloads = {
        "collection": _FAKE_CREDENTIAL,
        "iterator": BrokenMapping({"detail": _FAKE_CREDENTIAL}),
        "renderer": BrokenRenderer(),
        "node_limit": ["safe"] * 10_001 + [_FAKE_CREDENTIAL],
    }
    if failure == "collection":

        def fail_collection() -> list[str]:
            raise RuntimeError(_FAKE_CREDENTIAL)

        monkeypatch.setattr(secret_scrub, "all_registered_secret_values", fail_collection)
    context = SkyvernContext(runtime_secret_values={_FAKE_CREDENTIAL, "event", "msg", "level", "warning"})
    logger = logging.getLogger("skyvern.test.protocol")
    event = {**dict.fromkeys(message_keys, "diagnostic"), "level": "warning", "payload": payloads[failure]}
    with skyvern_context.scoped(context):
        out = redact_registered_secrets(logger, "warning", event)
    assert out == {**dict.fromkeys(message_keys or ("event",), REDACTED_SECRET_PLACEHOLDER), "level": "warning"}
    assert all(event[key] == "diagnostic" for key in message_keys)
    assert event["payload"] is payloads[failure]
    json_event = out.copy()
    if "event" in json_event:
        json_event = structlog.processors.EventRenamer("msg")(logger, "warning", json_event)
    assert json.loads(render_bounded_json(logger, "warning", json_event)) == {
        "msg": REDACTED_SECRET_PLACEHOLDER,
        "level": "warning",
    }
    console = structlog.dev.ConsoleRenderer(colors=False, event_key="event" if "event" in out else "msg")
    rendered = console(logger, "warning", out.copy())
    assert REDACTED_SECRET_PLACEHOLDER in rendered
    assert "[warning" in rendered
    assert _FAKE_CREDENTIAL not in rendered


def test_registered_processor_bounds_cycles_and_depth() -> None:
    cyclic: dict = {"detail": _FAKE_CREDENTIAL}
    cyclic["self"] = cyclic
    cyclic["shared"] = cyclic
    context = SkyvernContext()
    context.register_secret_value(_FAKE_CREDENTIAL)
    with skyvern_context.scoped(context):
        out = redact_registered_secrets(
            None, "warning", {"event": "diagnostic", "cycle": cyclic, "deep": _nested_mapping(_FAKE_CREDENTIAL)}
        )  # type: ignore[arg-type]
    assert _FAKE_CREDENTIAL not in json.dumps(out)
    assert out["cycle"] == dict.fromkeys(("detail", "self", "shared"), REDACTED_SECRET_PLACEHOLDER)
    assert cyclic["self"] is cyclic


@pytest.mark.asyncio
async def test_registered_secrets_stay_with_current_concurrent_run(
    monkeypatch: pytest.MonkeyPatch,
    registered_log_stream: io.StringIO,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    run_ids = ("wr_log_a", "wr_log_b")
    values = ("synthetic-run-alpha", "synthetic-run-beta")
    runtime_values = ("synthetic-runtime-alpha", "synthetic-runtime-beta")
    manager = workflow_context_manager_factory(workflow_run_id=run_ids[0], secrets={"ref": values[0]})
    other = workflow_context_manager_factory(workflow_run_id=run_ids[1], secrets={"ref": values[1]})
    manager.workflow_run_contexts.update(other.workflow_run_contexts)
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", manager)
    ready = [asyncio.Event(), asyncio.Event()]

    async def emit(index: int) -> SkyvernContext:
        context = SkyvernContext(workflow_run_id=run_ids[index])
        context.register_secret_value(runtime_values[index])
        with skyvern_context.scoped(context):
            ready[index].set()
            await ready[1 - index].wait()
            fields = {
                "own": values[index],
                "runtime": runtime_values[index],
                "unregistered": values[1 - index],
                "unregistered_runtime": runtime_values[1 - index],
            }
            structlog.get_logger("skyvern.test.isolation").warning("run isolation", **fields)
            logging.getLogger("skyvern.test.foreign_isolation").warning("run isolation", extra=fields)
            await asyncio.sleep(0)
        return context

    contexts = await asyncio.gather(emit(0), emit(1))
    manager.workflow_run_contexts.clear()
    for context in contexts:
        context.runtime_secret_values.clear()
    records = [json.loads(line) for line in registered_log_stream.getvalue().splitlines()]
    for index, context in enumerate(contexts):
        own_records = [record for record in records if record.get("workflow_run_id") == run_ids[index]]
        assert len(own_records) == 2
        own_records.extend(json.loads(json.dumps(context.log, cls=SkyvernJSONLogEncoder)))
        for record in own_records:
            assert record["own"] == REDACTED_SECRET_PLACEHOLDER
            assert record["runtime"] == REDACTED_SECRET_PLACEHOLDER
            assert record["unregistered"] == values[1 - index]
            assert record["unregistered_runtime"] == runtime_values[1 - index]

    fresh = SkyvernContext(workflow_run_id=run_ids[0])
    with skyvern_context.scoped(fresh):
        structlog.get_logger("skyvern.test.isolation").warning("after teardown", detail=values[0])
    assert fresh.log[-1]["detail"] == values[0]
    assert secret_scrub.all_registered_secret_values() == []


def test_registered_logging_starts_without_app_or_cloud_imports() -> None:
    script = textwrap.dedent(
        """
        import json
        import logging
        import sys

        class BlockApplicationImports:
            def find_spec(self, name, path=None, target=None):
                if name in {"skyvern.forge.forge_app", "skyvern.forge.forge_app_initializer", "cloud", "starlette"} or name.startswith(("cloud.", "starlette.")):
                    raise ModuleNotFoundError(name)
                return None

        sys.meta_path.insert(0, BlockApplicationImports())
        import structlog
        from skyvern.config import settings
        from skyvern.forge.sdk.core import skyvern_context
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
        from skyvern.forge.sdk.forge_log import setup_logger
        from skyvern.forge.skyvern_json_encoder import SkyvernJSONLogEncoder

        settings.JSON_LOGGING = True
        settings.ENABLE_SECRET_ARTIFACT_REDACTION = False
        setup_logger()
        structlog.get_logger("skyvern.test.boot").warning("logging before app")
        context = SkyvernContext(workflow_run_id="wr_boot")
        context.register_secret_value("synthetic-boot-value")
        context.register_secret_value("q7")
        with skyvern_context.scoped(context):
            structlog.get_logger("skyvern.test.boot").warning("native boot", detail="synthetic-boot-value", note="q7")
            logging.getLogger("skyvern.test.boot").warning("foreign boot %s", "synthetic-boot-value")
        context.runtime_secret_values.clear()
        print(json.dumps(context.log, cls=SkyvernJSONLogEncoder))
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for message in ("logging before app", "native boot", "foreign boot"):
        assert message in output
    assert "synthetic-boot-value" not in output
    assert '"note": "q7"' not in output
    assert REDACTED_SECRET_PLACEHOLDER in output


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
        assert names.index("redact_sensitive_event_fields") < names.index("redact_registered_secrets")
        assert names.index("redact_registered_secrets") + 1 == names.index("skyvern_logs_processor")
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


_CODEBLOCK_PARAMETER_SECRET = "codeblock-parameter-secret-16595"


def _substring_redactor(value: object) -> object:
    if isinstance(value, str):
        return value.replace(_CODEBLOCK_PARAMETER_SECRET, "[redacted]").replace("id", "[redacted]")
    if isinstance(value, dict):
        return {_substring_redactor(key): _substring_redactor(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_substring_redactor(item) for item in value)
    return value


@pytest.mark.parametrize("registered_log_stream", [True, False], indirect=True, ids=["json", "console"])
def test_codeblock_fail_closed_redaction_emits_blanked_records_with_identity(
    registered_log_stream: io.StringIO,
) -> None:
    context_identity = {"workflow_run_id": "wr_575775527211416595", "request_id": "req_16595", "run_id": "run_16595"}
    with skyvern_context.scoped(SkyvernContext(**context_identity)), codeblock_parameter_log_redaction(lambda _: ""):
        structlog.get_logger("skyvern.forge.sdk.forge_log").warning(
            f"native {_CODEBLOCK_PARAMETER_SECRET}",
            payload=_CODEBLOCK_PARAMETER_SECRET,
            file=_CODEBLOCK_PARAMETER_SECRET,
            workflow_run_block_id="wrb_575775527211416595",
            **{f"key_{_CODEBLOCK_PARAMETER_SECRET}": 1},
        )
        try:
            raise ValueError(_CODEBLOCK_PARAMETER_SECRET)
        except ValueError:
            logging.getLogger("skyvern.forge.log_redaction").warning(
                "stdlib %s",
                _CODEBLOCK_PARAMETER_SECRET,
                extra={
                    "payload": _CODEBLOCK_PARAMETER_SECRET,
                    "workflow_run_block_id": "wrb_575775527211416595",
                    f"key_{_CODEBLOCK_PARAMETER_SECRET}": 1,
                },
            )

    emitted = registered_log_stream.getvalue()
    assert _CODEBLOCK_PARAMETER_SECRET not in emitted
    if not settings.JSON_LOGGING:
        native_line = emitted.splitlines()[0]
        assert "[test_forge_log_redaction.py" in native_line
        for value in (*context_identity.values(), "wrb_575775527211416595"):
            assert value in native_line
        return
    native, stdlib = (json.loads(line) for line in emitted.splitlines())
    for record, logger_name in ((native, "skyvern.forge.sdk.forge_log"), (stdlib, "skyvern.forge.log_redaction")):
        assert "payload" not in record
        assert record["level"] == "warning"
        assert record["logger"] == logger_name
        assert record["workflow_run_block_id"] == "wrb_575775527211416595"
        assert CODEBLOCK_LOG_REDACTED not in record["timestamp"]
        assert "exception" not in record
    assert native["msg"] == CODEBLOCK_LOG_REDACTED
    for key, value in context_identity.items():
        assert native[key] == value
    assert stdlib["workflow_run_id"] == context_identity["workflow_run_id"]
    assert native["file"] == CODEBLOCK_LOG_REDACTED
    assert native["func_name"] == "test_codeblock_fail_closed_redaction_emits_blanked_records_with_identity"
    assert type(native["lineno"]) is int


@pytest.mark.parametrize("registered_log_stream", [True], indirect=True)
def test_codeblock_redaction_never_rewrites_field_names(registered_log_stream: io.StringIO) -> None:
    with codeblock_parameter_log_redaction(_substring_redactor):
        structlog.get_logger("skyvern.test.codeblock_native").warning(
            "native",
            workflow_run_id="wr_575775527211416595",
            payload=_CODEBLOCK_PARAMETER_SECRET,
        )
        logging.getLogger("skyvern.test.codeblock_stdlib").warning(
            "stdlib",
            extra={
                "workflow_run_id": "wr_575775527211416595",
                "payload": _CODEBLOCK_PARAMETER_SECRET,
            },
        )

    emitted = registered_log_stream.getvalue()
    assert _CODEBLOCK_PARAMETER_SECRET not in emitted
    records = [json.loads(line) for line in emitted.splitlines()]
    assert len(records) == 2
    for record in records:
        assert "workflow_run_[redacted]" not in record
        assert record["workflow_run_id"] == "wr_575775527211416595"
        assert record["payload"] == "[redacted]"


def test_codeblock_redaction_redacts_caller_built_field_names(monkeypatch: pytest.MonkeyPatch) -> None:
    dynamic_key = f"key_{_CODEBLOCK_PARAMETER_SECRET}"
    (handler,) = _buffered_logger(monkeypatch, "skyvern.test.codeblock_dynamic_key", 1)
    logger = logging.getLogger("skyvern.test.codeblock_dynamic_key")

    with codeblock_parameter_log_redaction(_substring_redactor):
        logger.handle(
            logger.makeRecord(
                logger.name, logging.INFO, __file__, 1, "x", (), None, extra={dynamic_key: 1, "workflow_run_id": "wr_1"}
            )
        )
        event = redact_codeblock_parameters(
            None,  # type: ignore[arg-type]
            "info",
            {"event": "x", dynamic_key: 1, "workflow_run_id": "wr_1"},
        )

    (record,) = handler.buffer
    for fields in (record.__dict__, event):
        assert not any(_CODEBLOCK_PARAMETER_SECRET in str(key) for key in fields)
        assert fields["key_[redacted]"] == 1
        assert fields["workflow_run_id"] == "wr_1"


def _exact_value_redactor(parameter: str) -> Callable[[object], object]:
    def redact(value: object) -> object:
        if isinstance(value, str):
            return value.replace(parameter, CODEBLOCK_LOG_REDACTED)
        if type(value) in (int, float) and str(value) == parameter:
            return CODEBLOCK_LOG_REDACTED
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    return redact


def _buffered_logger(monkeypatch: pytest.MonkeyPatch, name: str, count: int) -> list[BufferingHandler]:
    handlers = [BufferingHandler(10) for _ in range(count)]
    logger = logging.getLogger(name)
    monkeypatch.setattr(logger, "handlers", handlers)
    monkeypatch.setattr(logger, "propagate", False)
    return handlers


@pytest.mark.parametrize("parameter", ["20", "INFO"])
def test_codeblock_redaction_keeps_level_record_metadata(monkeypatch: pytest.MonkeyPatch, parameter: str) -> None:
    handlers = _buffered_logger(monkeypatch, "skyvern.test.codeblock_levelno", 2)
    logger = logging.getLogger("skyvern.test.codeblock_levelno")

    with codeblock_parameter_log_redaction(_exact_value_redactor(parameter)):
        logger.handle(logger.makeRecord(logger.name, logging.INFO, __file__, 20, "hello", (), None))

    for handler in handlers:
        (record,) = handler.buffer
        assert type(record.levelno) is int and record.levelno == logging.INFO
        assert record.levelname == "INFO"
        assert type(record.lineno) is int and str(record.lineno) != parameter


def test_codeblock_fail_closed_keeps_enum_level_comparable(monkeypatch: pytest.MonkeyPatch) -> None:
    level = enum.IntEnum("Level", {"INFO": logging.INFO}).INFO
    handlers = _buffered_logger(monkeypatch, "skyvern.test.codeblock_enum_level", 2)
    logger = logging.getLogger("skyvern.test.codeblock_enum_level")

    with codeblock_parameter_log_redaction(lambda _: ""):
        logger.handle(logger.makeRecord(logger.name, level, __file__, 20, "hello", (), None))

    for handler in handlers:
        (record,) = handler.buffer
        assert record.levelno == logging.INFO
        assert record.levelname == "INFO"


def test_codeblock_fail_closed_blanks_runtime_chosen_names(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "parameter16595"
    platform_name = "skyvern.forge.sdk.forge_log"
    names = (secret, f"skyvern.{secret}", platform_name)
    handlers = {name: _buffered_logger(monkeypatch, name, 1)[0] for name in names}

    with codeblock_parameter_log_redaction(lambda _: ""):
        for name in handlers:
            record = logging.getLogger(name).makeRecord(name, logging.INFO, __file__, 1, "x", (), None)
            record.threadName = secret
            logging.getLogger(name).handle(record)
        events = [
            redact_codeblock_parameters(None, "info", {"event": "x", "logger": name})  # type: ignore[arg-type]
            for name in handlers
        ]

    *secret_records, platform_record = (handler.buffer[0] for handler in handlers.values())
    for record, event in zip(secret_records, events):
        assert secret not in repr(record.__dict__) and secret not in repr(event)
    assert platform_record.name == events[-1]["logger"] == platform_name
    assert platform_record.funcName == secret_records[0].funcName


def test_codeblock_fail_closed_blanks_caller_ids_without_platform_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "parameter16595"
    (handler,) = _buffered_logger(monkeypatch, "skyvern.test.fail_closed_ids", 1)
    logger = logging.getLogger("skyvern.test.fail_closed_ids")
    ids = {"workflow_run_block_id": secret, "workflow_run_id": "wr_575775527211416595"}

    with codeblock_parameter_log_redaction(lambda _: ""):
        logger.handle(logger.makeRecord(logger.name, logging.INFO, __file__, 1, "x", (), None, extra=ids))
        event = redact_codeblock_parameters(None, "info", {"event": "x", **ids})  # type: ignore[arg-type]

    for fields in (handler.buffer[0].__dict__, event):
        assert fields["workflow_run_block_id"] == CODEBLOCK_LOG_REDACTED
        assert fields["workflow_run_id"] == "wr_575775527211416595"


def test_codeblock_fail_closed_processor_keeps_only_id_shaped_identity() -> None:
    with codeblock_parameter_log_redaction(lambda _: ""):
        out = redact_codeblock_parameters(
            None,  # type: ignore[arg-type]
            "info",
            {
                "event": "x",
                "request_id": "r" * 3000,
                "workflow_run_id": "wr_575775527211416595",
                "organization_id": "o 1",
                "browser_session_id": "pbs_16595",
                "organization_name": "Org16595",
            },
        )

    assert out == {
        "event": CODEBLOCK_LOG_REDACTED,
        "request_id": CODEBLOCK_LOG_REDACTED,
        "workflow_run_id": "wr_575775527211416595",
        "organization_id": CODEBLOCK_LOG_REDACTED,
        "browser_session_id": CODEBLOCK_LOG_REDACTED,
        "organization_name": CODEBLOCK_LOG_REDACTED,
    }


class _CallerInt(int):
    pass


@pytest.mark.parametrize("path", ["extras", "dict_msg", "record_attribute"])
def test_codeblock_fail_closed_stdlib_record_keeps_only_id_shaped_identity(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    (handler,) = _buffered_logger(monkeypatch, "skyvern.config", 1)
    logger = logging.getLogger("skyvern.config")
    identity = {
        "request_id": "r" * 3000,
        "workflow_run_id": "wr_575775527211416595",
        "browser_session_id": "pbs_16595",
        "organization_name": "Org16595",
    }
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        {"event": "x", **identity} if path == "dict_msg" else "x",
        (),
        None,
        extra=identity if path == "extras" else None,
    )
    if path == "record_attribute":
        record.funcName = "f" * 3000
        record.lineno = _CallerInt(1)

    with codeblock_parameter_log_redaction(lambda _: ""):
        logger.handle(record)

    (emitted,) = handler.buffer
    if path == "record_attribute":
        assert emitted.funcName == CODEBLOCK_LOG_REDACTED
        assert emitted.lineno == CODEBLOCK_LOG_REDACTED
        assert emitted.name == logger.name
        return
    fields = emitted.msg if path == "dict_msg" else emitted.__dict__
    assert fields["request_id"] == CODEBLOCK_LOG_REDACTED
    assert fields["browser_session_id"] == CODEBLOCK_LOG_REDACTED
    assert fields["organization_name"] == CODEBLOCK_LOG_REDACTED
    assert fields["workflow_run_id"] == "wr_575775527211416595"
