"""Exact-string scrubbing of filled credential values from logs, span exceptions and tool results.

A value filled in one turn persists in the page's input.value on the cross-turn
debug session, so registered values are also kept in a session-keyed registry to
stay scrubbed on readbacks in later turns whose per-turn context is empty.
"""

from __future__ import annotations

import html
import json
from urllib.parse import quote, quote_plus

from skyvern.forge.sdk.core import skyvern_context

REDACTED_SECRET_PLACEHOLDER = "[REDACTED_SECRET]"

_SESSION_SCRUB_VALUES: dict[str, list[str]] = {}
# The chats that registered or read each session's values, so the chat's later log lines own them
# even after its context is replaced.
_SESSION_SCRUB_CHAT_IDS: dict[str, set[str]] = {}
# A session its chat closes is dropped then; one closed by anything else is only FIFO-evicted.
_MAX_SCRUB_SESSIONS = 1024

# Substring-replacing a short value into stored content corrupts it: a six-digit OTP occurs by
# chance inside ports, counts, and ids. Rewrites of the stored workflow apply this floor — both the
# persisted row and the draft an edit anchors against, which have to stay the same string.
MIN_PERSISTED_REDACTION_LENGTH = 8

# A log line replaces another session's values inside text only from this length, because shorter ones occur by
# chance in nearly every path and id. Shorter ones still match as whole tokens, and the line's own request,
# browser session and chat apply at any length.
MIN_CROSS_SESSION_LOG_REDACTION_LENGTH = 4

_ALL_VALUES_CACHE: tuple[tuple[tuple[str, tuple[str, ...]], ...], list[str]] | None = None
# That list split at the cross-session floor, keyed on the list's identity, which changes only with the registry.
_LOG_SPLIT_CACHE: tuple[list[str], int, list[str], frozenset[str]] | None = None


