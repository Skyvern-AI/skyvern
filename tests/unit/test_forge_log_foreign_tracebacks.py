from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot import secret_scrub
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.forge_log import setup_logger

_REGISTERED_CREDENTIAL = "fake-registered-pa55w0rd-9f3c1a"


@pytest.fixture
def json_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    setup_logger()
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    stream = io.StringIO()
    previous = handler.setStream(stream)
    try:
        yield stream
    finally:
        handler.setStream(previous)


def _raise_through_wrapper() -> None:
    def async_wrapper() -> None:
        raise ValueError("kaboom from an activity")

    async_wrapper()


def test_foreign_stdlib_exception_renders_single_structured_line(json_stream: io.StringIO) -> None:
    """temporalio/asyncio emit stdlib records; their exc_info must collapse to one JSON entry."""
    logger = logging.getLogger("temporalio.activity")
    try:
        _raise_through_wrapper()
    except ValueError:
        logger.warning("Completing activity as failed", exc_info=True)

    lines = json_stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["logger"] == "temporalio.activity"
    assert "exc_info" not in record  # the raw (traceback, ...) tuple is never dumped
    assert "<traceback object at" not in json_stream.getvalue()
    assert "Traceback (most recent call last)" in record["exception"]
    assert "ValueError: kaboom from an activity" in record["exception"]
    assert "async_wrapper" in record["exception"]
    assert record["error_type"] == "builtins.ValueError"
    assert record["error_category"] == "ERROR"
    assert record["exception_hash"]


def test_exception_log_fields_matches_processor_output_for_raised_exception() -> None:
    """exception_log_fields lets a warning carry the same dashboard fields add_error_processor
    derives from exc_info, so downgraded lines stay groupable without rendering a traceback."""
    from skyvern.forge.sdk.forge_log import exception_log_fields

    try:
        _raise_through_wrapper()
    except ValueError as exc:
        fields = exception_log_fields(exc)

    assert fields["error_type"] == "builtins.ValueError"
    assert fields["error_category"] == "ERROR"
    assert fields["exception_hash"]


def test_exception_log_fields_omits_hash_when_never_raised() -> None:
    from skyvern.forge.sdk.forge_log import exception_log_fields

    fields = exception_log_fields(ValueError("never raised"))

    assert fields["error_type"] == "builtins.ValueError"
    assert fields["error_category"] == "ERROR"
    assert "exception_hash" not in fields


@pytest.fixture
def registered_credential() -> Iterator[str]:
    secret_scrub._SESSION_SCRUB_VALUES.clear()
    secret_scrub._SESSION_SCRUB_VALUES["pbs_foreign_traceback"] = [_REGISTERED_CREDENTIAL]
    try:
        yield _REGISTERED_CREDENTIAL
    finally:
        secret_scrub._SESSION_SCRUB_VALUES.clear()


def _raise_with_credential_in_message(credential: str) -> None:
    """A driver renders a bound parameter — including a credential — into the exception message."""
    raise RuntimeError(f"(psycopg.errors.UniqueViolation) INSERT failed [parameters: ('{credential}',)]")


@pytest.mark.parametrize("logger_name", ["temporalio.activity", "asyncio", "sqlalchemy.engine.Engine"])
def test_foreign_record_exception_text_is_redacted(
    json_stream: io.StringIO, registered_credential: str, logger_name: str
) -> None:
    """Foreign stdlib records reach the same serializer as native ones and must be scrubbed there.

    The redaction processors used to run only in the structlog chain, so anything logged through a
    stdlib logger (temporal, asyncio, sqlalchemy, uvicorn) shipped its exception text unredacted.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    try:
        _raise_with_credential_in_message(registered_credential)
    except RuntimeError:
        logger.error("statement failed", exc_info=True)

    payload = json_stream.getvalue()
    assert registered_credential not in payload
    assert REDACTED_SECRET_PLACEHOLDER in payload


def test_redaction_does_not_blind_the_foreign_traceback(json_stream: io.StringIO, registered_credential: str) -> None:
    """Only the credential is removed — type, frame, and traceback structure survive."""
    logger = logging.getLogger("temporalio.activity")
    logger.setLevel(logging.INFO)
    try:
        _raise_with_credential_in_message(registered_credential)
    except RuntimeError:
        logger.error("statement failed", exc_info=True)

    record = json.loads(json_stream.getvalue().strip().splitlines()[0])
    assert "Traceback (most recent call last)" in record["exception"]
    assert "RuntimeError" in record["exception"]
    assert "_raise_with_credential_in_message" in record["exception"]
    assert "psycopg.errors.UniqueViolation" in record["exception"]
    assert record["error_type"] == "builtins.RuntimeError"


def test_native_structlog_exception_text_stays_redacted(json_stream: io.StringIO, registered_credential: str) -> None:
    """Guards the additive fix: the structlog chain must keep its own redaction pass.

    ``skyvern_logs_processor`` copies the event dict into ``context.log`` (persisted to the per-run
    S3 log artifact) BEFORE the formatter runs, so moving the redactors instead of adding them
    would close the stdout leak and open one into run artifacts.
    """
    logger = structlog.get_logger("skyvern.native_redaction_test")
    try:
        _raise_with_credential_in_message(registered_credential)
    except RuntimeError:
        logger.exception("statement failed")

    assert registered_credential not in json_stream.getvalue()


def test_native_structlog_exception_not_double_processed(json_stream: io.StringIO) -> None:
    logger = structlog.get_logger("skyvern.foreign_traceback_test")
    try:
        _raise_through_wrapper()
    except ValueError:
        logger.exception("native boom")

    lines = json_stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert "exc_info" not in record
    assert "Traceback (most recent call last)" in record["exception"]
    assert record["exception"].count("Traceback (most recent call last)") == 1
    assert record["error_type"] == "builtins.ValueError"
