import logging
import random
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterator
from weakref import WeakSet

import structlog
from structlog.typing import EventDict, Processor

from skyvern._version import __version__
from skyvern.config import settings
from skyvern.forge.log_redaction import (
    REDACTED,
    is_proxy_observability_key,
    is_sensitive_key,
    redact_bearer_tokens_in_text,
    redact_proxy_observability_value,
    redact_sensitive_fields,
)
from skyvern.forge.sdk.core import skyvern_context

LOGGING_LEVEL_MAP: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Resolved once at setup time and injected into every log event.
_entrypoint: str = "unknown"


class _CodeBlockLogRedactionScope:
    __slots__ = ("parent", "processed_records", "redactor")

    def __init__(
        self,
        redactor: Callable[[Any], Any],
        parent: "_CodeBlockLogRedactionScope | None",
    ) -> None:
        self.parent = parent
        self.processed_records: WeakSet[logging.LogRecord] = WeakSet()
        self.redactor: Callable[[Any], Any] | None = redactor


_codeblock_log_scope: ContextVar[_CodeBlockLogRedactionScope | None] = ContextVar(
    "codeblock_log_redaction_scope", default=None
)
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _current_codeblock_log_scope() -> _CodeBlockLogRedactionScope | None:
    scope = _codeblock_log_scope.get()
    while scope is not None:
        if scope.redactor is not None:
            return scope
        scope = scope.parent
    return None


def current_codeblock_log_redactor() -> Callable[[Any], Any] | None:
    scope = _current_codeblock_log_scope()
    return scope.redactor if scope is not None else None


def _install_codeblock_fastmcp_trace_guard() -> None:
    from fastmcp import telemetry as base_telemetry
    from fastmcp.client import telemetry as client_telemetry
    from fastmcp.server import telemetry as server_telemetry
    from opentelemetry.trace import NoOpTracer

    noop_tracer = NoOpTracer()
    for telemetry in (base_telemetry, client_telemetry, server_telemetry):
        get_tracer = telemetry.get_tracer
        if getattr(get_tracer, "_skyvern_codeblock_guard", False) is True:
            continue

        def guarded_get_tracer(version: str | None = None, *, _get_tracer: Any = get_tracer) -> Any:
            if current_codeblock_log_redactor() is not None:
                return noop_tracer
            return _get_tracer(version)

        guarded_get_tracer._skyvern_codeblock_guard = True  # type: ignore[attr-defined]
        telemetry.get_tracer = guarded_get_tracer


def _render_opaque_log_values(value: Any) -> Any:
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if type(value) is dict:
        return {_render_opaque_log_values(key): _render_opaque_log_values(item) for key, item in value.items()}
    if type(value) is list:
        return [_render_opaque_log_values(item) for item in value]
    if type(value) is tuple:
        return tuple(_render_opaque_log_values(item) for item in value)
    try:
        return repr(value)
    except BaseException:
        return ""


def _redact_codeblock_log_value(value: Any) -> Any:
    redactor = current_codeblock_log_redactor()
    if redactor is None:
        return value
    try:
        return redactor(_render_opaque_log_values(value))
    except BaseException:
        return ""


class _CodeBlockParameterLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        scope = _current_codeblock_log_scope()
        if scope is None or record in scope.processed_records:
            return True
        try:
            message = record.msg if isinstance(record.msg, dict) else record.getMessage()
        except BaseException:
            message = ""
        extra_keys = set(record.__dict__) - _STANDARD_LOG_RECORD_FIELDS
        metadata: dict[str, str] = {}
        for key in _STANDARD_LOG_RECORD_FIELDS - {"msg", "args", "exc_info", "exc_text", "stack_info"}:
            value = record.__dict__.get(key)
            if type(value) is str:
                metadata[key] = value
            elif type(value) in {bool, int, float}:
                metadata[key] = str(value)
            elif value is not None:
                return False
        redacted = _redact_codeblock_log_value(
            {
                "message": message,
                "extras": {key: record.__dict__[key] for key in extra_keys},
                "metadata": metadata,
            }
        )
        if type(redacted) is not dict or set(redacted) != {"message", "extras", "metadata"}:
            return False
        redacted_message = redacted["message"]
        extras = redacted["extras"]
        redacted_metadata = redacted["metadata"]
        if type(extras) is not dict or set(extras) != extra_keys or type(redacted_metadata) is not dict:
            return False
        if isinstance(message, dict):
            message_key = "event" if "event" in message else "msg" if "msg" in message else None
            if not isinstance(redacted_message, dict) or (
                message_key is not None and message_key not in redacted_message
            ):
                redacted_message = {message_key or "event": ""}
        for key, value in metadata.items():
            redacted_value = redacted_metadata.get(key)
            original = record.__dict__[key]
            if type(original) is str and type(redacted_value) is str:
                record.__dict__[key] = redacted_value
            elif redacted_value != value:
                return False
        record.msg = redacted_message if isinstance(redacted_message, (str, dict)) else ""
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.__dict__.update(extras)
        scope.processed_records.add(record)
        return True


