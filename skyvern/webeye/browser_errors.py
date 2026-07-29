"""Engine-neutral browser-error taxonomy and classifier.

Skyvern can select among several browser engines (for example stock Playwright or a
private engine), each of which raises its own native error classes with distinct
identities. This module owns a taxonomy that recovery code can branch on without
importing any engine, plus a classifier parameterized by the *selected* engine's
native error families. It deliberately imports no browser driver at module scope so
it stays driver-agnostic and reusable by every adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from skyvern.exceptions import SkyvernException, redact_cdp_endpoint_urls, redact_ws_endpoint_urls

ExceptionType = type[BaseException]
ErrorPredicate = Callable[[BaseException], bool]


class BrowserAutomationError(SkyvernException):
    """Engine-neutral base for a browser failure classified from a driver-native error."""


class BrowserTimeoutError(BrowserAutomationError):
    """A driver operation exceeded its deadline."""


class BrowserTargetClosedError(BrowserAutomationError):
    """The target page, frame, context, or browser was closed underneath an operation."""


class BrowserCdpConnectionError(BrowserAutomationError):
    """A CDP/transport-level failure talking to the browser."""


class BrowserRetryableCdpError(BrowserCdpConnectionError):
    """A CDP/transport failure the caller may retry (e.g. a transient disconnect)."""


class BrowserErrorFamiliesConfigError(ValueError):
    """Raised when an engine's error-family configuration is invalid or ambiguous."""


_FAMILY_TYPE_FIELDS = (
    "timeout_types",
    "target_closed_types",
    "cdp_connection_types",
    "retryable_types",
    "base_error_types",
)


@dataclass(frozen=True)
class BrowserEngineErrorFamilies:
    """Native error classes/predicates for one selected engine, grouped by taxonomy; a native type
    appearing in more than one family is rejected at construction time (rather than resolved by
    isinstance ordering) to keep classification unambiguous, and the ``cdp_connection_predicate`` /
    ``retryable_predicate`` run on *every* exception that reaches their step unmatched by an earlier
    family's types, so a predicate must scope itself by the engine's identity (type-check, or match
    transport signals only that engine emits) — one keyed solely on message substrings will also
    claim unrelated exceptions carrying the same text.
    """

    timeout_types: tuple[ExceptionType, ...] = ()
    target_closed_types: tuple[ExceptionType, ...] = ()
    cdp_connection_types: tuple[ExceptionType, ...] = ()
    retryable_types: tuple[ExceptionType, ...] = ()
    base_error_types: tuple[ExceptionType, ...] = ()
    cdp_connection_predicate: ErrorPredicate | None = None
    retryable_predicate: ErrorPredicate | None = None

    def __post_init__(self) -> None:
        seen: dict[ExceptionType, str] = {}
        for field_name in _FAMILY_TYPE_FIELDS:
            for entry in getattr(self, field_name):
                if not (isinstance(entry, type) and issubclass(entry, BaseException)):
                    raise BrowserErrorFamiliesConfigError(
                        f"{field_name} entry {entry!r} is not a BaseException subclass"
                    )
                prior = seen.get(entry)
                if prior is not None:
                    location = f"twice in {field_name}" if prior == field_name else f"in both {prior} and {field_name}"
                    raise BrowserErrorFamiliesConfigError(
                        f"{entry.__name__} appears {location}; a native error type may only belong to one family"
                    )
                seen[entry] = field_name


def _matches(exc: BaseException, types: tuple[ExceptionType, ...], predicate: ErrorPredicate | None) -> bool:
    if types and isinstance(exc, types):
        return True
    return predicate is not None and predicate(exc)


def _wrap(cls: type[BrowserAutomationError], exc: BaseException) -> BrowserAutomationError:
    """Wrap a native error as ``cls``, redacting browser-endpoint URLs from ``.message`` while keeping
    the raw native error on ``__cause__`` for logs and diagnostics. A ws/wss devtools socket URL is
    unambiguously a CDP endpoint, so it is redacted for every family; an http/https URL is only known
    to be a CDP discovery endpoint (rather than an ordinary navigation/target/proxy URL the user wants
    to see) when ``cls`` is a CDP-connection family, so http(s) redaction is scoped there.
    """
    raw = str(exc)
    redacted = (
        redact_cdp_endpoint_urls(raw) if issubclass(cls, BrowserCdpConnectionError) else redact_ws_endpoint_urls(raw)
    )
    error = cls(redacted)
    error.__cause__ = exc
    return error


def classify_browser_error(
    exc: BaseException,
    families: BrowserEngineErrorFamilies,
) -> BrowserAutomationError | None:
    """Map a native engine error to the taxonomy, or return ``None`` if unrecognized. Precedence is
    most-specific first — retryable CDP, target closed, timeout, CDP transport, then the engine's
    generic base error — so a type matching several families via inheritance (e.g. a timeout that
    subclasses the base error) resolves to the earliest match; ``None`` means the error is not in
    this engine's taxonomy and the caller must re-raise it (never swallowed or flattened into the
    base type), and positive classifications preserve the original native exception as ``__cause__``.
    """
    if _matches(exc, families.retryable_types, families.retryable_predicate):
        return _wrap(BrowserRetryableCdpError, exc)
    if _matches(exc, families.target_closed_types, None):
        return _wrap(BrowserTargetClosedError, exc)
    if _matches(exc, families.timeout_types, None):
        return _wrap(BrowserTimeoutError, exc)
    if _matches(exc, families.cdp_connection_types, families.cdp_connection_predicate):
        return _wrap(BrowserCdpConnectionError, exc)
    if _matches(exc, families.base_error_types, None):
        return _wrap(BrowserAutomationError, exc)
    return None
