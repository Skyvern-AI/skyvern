from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.forge_log import setup_logger


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """setup_logger() replaces the root handler process-wide, so JSON-mode tests would
    otherwise leak that handler into every test that runs after them."""
    yield
    skyvern_context.reset()
    setup_logger()


def _render(record: logging.LogRecord) -> str:
    setup_logger()
    formatter = logging.getLogger().handlers[0].formatter
    assert formatter is not None
    return formatter.format(record)


def _foreign_record(**extra: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="codeblock.codeblock_grpc",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="CodeBlock runner unavailable",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _native_json_payload(capsys: pytest.CaptureFixture[str], **kwargs: str) -> dict[str, str]:
    """Render a native structlog call through the JSON branch — the one production runs."""
    setup_logger()
    structlog.get_logger("skyvern.reserved_keys_test").info("Wrote the final state", **kwargs)
    captured = capsys.readouterr()
    line = (captured.err or captured.out).strip().splitlines()[-1]
    return json.loads(line)


def test_native_json_log_renames_reserved_status_kwarg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Datadog intake preprocessing reads the reserved `status` attribute before `level`,
    # so status="completed" made this exact log line `critical` in production (SKY-13809).
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = _native_json_payload(capsys, status="completed")

    assert payload["event_status"] == "completed"
    assert "status" not in payload
    assert payload["level"] == "info"


def test_native_json_log_renames_all_reserved_kwargs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = _native_json_payload(
        capsys,
        status="would_fire",
        message="a domain payload",
        host="10.0.0.1",
        service="anchor",
        source="stderr",
        hostname="pod-abc123",
        severity="advisory",
    )

    assert payload["event_status"] == "would_fire"
    assert payload["event_message"] == "a domain payload"
    assert payload["event_host"] == "10.0.0.1"
    assert payload["event_service"] == "anchor"
    assert payload["event_source"] == "stderr"
    # Datadog resolves the log's host from `host`, `hostname`, or `syslog.hostname`.
    assert payload["event_hostname"] == "pod-abc123"
    # Datadog's status-attribute list is status, severity, level — stripping `status`
    # would otherwise promote a domain `severity` kwarg to the severity source.
    assert payload["event_severity"] == "advisory"
    assert payload["level"] == "info"
    for reserved in ("status", "message", "host", "service", "source", "hostname", "severity"):
        assert reserved not in payload


def test_msg_and_error_kwargs_are_deliberately_not_renamed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `msg` MUST stay out of the rename map: this processor runs at the render seam, after
    # EventRenamer has already moved the real log message into `msg` — renaming it there
    # would strip the message off every line. `error` is a standard attribute, not an intake
    # remap source, and ~477 call sites query it as `@error`.
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = _native_json_payload(capsys, error="boom")

    assert payload["msg"].startswith("Wrote the final state")
    assert payload["error"] == "boom"
    assert "event_msg" not in payload
    assert "event_error" not in payload


def test_native_json_log_does_not_duplicate_structured_fields_in_msg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = _native_json_payload(capsys, status="completed")

    assert payload["msg"] == "Wrote the final state"


def test_foreign_record_extra_reserved_keys_renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Foreign stdlib records (`extra={"status": ...}`) ship through the same formatter
    # seam and hit the same intake preprocessing.
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = json.loads(_render(_foreign_record(status="UNAVAILABLE", host="10.0.0.2")))

    assert payload["event_status"] == "UNAVAILABLE"
    assert payload["event_host"] == "10.0.0.2"
    assert "status" not in payload
    assert "host" not in payload
    assert payload.get("msg", "").startswith("CodeBlock runner unavailable")


def test_explicit_escaped_kwarg_wins_over_renamed_reserved_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # If a call site ever passes both, the explicit `event_status` is the intentional one;
    # the reserved key is still stripped so it cannot reach intake.
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    payload = _native_json_payload(capsys, status="completed", event_status="explicit")

    assert payload["event_status"] == "explicit"
    assert "status" not in payload
    assert payload["msg"] == "Wrote the final state"


def test_console_mode_keeps_authored_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local consoles never reach Datadog intake; keep dev output as authored.
    monkeypatch.setattr(settings, "JSON_LOGGING", False)
    rendered = _render(_foreign_record(status="UNAVAILABLE"))

    assert "event_status" not in rendered
    assert "UNAVAILABLE" in rendered
