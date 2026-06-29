from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from skyvern.forge.sdk.forge_log import _DriverPipeNoiseFilter, setup_logger


def _orphaned_future_record(exc_text: str) -> logging.LogRecord:
    exc = Exception(exc_text)
    message = f"Future exception was never retrieved\nfuture: <Future finished exception=Exception('{exc_text}')>"
    return logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=(type(exc), exc, None),
    )


def test_filter_drops_driver_pipe_future_noise() -> None:
    record = _orphaned_future_record("Connection closed while reading from the driver")
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_matches_via_exc_info_when_message_lacks_repr() -> None:
    exc = Exception("Connection closed while reading from the driver")
    record = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Future exception was never retrieved",
        args=(),
        exc_info=(type(exc), exc, None),
    )
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_keeps_other_orphaned_future_exceptions() -> None:
    record = _orphaned_future_record("some other unrelated failure")
    assert _DriverPipeNoiseFilter().filter(record) is True


def test_filter_keeps_real_driver_errors_not_from_orphaned_future() -> None:
    exc = Exception("Connection closed while reading from the driver")
    record = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Task exception was never retrieved",
        args=(),
        exc_info=(type(exc), exc, None),
    )
    assert _DriverPipeNoiseFilter().filter(record) is True


def test_filter_keeps_unrelated_asyncio_logs() -> None:
    record = logging.LogRecord(
        name="asyncio",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="Using selector: %s",
        args=("KqueueSelector",),
        exc_info=None,
    )
    assert _DriverPipeNoiseFilter().filter(record) is True


@pytest.fixture
def _restore_asyncio_filters() -> Iterator[None]:
    asyncio_logger = logging.getLogger("asyncio")
    saved = list(asyncio_logger.filters)
    yield
    asyncio_logger.filters = saved


def test_setup_logger_installs_single_driver_pipe_filter(_restore_asyncio_filters: None) -> None:
    setup_logger()
    setup_logger()
    asyncio_logger = logging.getLogger("asyncio")
    installed = [f for f in asyncio_logger.filters if isinstance(f, _DriverPipeNoiseFilter)]
    assert len(installed) == 1


def test_setup_logger_filter_suppresses_emitted_noise(_restore_asyncio_filters: None) -> None:
    setup_logger()
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    asyncio_logger = logging.getLogger("asyncio")
    handler = _Capture()
    asyncio_logger.addHandler(handler)
    try:
        asyncio_logger.handle(_orphaned_future_record("Connection closed while reading from the driver"))
        asyncio_logger.handle(_orphaned_future_record("a genuinely different error"))
    finally:
        asyncio_logger.removeHandler(handler)

    messages = [r.getMessage() for r in captured]
    assert not any("Connection closed while reading from the driver" in m for m in messages)
    assert any("a genuinely different error" in m for m in messages)
