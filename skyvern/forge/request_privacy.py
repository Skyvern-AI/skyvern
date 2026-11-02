from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Coroutine, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from structlog.typing import EventDict

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import Scope

_private_logging: ContextVar[str | None] = ContextVar("private_request_logging", default=None)
_private_audit: ContextVar[bool] = ContextVar("private_request_audit", default=False)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"})
_OUTCOMES = frozenset({"complete", "failed", "stream_failed", "disconnected", "cancelled"})
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_Result = TypeVar("_Result")


class _GeneratorAwaitable(Generic[_Result]):
    def __init__(self, coroutine: Generator[Any, None, _Result]) -> None:
        self.coroutine = coroutine

    def __await__(self) -> Generator[Any, None, _Result]:
        return (yield from self.coroutine)


@dataclass(frozen=True)
class PrivateRequestPolicy:
    classifier: Callable[[str], bool]
    audit_path: str


def register_private_request_classifier(app: Starlette, classifier: Callable[[str], bool], *, audit_path: str) -> None:
    if not callable(classifier) or re.fullmatch(r"/[a-z0-9_/-]{1,120}", audit_path) is None:
        raise ValueError("Private request classification requires a callable and a static audit path")
    app.state.private_request_policy = PrivateRequestPolicy(classifier, audit_path)


def private_request_path(scope: Scope) -> str | None:
    application = scope.get("app")
    if application is None:
        return None
    try:
        policy = application.state.private_request_policy
    except AttributeError:
        return None
    if not isinstance(policy, PrivateRequestPolicy):
        return "/private"
    try:
        private = policy.classifier(scope["path"])
    except Exception:
        return policy.audit_path
    if type(private) is not bool:
        return policy.audit_path
    return policy.audit_path if private else None


@contextmanager
def private_logging_scope(audit_path: str | None = None) -> Iterator[None]:
    try:
        install_private_task_guard(asyncio.get_running_loop())
    except RuntimeError:
        pass
    token = _private_logging.set(audit_path or _private_logging.get() or "/private")
    try:
        yield
    finally:
        _private_logging.reset(token)


def _private_task_done(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        with private_logging_scope():
            logging.getLogger(__name__).error("private.background_task_failed")


def install_private_task_guard(loop: asyncio.AbstractEventLoop) -> None:
    previous = loop.get_task_factory()
    if previous is not None and getattr(previous, "_private_task_guard", False):
        return

    def factory(
        loop: asyncio.AbstractEventLoop,
        coroutine: Coroutine[Any, Any, _Result] | Generator[Any, None, _Result],
        **kwargs: Any,
    ) -> asyncio.Future[_Result]:
        context = kwargs.get("context")
        private = _private_logging.get() or (context.get(_private_logging) if context is not None else None)
        selected = coroutine
        entered = False
        if private:

            async def run_private() -> _Result:
                nonlocal entered
                entered = True
                with private_logging_scope(private):
                    if isinstance(coroutine, Coroutine):
                        return await coroutine
                    return await _GeneratorAwaitable(coroutine)

            selected = run_private()
        try:
            # Python 3.11 accepts generator coroutines; newer stubs narrow the constructor annotation.
            selected_coroutine = cast(Coroutine[Any, Any, _Result], selected)
            task = (
                previous(loop, selected_coroutine, **kwargs)
                if previous is not None
                else asyncio.Task(selected_coroutine, loop=loop, **kwargs)
            )
        except BaseException:
            if private:
                selected.close()
                if not entered:
                    coroutine.close()
            raise
        if private:

            def done(task: asyncio.Future[Any]) -> None:
                if not entered:
                    coroutine.close()
                _private_task_done(task)

            task.add_done_callback(done)
        return task

    factory._private_task_guard = True  # type: ignore[attr-defined]
    loop.set_task_factory(factory)


@contextmanager
def private_request_audit() -> Iterator[None]:
    token = _private_audit.set(True)
    try:
        yield
    finally:
        _private_audit.reset(token)


def redact_private_log_event(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    if not _private_logging.get():
        return event_dict
    key = "event" if "event" in event_dict else "msg"
    safe: EventDict = {key: "private.suppressed_log"}
    if _private_audit.get():
        safe[key] = "api.raw_request"
        safe["path"] = _private_logging.get()
        if type(event_dict.get("method")) is str and event_dict["method"] in _METHODS:
            safe["method"] = event_dict["method"]
        status = event_dict.get("status_code")
        if type(status) is int and 100 <= status <= 599:
            safe["status_code"] = status
        duration = event_dict.get("duration_seconds")
        if type(duration) is float and math.isfinite(duration) and duration >= 0:
            safe["duration_seconds"] = duration
        if type(event_dict.get("outcome")) is str and event_dict["outcome"] in _OUTCOMES:
            safe["outcome"] = event_dict["outcome"]
    if method_name in {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    }:
        safe["level"] = method_name
    return safe


def redact_private_foreign_event(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    safe = redact_private_log_event(logger, method_name, event_dict)
    for plumbing in ("_record", "_from_structlog"):
        if plumbing in event_dict:
            safe[plumbing] = event_dict[plumbing]
    return safe


def private_export_record(record: logging.LogRecord) -> logging.LogRecord:
    if not _private_logging.get():
        return record
    levels = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warning",
        logging.ERROR: "error",
        logging.CRITICAL: "critical",
    }
    level = record.levelno if type(record.levelno) is int and record.levelno in levels else logging.INFO
    event = record.msg if isinstance(record.msg, dict) else {}
    safe = redact_private_log_event(None, levels[level], event)
    exported = logging.LogRecord("private.request", level, "", 0, safe, (), None, func="")
    exported.levelname = levels[level].upper()
    for key, value in safe.items():
        if key not in {"event", "msg", "level"}:
            setattr(exported, key, value)
    return exported


class PrivateRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _private_logging.get():
            return True
        if isinstance(record.msg, dict):
            record.msg = redact_private_log_event(None, record.levelname.lower(), record.msg)
        else:
            record.msg = "private.suppressed_log"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        for key in set(record.__dict__) - _STANDARD_RECORD_FIELDS - {"_logger", "_name"}:
            del record.__dict__[key]
        return True