_CODEBLOCK_PARAMETER_LOG_FILTER = _CodeBlockParameterLogFilter()


@contextmanager
def codeblock_parameter_log_redaction(redactor: Callable[[Any], Any]) -> Iterator[None]:
    parent = _codeblock_log_scope.get()
    parent_redactor = current_codeblock_log_redactor()
    if parent_redactor is not None:
        nested_redactor = redactor

        def redactor(value: Any) -> Any:
            return nested_redactor(parent_redactor(value))

    scope = _CodeBlockLogRedactionScope(redactor, parent)
    token = _codeblock_log_scope.set(scope)
    try:
        _install_codeblock_fastmcp_trace_guard()
        root_logger = logging.getLogger()
        loggers = [root_logger, logging.getLogger("openai.agents")]
        loggers.extend(
            logger for logger in logging.Logger.manager.loggerDict.values() if isinstance(logger, logging.Logger)
        )
        for logger in loggers:
            for handler in logger.handlers:
                handler.addFilter(_CODEBLOCK_PARAMETER_LOG_FILTER)
        yield
    finally:
        scope.redactor = None
        _codeblock_log_scope.reset(token)


_DRIVER_PIPE_CLOSED_ERROR = "Connection closed while reading from the driver"
_TARGET_CLOSED_ERROR = "Target page, context or browser has been closed"
_ORPHANED_FUTURE_MESSAGE = "Future exception was never retrieved"
_ORPHANED_TASK_MESSAGE = "Task exception was never retrieved"
_TARGET_CLOSED_ERROR_TYPE = "TargetClosedError"
_CHANNEL_COLLECTED_ERROR = "The object has been collected to prevent unbounded heap growth"

# Production collection splits records at the observed 75 KiB boundary. Keep JSON
# below that boundary so one application event remains one Datadog event.
MAX_JSON_LOG_BYTES = 64 * 1024
_OVERSIZED_LOG_VALUE_CHARS = 4 * 1024
_OVERSIZED_LOG_METADATA_CHARS = 512
_OVERSIZED_LOG_FIELDS = (
    "msg",
    "timestamp",
    "level",
    "logger",
    "entrypoint",
    "env",
    "version",
    "event_status",
    "event_severity",
    "event_message",
    "event_host",
    "event_hostname",
    "event_service",
    "event_source",
    "pathname",
    "filename",
    "module",
    "func_name",
    "lineno",
    "request_id",
    "organization_id",
    "organization_name",
    "step_id",
    "task_id",
    "run_id",
    "workflow_id",
    "workflow_run_id",
    "workflow_permanent_id",
    "task_v2_id",
    "browser_session_id",
    "copilot_session_id",
    "browser_container_ip",
    "browser_container_task_arn",
    "error",
    "error_type",
    "error_category",
    "exception_hash",
    "exception",
)


def _json_log_default(obj: Any) -> Any:
    """Serialize what json.dumps cannot, keeping Decimal numeric.

    structlog's fallback is repr(), which renders a Decimal as the string
    "Decimal('0.004')" — valid JSON, but a string facet no query can aggregate.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if hasattr(obj, "__structlog__"):
        return obj.__structlog__()
    return repr(obj)


_JSON_RENDERER = structlog.processors.JSONRenderer(default=_json_log_default)


class _DriverPipeNoiseFilter(logging.Filter):
    """Drop asyncio's orphaned-task/future noise from a torn-down Playwright driver/target.

    All variants are the same benign artifact: a fire-and-forget driver coroutine left a
    task/future behind, then the target/driver went away before it resolved, so asyncio's
    __del__ logs the un-retrieved exception at ERROR. Suppressed cases:
      - driver-pipe close (matched by its distinctive message);
      - patchright's TargetClosedError, matched by exception *type* not text — the same
        "...has been closed" message can also come from a crashed/killed browser, so a
        type check keeps real failures visible (Future variant only, unchanged);
      - Playwright's "Channel.send: The object has been collected ..." teardown, matched by
        its distinctive message on either the Task or Future orphaned-artifact variant.
        This is a recurring, high-volume, pre-existing teardown pattern; it is unrelated to
        OTEL instrumentation (asyncio's __del__ emits it whether or not AsyncioInstrumentor
        is loaded — the instrumentor only adds a trace_coroutine frame to the traceback).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        exc = record.exc_info[1] if record.exc_info and len(record.exc_info) > 1 else None
        is_orphaned = _ORPHANED_FUTURE_MESSAGE in message or _ORPHANED_TASK_MESSAGE in message
        if is_orphaned and (
            _CHANNEL_COLLECTED_ERROR in message or (exc is not None and _CHANNEL_COLLECTED_ERROR in str(exc))
        ):
            return False
        if _ORPHANED_FUTURE_MESSAGE not in message:
            return True
        if _DRIVER_PIPE_CLOSED_ERROR in message or (exc is not None and _DRIVER_PIPE_CLOSED_ERROR in str(exc)):
            return False
        if exc is not None:
            return type(exc).__name__ != _TARGET_CLOSED_ERROR_TYPE
        # No exception object to type-check (message-only record) — fall back to the text.
        return _TARGET_CLOSED_ERROR not in message