def _registry_fingerprint() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Cheap key that changes on any registry mutation, including a direct one.

    Keyed by each session's values so clearing and reusing a session ID with the same value count
    cannot serve stale credentials from the prior session lifetime.

    Deriving the key from the data rather than a hand-maintained counter means a caller that
    mutates the dict directly cannot be served a stale list.
    """
    return tuple(sorted((session_id, tuple(values)) for session_id, values in _SESSION_SCRUB_VALUES.items()))


def _nonempty(value: object) -> str | None:
    # A context id can carry log provenance as a str subclass; registry keys stay plain strings.
    return str.__str__(value) if isinstance(value, str) and value else None


def _owned_scrub_values(context: object) -> set[str]:
    # Log redaction fails closed on any error, so a partial context (a test double) must not raise here.
    values = getattr(context, "copilot_scrub_values", None)
    return values if isinstance(values, set) else set()


# encodeURIComponent leaves these literal where Python's quote does not.
_ENCODE_URI_COMPONENT_SAFE = "-_.!~*'()"
# Chromium leaves these literal when it serializes a URL path, so a value in a path segment keeps them.
_CHROMIUM_URL_PATH_SAFE = "!$%&'()*+,/:;=@[]"
# Before serializing a path, Chromium turns a backslash into a slash and drops tabs and newlines.
_CHROMIUM_URL_PATH_REWRITES = str.maketrans({"\\": "/", "\t": None, "\n": None, "\r": None})


def encoded_secret_variants(value: str) -> list[str]:
    """The forms a page can echo a value in: a URL query or path, escaped markup, a JSON or Python literal."""
    try:
        forms = [
            value,
            html.escape(value, quote=False),
            html.escape(value, quote=True),
            quote(value),
            quote(value, safe=""),
            quote(value, safe=_ENCODE_URI_COMPONENT_SAFE),
            quote(value.translate(_CHROMIUM_URL_PATH_REWRITES), safe=_CHROMIUM_URL_PATH_SAFE),
            quote_plus(value, safe=""),
            json.dumps(value, ensure_ascii=True)[1:-1],
            repr(value)[1:-1],
            value.encode("unicode_escape").decode("ascii"),
        ]
    except UnicodeError:
        forms = [value]
    return list(dict.fromkeys(form for form in forms if form))


def register_session_scrub_values(session_id: str | None, variants: list[str]) -> None:
    """Add a value's encoded forms to its browser session's registry and to the current request and chat."""
    new_session = session_id is not None and session_id not in _SESSION_SCRUB_VALUES
    if session_id is not None:
        session_values = _SESSION_SCRUB_VALUES.setdefault(session_id, [])
        for variant in variants:
            if variant not in session_values:
                session_values.append(variant)
    context = skyvern_context.current()
    if context is not None:
        _owned_scrub_values(context).update(variants)
        chat_id = _nonempty(getattr(context, "copilot_session_id", None))
        if session_id is not None and chat_id is not None:
            _SESSION_SCRUB_CHAT_IDS.setdefault(session_id, set()).add(chat_id)
    if new_session:
        while len(_SESSION_SCRUB_VALUES) > _MAX_SCRUB_SESSIONS:
            clear_session_scrub_values(next(iter(_SESSION_SCRUB_VALUES)))


def read_session_scrub_values(session_id: str) -> list[str]:
    session_values = [value for value in _SESSION_SCRUB_VALUES.get(session_id, []) if isinstance(value, str) and value]
    # A turn reading values an earlier one filled can log them too, so they become this request's own, and
    # its chat's, which survives the context prepare_workflow swaps in.
    context = skyvern_context.current()
    if context is not None:
        _owned_scrub_values(context).update(session_values)
        chat_id = _nonempty(getattr(context, "copilot_session_id", None))
        if session_values and chat_id is not None:
            _SESSION_SCRUB_CHAT_IDS.setdefault(session_id, set()).add(chat_id)
    return session_values


def clear_session_scrub_values(session_id: str | None) -> None:
    if isinstance(session_id, str):
        _SESSION_SCRUB_VALUES.pop(session_id, None)
        _SESSION_SCRUB_CHAT_IDS.pop(session_id, None)


def log_scrub_values() -> tuple[list[str], frozenset[str]]:
    """A log line's values to replace inside text, longest first (its own request's, browser session's and chat's at
    any length, other sessions' from ``MIN_CROSS_SESSION_LOG_REDACTION_LENGTH``), and every shorter registered value,
    which it matches only as a whole token."""
    cross_session, short = _cross_session_log_split(all_registered_secret_values())
    owned: set[str] = set()
    context = skyvern_context.current()
    if context is not None:
        owned.update(_owned_scrub_values(context))
        browser_session_id = _nonempty(getattr(context, "browser_session_id", None))
        if browser_session_id is not None:
            owned.update(_SESSION_SCRUB_VALUES.get(browser_session_id, ()))
        chat_id = _nonempty(getattr(context, "copilot_session_id", None))
        if chat_id is not None:
            for session_id, chat_ids in list(_SESSION_SCRUB_CHAT_IDS.items()):
                if chat_id in chat_ids:
                    owned.update(_SESSION_SCRUB_VALUES.get(session_id, ()))
    if not owned:
        return cross_session, short
    values = owned.union(cross_session)
    return sorted((value for value in values if isinstance(value, str) and value), key=len, reverse=True), short


def _cross_session_log_split(values: list[str]) -> tuple[list[str], frozenset[str]]:
    global _LOG_SPLIT_CACHE
    floor = MIN_CROSS_SESSION_LOG_REDACTION_LENGTH
    cached = _LOG_SPLIT_CACHE
    if cached is not None and cached[0] is values and cached[1] == floor:
        return cached[2], cached[3]
    cross_session = [value for value in values if len(value) >= floor]
    short = frozenset(value for value in values if len(value) < floor)
    _LOG_SPLIT_CACHE = (values, floor, cross_session, short)
    return cross_session, short


def all_registered_secret_values() -> list[str]:
    """Every credential value registered by any session in this process, longest first. Rewrites of persisted
    content use only their own session's values instead, so one session's short value cannot corrupt another's data."""
    global _ALL_VALUES_CACHE
    fingerprint = _registry_fingerprint()
    cached = _ALL_VALUES_CACHE
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    merged: set[str] = set()
    for values in _SESSION_SCRUB_VALUES.values():
        merged.update(value for value in values if isinstance(value, str) and value)
    computed = sorted(merged, key=len, reverse=True)
    _ALL_VALUES_CACHE = (fingerprint, computed)
    return computed


def scrub_all_registered_from_text(text: str) -> str:
    """Session-agnostic scrub for span exceptions and model-facing text: every session's values at any length,
    unlike a log line, plus the current request's own."""
    values = all_registered_secret_values()
    owned = _owned_scrub_values(skyvern_context.current())
    if owned:
        values = sorted(owned.union(values), key=len, reverse=True)
    for value in values:
        text = text.replace(value, REDACTED_SECRET_PLACEHOLDER)
    return text
