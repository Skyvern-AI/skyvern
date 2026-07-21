from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from skyvern.forge.sdk.forge_log import _DriverPipeNoiseFilter, setup_logger


class TargetClosedError(Exception):
    """Stand-in for patchright's TargetClosedError (the filter matches by type name)."""


class PlaywrightError(Exception):
    """Stand-in for playwright/patchright's Error carrying the collected-object message."""


_CHANNEL_COLLECTED_TEXT = "Channel.send: The object has been collected to prevent unbounded heap growth."


def _orphaned_task_record_for(
    exc: Exception, *, message: str = "Task exception was never retrieved", include_repr: bool = True
) -> logging.LogRecord:
    msg = message
    if include_repr:
        msg = f"{message}\ntask: <Task finished name='Task-1' coro=<foo() done> exception={exc!r}>"
    return logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=(type(exc), exc, None),
    )


def _orphaned_future_record(exc_text: str) -> logging.LogRecord:
    return _orphaned_future_record_for(Exception(exc_text))


def _orphaned_future_record_for(exc: Exception, *, include_repr: bool = True) -> logging.LogRecord:
    message = "Future exception was never retrieved"
    if include_repr:
        message = f"{message}\nfuture: <Future finished exception={exc!r}>"
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


def test_filter_drops_target_closed_future_noise() -> None:
    record = _orphaned_future_record_for(TargetClosedError("Target page, context or browser has been closed"))
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_drops_target_closed_via_exc_info_when_message_lacks_repr() -> None:
    record = _orphaned_future_record_for(
        TargetClosedError("Target page, context or browser has been closed"), include_repr=False
    )
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_drops_target_closed_message_only_without_exc_info() -> None:
    record = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Future exception was never retrieved\nfuture: <Future finished exception=Target page, context or browser has been closed>",
        args=(),
        exc_info=None,
    )
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_keeps_target_closed_lookalike_with_wrong_type() -> None:
    # Same "...has been closed" text but NOT a TargetClosedError (e.g. a crashed browser
    # surfacing the message on a plain exception) must stay visible — SKY-11962 codex review.
    record = _orphaned_future_record_for(Exception("Target page, context or browser has been closed"))
    assert _DriverPipeNoiseFilter().filter(record) is True


def test_filter_keeps_target_closed_error_from_awaited_path() -> None:
    # A run-affecting crash surfaces its actionable TargetClosedError through an AWAITED
    # driver call, logged on an app logger with a normal message — NOT asyncio's
    # "Future exception was never retrieved" record. The filter only ever touches the
    # orphaned-future artifact (message must contain that marker), so the crash signal is
    # never suppressed even though the fire-and-forget orphaned future carrying the same
    # TargetClosedError is dropped. Proves the codex P1 "this hides genuine crashes" does
    # not hold: the crash's actionable signal travels the awaited path this filter ignores.
    exc = TargetClosedError("Target page, context or browser has been closed")
    record = logging.LogRecord(
        name="skyvern.webeye.actions.handler",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Unhandled exception in action handler",
        args=(),
        exc_info=(type(exc), exc, None),
    )
    assert _DriverPipeNoiseFilter().filter(record) is True


def test_filter_drops_collected_object_task_noise() -> None:
    # The recurring Hetzner burst: an orphaned Playwright teardown coroutine raises
    # "Channel.send: The object has been collected ..." and asyncio logs it as a
    # "Task exception was never retrieved" record. Pre-existing benign teardown noise.
    record = _orphaned_task_record_for(PlaywrightError(_CHANNEL_COLLECTED_TEXT))
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_drops_collected_object_future_noise() -> None:
    # Same collected-object teardown surfacing on the Future variant.
    record = _orphaned_future_record_for(PlaywrightError(_CHANNEL_COLLECTED_TEXT))
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_drops_collected_object_via_exc_info_when_message_lacks_repr() -> None:
    record = _orphaned_task_record_for(PlaywrightError(_CHANNEL_COLLECTED_TEXT), include_repr=False)
    assert _DriverPipeNoiseFilter().filter(record) is False


def test_filter_keeps_execution_context_destroyed_orphaned_task() -> None:
    # A different asyncio teardown class must stay visible — narrow suppression only.
    record = _orphaned_task_record_for(PlaywrightError("Execution context was destroyed"))
    assert _DriverPipeNoiseFilter().filter(record) is True


def test_filter_keeps_generic_orphaned_task() -> None:
    record = _orphaned_task_record_for(RuntimeError("some genuinely different bug"))
    assert _DriverPipeNoiseFilter().filter(record) is True


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
        asyncio_logger.handle(_orphaned_task_record_for(PlaywrightError(_CHANNEL_COLLECTED_TEXT)))
        asyncio_logger.handle(_orphaned_future_record("a genuinely different error"))
    finally:
        asyncio_logger.removeHandler(handler)

    messages = [r.getMessage() for r in captured]
    assert not any("Connection closed while reading from the driver" in m for m in messages)
    assert not any(_CHANNEL_COLLECTED_TEXT in m for m in messages)
    assert any("a genuinely different error" in m for m in messages)