def _get_entrypoint() -> str:
    """Derive a human-readable entrypoint name for the current process.

    For ``python -m skyvern.forge`` → ``skyvern.forge``
    For ``python scripts/take_screenshot_worker.py`` → ``take_screenshot_worker``
    """
    # For -m invocations, __spec__ gives the clean module name.
    main_mod = sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None) if main_mod else None
    spec_name = getattr(spec, "name", None) if spec else None
    if spec_name and spec_name != "__main__":
        if spec_name.endswith(".__main__"):
            spec_name = spec_name[: -len(".__main__")]
        return spec_name

    # For direct script / uvicorn-reload invocations, use sys.argv[0].
    if sys.argv and sys.argv[0] not in ("-c", "-m"):
        argv0 = Path(sys.argv[0])
        # Handle __main__.py paths (e.g. /path/to/skyvern/forge/__main__.py → skyvern.forge)
        if argv0.name == "__main__.py":
            # Walk up through Python packages (directories with __init__.py)
            parts: list[str] = []
            current = argv0.parent
            while (current / "__init__.py").exists():
                parts.append(current.name)
                current = current.parent
            if parts:
                return ".".join(reversed(parts))
            # Namespace package (no __init__.py) — use the directory name
            return argv0.parent.name
        return argv0.stem

    return "unknown"


def _add_entrypoint(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Inject the process entrypoint into every log event (runs for both JSON and console)."""
    event_dict["entrypoint"] = _entrypoint
    return event_dict


# Datadog intake preprocessing derives the log's severity/message/host/service/source from
# these reserved attribute names, so a domain kwarg like status="completed" becomes the
# log's severity (`c*` → critical). Renamed at the render seam only: `context.log` (the S3
# run artifact) and the console renderer keep the authored names.
# `severity` is here because it sits between `status` and `level` in Datadog's status-attribute
# list, so stripping only `status` would promote it to the severity source. `msg` is
# deliberately absent: this runs after EventRenamer has moved the real message there, so
# renaming it would strip the message off every line.
RESERVED_LOG_KEY_RENAMES: dict[str, str] = {
    "status": "event_status",
    "severity": "event_severity",
    "message": "event_message",
    "host": "event_host",
    "hostname": "event_hostname",
    "service": "event_service",
    "source": "event_source",
}


def escape_reserved_log_keys(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    for key, renamed in RESERVED_LOG_KEY_RENAMES.items():
        if key in event_dict:
            # setdefault: an explicitly authored `event_*` kwarg wins over the rename.
            event_dict.setdefault(renamed, event_dict.pop(key))
    return event_dict


# Also appended to `msg` so pasting an id (pbs_/wr_/tsk_/...) into Datadog free-text
# search still matches — attribute JSON alone only matches `@field:value` queries.
# Bounded to these short ids on purpose: copying arbitrary kwargs into `msg` is what
# fragmented oversized logs (SKY-13848).
SEARCHABLE_LOG_ID_KEYS: tuple[str, ...] = (
    "request_id",
    "organization_id",
    "organization_name",
    "step_id",
    "task_id",
    "run_id",
    "workflow_id",
    "workflow_run_id",
    "workflow_permanent_id",
    "task_v2_id",
    "browser_session_id",
    "copilot_session_id",
    "browser_container_ip",
    "browser_container_task_arn",
)
_SEARCHABLE_ID_MAX_CHARS = 256


def add_log_context(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add request and process context, appending only the correlation ids to ``msg``."""
    # Add context to the log
    context = skyvern_context.current()
    if context:
        if getattr(context, "request_id", None):
            event_dict["request_id"] = context.request_id
        if getattr(context, "organization_id", None):
            event_dict["organization_id"] = context.organization_id
        if getattr(context, "organization_name", None):
            event_dict["organization_name"] = context.organization_name
        if getattr(context, "step_id", None):
            event_dict["step_id"] = context.step_id
        if getattr(context, "task_id", None):
            event_dict["task_id"] = context.task_id
        if getattr(context, "run_id", None):
            event_dict["run_id"] = context.run_id
        if getattr(context, "workflow_id", None):
            event_dict["workflow_id"] = context.workflow_id
        if getattr(context, "workflow_run_id", None):
            event_dict["workflow_run_id"] = context.workflow_run_id
        if getattr(context, "workflow_permanent_id", None):
            event_dict["workflow_permanent_id"] = context.workflow_permanent_id
        if getattr(context, "task_v2_id", None):
            event_dict["task_v2_id"] = context.task_v2_id
        if getattr(context, "browser_session_id", None):
            event_dict["browser_session_id"] = context.browser_session_id
        if getattr(context, "copilot_session_id", None):
            event_dict["copilot_session_id"] = context.copilot_session_id
        if getattr(context, "codeblock_execution_path", None):
            event_dict["codeblock_execution_path"] = context.codeblock_execution_path
        if getattr(context, "browser_container_ip", None):
            event_dict["browser_container_ip"] = context.browser_container_ip
        if getattr(context, "browser_container_task_arn", None):
            event_dict["browser_container_task_arn"] = context.browser_container_task_arn

    # Add process-level context to the log
    event_dict["env"] = settings.ENV
    event_dict["version"] = __version__

    searchable_ids = [
        f"{key}={value[:_SEARCHABLE_ID_MAX_CHARS]}"
        for key in SEARCHABLE_LOG_ID_KEYS
        if isinstance((value := event_dict.get(key)), str) and value
    ]
    msg = event_dict.get("msg")
    if searchable_ids and isinstance(msg, str):
        event_dict["msg"] = f"{msg} | {', '.join(searchable_ids)}"

    return event_dict


def _truncate_log_value(value: Any, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, float)):
        return value
    if isinstance(value, int):
        value = int(value)
    text = value if isinstance(value, str) else str(value)
    if isinstance(value, int) and len(text) <= max_chars:
        return value
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated]"


