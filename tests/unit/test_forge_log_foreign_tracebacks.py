from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from skyvern.config import settings
from skyvern.forge.sdk.forge_log import setup_logger


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