def render_bounded_json(logger: logging.Logger, method_name: str, event_dict: EventDict) -> str:
    """Render one valid JSON record below the collector's observed split boundary."""
    rendered = _JSON_RENDERER(logger, method_name, event_dict)
    # JSONRenderer's default serializer escapes non-ASCII characters, so character
    # length is also the emitted UTF-8 byte length without allocating another copy.
    original_size_bytes = len(rendered)
    if original_size_bytes <= MAX_JSON_LOG_BYTES:
        return rendered

    bounded = {
        key: _truncate_log_value(
            event_dict[key],
            _OVERSIZED_LOG_VALUE_CHARS if key in {"msg", "exception"} else _OVERSIZED_LOG_METADATA_CHARS,
        )
        for key in _OVERSIZED_LOG_FIELDS
        if key in event_dict
    }
    omitted_fields = sorted(str(key)[:128] for key in event_dict if key not in bounded)
    bounded.update(
        {
            "log_truncated": True,
            "original_size_bytes": original_size_bytes,
            "omitted_field_count": len(omitted_fields),
            "omitted_fields": omitted_fields[:50],
        }
    )
    rendered = _JSON_RENDERER(logger, method_name, bounded)
    if len(rendered) <= MAX_JSON_LOG_BYTES:
        return rendered

    # Unusual escaped/control-heavy metadata can expand during JSON encoding. Keep a
    # minimal correlated record rather than emitting another line the collector splits.
    minimal = {
        key: _truncate_log_value(bounded[key], 256 if key == "msg" else 128)
        for key in _OVERSIZED_LOG_FIELDS
        if key in bounded and key != "exception"
    }
    minimal.update(
        {
            "log_truncated": True,
            "original_size_bytes": original_size_bytes,
            "omitted_field_count": sum(1 for key in event_dict if key not in minimal),
        }
    )
    rendered = _JSON_RENDERER(logger, method_name, minimal)
    if len(rendered) <= MAX_JSON_LOG_BYTES:
        return rendered
    return _JSON_RENDERER(
        logger,
        method_name,
        {
            "msg": "Oversized log record",
            "level": _truncate_log_value(event_dict.get("level"), 32),
            "log_truncated": True,
            "original_size_bytes": original_size_bytes,
            "omitted_field_count": len(event_dict),
        },
    )


def redact_registered_secrets(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Redact credential values the copilot filled during a turn from every string in the event dict.

    Imported lazily: this module is imported far earlier in boot than the copilot package.
    """
    from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER, all_registered_secret_values

    secrets = all_registered_secret_values()
    if not secrets:
        return event_dict

    def scrub(node: Any) -> Any:
        # Structured nested values are rendered in full, so recurse before JSON serialization.
        if isinstance(node, str):
            for secret in secrets:
                if secret in node:
                    node = node.replace(secret, REDACTED_SECRET_PLACEHOLDER)
            return node
        if isinstance(node, dict):
            return {key: scrub(item) for key, item in node.items()}
        if isinstance(node, list):
            return [scrub(item) for item in node]
        if isinstance(node, tuple):
            return tuple(scrub(item) for item in node)
        return node

    for key, value in list(event_dict.items()):
        event_dict[key] = scrub(value)
    return event_dict


def redact_codeblock_parameters(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    del logger, method_name
    message_key = "event" if "event" in event_dict else "msg" if "msg" in event_dict else None
    redacted = _redact_codeblock_log_value(event_dict)
    if not isinstance(redacted, dict) or (message_key is not None and message_key not in redacted):
        return {message_key or "event": ""}
    return redacted


def redact_bearer_tokens(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Redact Bearer credentials from every top-level string value in the event dict.

    Covers `?token=Bearer%20<jwt>` query strings (e.g. WebSocket connection URLs),
    `Authorization: Bearer <token>` header values, and bare `Bearer <token>` runs in
    exception strings. Bearer credentials nested inside structured kwargs are handled
    by ``redact_sensitive_event_fields`` below, which recurses into their strings.
    """
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = redact_bearer_tokens_in_text(value)
    return event_dict


def redact_sensitive_event_fields(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive-named kwargs (auth headers, tokens, credentials) before rendering.

    Reuses the shared field redactor so structured kwargs such as
    ``headers={"Authorization": ...}``, ``payload={...}``, or ``response_body={...}``
    are masked before JSON serialization.
    Top-level keys whose name is sensitive are masked outright; every other
    non-string value is redacted recursively — models, tuples and sets included,
    since the formatter renders those in full too. Plain string values are left to
    the bearer / registered-secret redactors above.

    Each kwarg is guarded independently: a caller-supplied container whose iteration
    raises must not take down the whole log call, so it fails closed to ``REDACTED``.
    """
    for key, value in list(event_dict.items()):
        try:
            if is_sensitive_key(key):
                event_dict[key] = REDACTED
            elif is_proxy_observability_key(key):
                event_dict[key] = redact_proxy_observability_value(key, value)
            elif not isinstance(value, str):
                event_dict[key] = redact_sensitive_fields(value)
        except Exception:
            event_dict[key] = REDACTED
    return event_dict


def _compact_action(action: Any) -> Any:
    """The few fields a log line needs to correlate. Anything else belongs in the database."""
    if isinstance(action, (str, int, float, bool, dict, list, type(None))):
        return action
    return {
        "id": getattr(action, "action_id", None),
        "type": str(getattr(action, "action_type", type(action).__name__)),
        "element_id": getattr(action, "element_id", None),
    }


def compact_action_objects(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Compact verbose Action / ActionResult kwargs to a few key fields.

    LOG.info(..., action=action, ...) was emitting ~3 KB per line because the
    Action dataclass dumps 25+ attributes including LLM-generated reasoning /
    intention / response strings. Full action data is persisted to the DB and
    queryable there; logs only need enough to correlate.

    ``actions=`` is handled the same way for the same reason: a batch of ten renders ten full
    reprs. It happens to keep a signed ``file_url`` out of that one shape, but this is a LOG VOLUME
    control and NOT a redaction control — it inspects two fixed keys and nothing else, so no part of
    it should be relied on to keep credentials out of logs. Credential redaction across the logging
    stack is a separate concern with its own ticket.
    """
    action = event_dict.get("action")
    if action is not None and not isinstance(action, (str, int, float, bool, dict, list, type(None))):
        try:
            event_dict["action"] = _compact_action(action)
        except Exception:
            pass

    actions = event_dict.get("actions")
    if isinstance(actions, (list, tuple)) and actions:
        try:
            event_dict["actions"] = [_compact_action(item) for item in actions]
        except Exception:
            pass

    action_result = event_dict.get("action_result")
    if isinstance(action_result, list) and action_result:
        try:
            event_dict["action_result"] = {
                "count": len(action_result),
                "success": all(getattr(r, "success", False) for r in action_result),
            }
        except Exception:
            pass

    return event_dict


def skyvern_logs_processor(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    A custom processor to add skyvern logs to the context
    """
    if method_name not in ["info", "warning", "error", "critical", "exception"]:
        return event_dict

    context = skyvern_context.current()
    if context:
        log_entry = dict(event_dict)
        context.log.append(log_entry)

    return event_dict


def sample_logs_processor(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """Probabilistically drop INFO call sites marked ``sampling=True`` for configured orgs.

    Placed after ``skyvern_logs_processor`` so the full line is still captured in
    ``context.log`` (persisted to the per-run S3 log artifact); only the stdout /
    Datadog stream is thinned. The ``sampling`` marker never ships downstream, and
    WARN/ERROR are never dropped even when marked.
    """
    if not event_dict.pop("sampling", False):
        return event_dict
    if method_name != "info":
        return event_dict

    context = skyvern_context.current()
    organization_id = context.organization_id if context else None
    if organization_id not in settings.LOG_SAMPLING_ORG_IDS:
        return event_dict

    if random.random() < settings.LOG_SAMPLING_RATE:
        return event_dict
    raise structlog.DropEvent


def add_filename_section(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    Add a fixed-width, bracketed filename:lineno section after the log level for console logs.
    """
    filename = event_dict.get("filename", "")
    lineno = event_dict.get("lineno", "")
    padded = f"[{filename:<30}:{lineno:<4}]" if filename else "[unknown        ]"
    event_dict["file"] = padded
    event_dict.pop("filename", None)
    event_dict.pop("lineno", None)
    return event_dict


class CustomConsoleRenderer(structlog.dev.ConsoleRenderer):
    """
    Show the bracketed filename:lineno section after the log level for console logs, and
    colorize it.
    """

    def __init__(self) -> None:
        super().__init__(sort_keys=False)

    def __call__(self, logger: logging.Logger, name: str, event_dict: EventDict) -> str:
        file_section = event_dict.pop("file", "")
        file_section_colored = f"\x1b[90m{file_section}\x1b[0m" if file_section else ""
        rendered = super().__call__(logger, name, event_dict)
        first_bracket = rendered.find("]")

        if first_bracket != -1:
            return rendered[: first_bracket + 1] + f" {file_section_colored}" + rendered[first_bracket + 1 :]
        else:
            return f"{file_section_colored} {rendered}"


def add_error_processor(logger: logging.Logger, method_name: str, event_dict: EventDict) -> EventDict:
    """
    A custom processor extending error logs with additional info
    """
    exc_info = event_dict.get("exc_info")

    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()

        if isinstance(exc_info, tuple) and len(exc_info) >= 2:
            exc_type = exc_info[0]
            exc_traceback: TracebackType | None = exc_info[2] if len(exc_info) >= 3 else None

            if exc_type is not None:
                # Get the fully qualified exception name (module.ClassName)
                error_type = (
                    f"{exc_type.__module__}.{exc_type.__name__}"
                    if hasattr(exc_type, "__module__")
                    else exc_type.__name__
                )
                event_dict["error_type"] = error_type

                # Categorize the exception
                category = _categorize_exception(exc_type, exc_type.__name__)
                event_dict["error_category"] = category

                # Generate exception hash from stack trace (stable identifier)
                if exc_traceback is not None:
                    exc_hash = _generate_exception_hash(exc_type, exc_traceback)
                    event_dict["exception_hash"] = exc_hash

    return event_dict


def exception_log_fields(exc: BaseException) -> dict[str, str]:
    """The error_type/error_category/exception_hash fields add_error_processor derives from exc_info.

    For callsites that want these dashboard fields on a log line that deliberately omits the
    traceback (e.g. a warning downgraded from an exception log).
    """
    exc_type = type(exc)
    fields = {
        "error_type": f"{exc_type.__module__}.{exc_type.__name__}",
        "error_category": _categorize_exception(exc_type, exc_type.__name__),
    }
    if exc.__traceback__ is not None:
        fields["exception_hash"] = _generate_exception_hash(exc_type, exc.__traceback__)
    return fields


def _generate_exception_hash(exc_type: type, tb: TracebackType) -> str:
    """
    Generate a stable hash for an exception based on:
    - Exception type
    - Stack trace (filename, line number, function name)

    Excludes dynamic data like error messages to ensure the same
    error from the same location always produces the same hash.
    """
    import hashlib  # noqa: PLC0415

    hasher = hashlib.sha256()

    hasher.update(f"{exc_type.__module__}.{exc_type.__name__}".encode())

    current_tb: TracebackType | None = tb
    while current_tb is not None:
        frame = current_tb.tb_frame
        code = frame.f_code

        filename = Path(code.co_filename).name
        lineno = current_tb.tb_lineno
        func_name = code.co_name
        hasher.update(f"{filename}:{lineno}:{func_name}".encode())

        current_tb = current_tb.tb_next

    return hasher.hexdigest()[:16]


def _categorize_exception(exc_type: type, exc_name: str) -> str:
    """
    Categorize an exception into TRANSIENT, BUG, or ERROR.

    TRANSIENT: Network/IO errors that might succeed on retry
    BUG: Programming errors indicating bugs
    ERROR: Everything else
    """
    # Check if it's a subclass of known exception types
    # TRANSIENT - IO and network related errors
    transient_exceptions = (
        IOError,
        OSError,
        ConnectionError,
        TimeoutError,
        ConnectionRefusedError,
        ConnectionAbortedError,
        ConnectionResetError,
        BrokenPipeError,
    )

    # BUG - Programming errors that indicate bugs
    bug_exceptions = (
        ZeroDivisionError,
        AttributeError,
        TypeError,
        KeyError,
        IndexError,
        NameError,
        AssertionError,
        NotImplementedError,
        RecursionError,
        UnboundLocalError,
        IndentationError,
        SyntaxError,
    )

    # Check for common HTTP/network library exceptions by name
    # (to avoid import dependencies)
    transient_patterns = [
        "HTTPError",
        "RequestException",
        "Timeout",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "ProxyError",
        "SSLError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "StreamConsumedError",
        "RetryError",
        "MaxRetryError",
        "URLError",
        "ProtocolError",
    ]

    # Check if exception is a subclass of transient exceptions
    try:
        if issubclass(exc_type, transient_exceptions):
            return "TRANSIENT"
    except TypeError:
        pass

    # Check if exception is a subclass of bug exceptions
    try:
        if issubclass(exc_type, bug_exceptions):
            return "BUG"
    except TypeError:
        pass

    # Check exception name against patterns
    for pattern in transient_patterns:
        if pattern in exc_name:
            return "TRANSIENT"

    # Default to ERROR for everything else
    return "ERROR"


_INTERPRETER_TRACEBACK_HOOK_MARKER = "_skyvern_interpreter_traceback_hook"
_UNRAISABLE_OBJECT_REPR_CHARS = 512
_INTERPRETER_HOOK_REENTRY = threading.local()


def _log_interpreter_traceback(
    msg: str,
    exc_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None],
    **fields: Any,
) -> bool:
    """Emit one structured record for a traceback the interpreter would print to stderr.

    Returns False when the caller must fall back to the hook it replaced.
    """
    exc_type, exc_value, exc_traceback = exc_info
    if exc_type is None or exc_value is None:
        return False
    # These hooks fire from arbitrary garbage-collection points, so emitting can itself trigger a
    # collection whose finalizer raises another unraisable. Without this guard that recurses until
    # the stack is exhausted.
    if getattr(_INTERPRETER_HOOK_REENTRY, "active", False):
        return False
    _INTERPRETER_HOOK_REENTRY.active = True
    try:
        logging.getLogger("skyvern.interpreter").error(
            msg,
            exc_info=(exc_type, exc_value, exc_traceback),
            extra={key: value for key, value in fields.items() if value is not None},
        )
    except Exception:
        return False
    finally:
        _INTERPRETER_HOOK_REENTRY.active = False
    return True


def _unraisable_object_repr(obj: Any) -> str | None:
    # CPython folds the object's repr into err_msg instead of setting `object` in some finalizer
    # paths, so None here is normal and the field is dropped rather than rendered as "None".
    if obj is None:
        return None
    try:
        return str(_truncate_log_value(repr(obj), _UNRAISABLE_OBJECT_REPR_CHARS))
    except BaseException:
        return "<unrepresentable>"


def _install_interpreter_traceback_hooks() -> None:
    """Route the tracebacks the interpreter writes straight to stderr through the logging stack.

    ``sys.excepthook``, ``threading.excepthook`` and ``sys.unraisablehook`` bypass ``logging``
    entirely and print a multi-line traceback to stderr. A line-oriented collector bills each
    frame as its own event, so one exception lands as ~30 unstitched events carrying none of the
    ``error_type``/``exception_hash`` fields ``add_error_processor`` derives — which is what
    ``exception_hash`` grouping and any error-rate threshold on the service are read against.
    """
    previous_excepthook = sys.excepthook
    if not getattr(previous_excepthook, _INTERPRETER_TRACEBACK_HOOK_MARKER, False):

        def excepthook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_traceback: TracebackType | None,
        ) -> None:
            if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) or not _log_interpreter_traceback(
                "Uncaught exception", (exc_type, exc_value, exc_traceback)
            ):
                previous_excepthook(exc_type, exc_value, exc_traceback)

        setattr(excepthook, _INTERPRETER_TRACEBACK_HOOK_MARKER, True)
        sys.excepthook = excepthook

    previous_threading_excepthook = threading.excepthook
    if not getattr(previous_threading_excepthook, _INTERPRETER_TRACEBACK_HOOK_MARKER, False):
        # Typed Any like codeblock_runner's unraisable hook: neither hook argument has a type
        # CPython exposes at runtime, and these annotations are evaluated at import time.
        def threading_excepthook(args: Any) -> None:
            # threading's default hook drops SystemExit silently; keep that.
            if args.exc_type is SystemExit or not _log_interpreter_traceback(
                "Uncaught exception in thread",
                (args.exc_type, args.exc_value, args.exc_traceback),
                thread_name=args.thread.name if args.thread is not None else None,
            ):
                previous_threading_excepthook(args)

        setattr(threading_excepthook, _INTERPRETER_TRACEBACK_HOOK_MARKER, True)
        threading.excepthook = threading_excepthook

    previous_unraisable_hook = sys.unraisablehook
    if not getattr(previous_unraisable_hook, _INTERPRETER_TRACEBACK_HOOK_MARKER, False):

        def unraisablehook(unraisable: Any) -> None:
            # The default renders the object into the message; keeping it in its own field leaves
            # `msg` low-cardinality enough to group on.
            if not _log_interpreter_traceback(
                "Exception ignored in interpreter callback",
                (unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback),
                unraisable_err_msg=unraisable.err_msg,
                unraisable_object=_unraisable_object_repr(unraisable.object),
            ):
                previous_unraisable_hook(unraisable)

        setattr(unraisablehook, _INTERPRETER_TRACEBACK_HOOK_MARKER, True)
        sys.unraisablehook = unraisablehook


def setup_logger() -> None:
    """
    Setup the logger with the specified format
    """
    global _entrypoint  # noqa: PLW0603
    _entrypoint = _get_entrypoint()

    # logging.config.dictConfig(logging_config)
    renderer = render_bounded_json if settings.JSON_LOGGING else CustomConsoleRenderer()
    additional_processors = (
        [
            redact_bearer_tokens,
            redact_registered_secrets,
            # After compaction: that pass is a log-volume control that trims Action
            # models down to a few fields, and redaction would otherwise expand them
            # into full dicts before it ran.
            compact_action_objects,
            redact_sensitive_event_fields,
            redact_codeblock_parameters,
            structlog.processors.EventRenamer("msg"),
            add_log_context,
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.PATHNAME,
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            ),
        ]
        if settings.JSON_LOGGING
        else [
            redact_bearer_tokens,
            redact_registered_secrets,
            compact_action_objects,
            redact_sensitive_event_fields,
            redact_codeblock_parameters,
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            ),
            add_filename_section,
        ]
    )
    LOG_LEVEL_VAL = LOGGING_LEVEL_MAP.get(settings.LOG_LEVEL, logging.INFO)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(LOG_LEVEL_VAL),
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_entrypoint,
            add_error_processor,
            structlog.processors.format_exc_info,
        ]
        + additional_processors
        + [skyvern_logs_processor, sample_logs_processor, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
    )
    # Foreign stdlib records never run the structlog chain above, so without these two a
    # record reaches Datadog with an empty message (its remapper reads `msg`, not `event`)
    # and no `organization_id` to group on.
    foreign_msg_chain: list[Processor] = (
        [structlog.processors.EventRenamer("msg"), add_log_context] if settings.JSON_LOGGING else []
    )

    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                # Without this, `extra={...}` on a stdlib log call is silently dropped
                # and never becomes a queryable attribute. Modules that run in images
                # without structlog installed have no other route to structured fields.
                structlog.stdlib.ExtraAdder(),
                add_error_processor,
                structlog.processors.format_exc_info,
                redact_sensitive_event_fields,
            ]
            + foreign_msg_chain,
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.TimeStamper(fmt="iso"),
                # Every record on this handler — native structlog AND foreign stdlib (temporal,
                # asyncio, sqlalchemy, uvicorn) — is serialized here, so this is the one seam that
                # covers both. `format_exc_info` in `foreign_pre_chain` has already rendered
                # exc_info to a string by now, so a secret in the exception text is reachable.
                # These stay duplicated in the structlog chain above on purpose: that pass also
                # guards `context.log`, which is persisted to the per-run S3 log artifact.
                # Native records are already redacted before `context.log`; foreign records get
                # their one field-redaction pass in `foreign_pre_chain`. Keep this shared
                # renderer chain unchanged so native structured values are not walked twice.
                redact_bearer_tokens,
                redact_registered_secrets,
                redact_codeblock_parameters,
                *([escape_reserved_log_keys] if settings.JSON_LOGGING else []),
                renderer,
            ],
        )
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    # Root at WARNING so third-party loggers (temporalio, grpc, litellm, …)
    # only surface warnings and errors.  Our packages get the configured level.
    root_logger.setLevel(logging.WARNING)
    for name in ("skyvern", "cloud", "workers", "scripts", "browser_controller", "codeblock"):
        logging.getLogger(name).setLevel(LOG_LEVEL_VAL)

    # uvicorn calls logging.config.dictConfig(LOGGING_CONFIG) during its own
    # startup, which RESETS the disabled flag on these loggers back to False.
    # setLevel() survives because uvicorn's default config sets level=INFO,
    # which is below WARNING/CRITICAL — our higher levels stay in effect when
    # __main__.py also passes a no-op log_config to uvicorn.run().
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)

    # Suppress noisy websockets library INFO logs ("connection open", "connection closed")
    # These are high-volume and not useful for debugging
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("websockets.legacy").setLevel(logging.WARNING)
    logging.getLogger("websockets.legacy.server").setLevel(logging.WARNING)

    # Anthropic Bedrock SDK emits high-volume WARN noise; keep only its errors.
    logging.getLogger("anthropic").setLevel(logging.ERROR)

    # Mute LiteLLM's high-volume library logs; our own LLM handler already logs calls/errors.
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Proxy").setLevel(logging.CRITICAL)

    # The OTLP gRPC exporter logs a WARNING per retry and an ERROR per dropped batch when its
    # endpoint is unreachable; raise its threshold via OTEL_EXPORTER_LOG_LEVEL (default WARNING) to
    # drop that spam where the endpoint is intentionally unavailable, keeping it visible elsewhere.
    logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(
        LOGGING_LEVEL_MAP.get(settings.OTEL_EXPORTER_LOG_LEVEL.upper(), logging.WARNING)
    )

    # Drop asyncio's orphaned-future noise from torn-down Playwright driver pipes (logged at
    # ERROR but non-actionable). setup_logger may run more than once (uvicorn reload), so keep
    # exactly one instance instead of stacking duplicates.
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.filters = [f for f in asyncio_logger.filters if not isinstance(f, _DriverPipeNoiseFilter)]
    asyncio_logger.addFilter(_DriverPipeNoiseFilter())

    # CPython prints "coroutine ... was never awaited" from the coroutine's __del__, so the
    # file:line it carries is wherever the collector happened to run -- botocore, asyncio, sys:1 --
    # and never the code that dropped it. Origin tracking makes the warning name the creating frame
    # instead; depth 1 measures at +57ns per coroutine creation on 3.11, 78.8 -> 136.0 (SKY-15069).
    sys.set_coroutine_origin_tracking_depth(1)

    # Last, so the handler these hooks log through is already installed.
    _install_interpreter_traceback_hooks()
