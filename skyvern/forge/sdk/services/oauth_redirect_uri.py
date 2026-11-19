"""Exact matching of an OAuth ``redirect_uri`` against the URIs registered for a client.

Both values must pass the registration rules, then the candidate must equal a registered entry
character for character, as Google and Microsoft compare them. Query, fragment, userinfo,
dot segments and percent-encoded paths are refused outright, so no entry can be widened into
a prefix or a host allowlist. Loopback redirects get no port variance: these are
web-application clients, and RFC 8252 section 7.3 port variance applies only to native clients.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

import structlog

from skyvern.config import settings

LOG = structlog.get_logger()

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_SCHEMES = frozenset({"https", "http"})
_warned_invalid_entries: set[str] = set()


def _rejection(redirect_uri: str) -> str | None:
    """Return the reason ``redirect_uri`` breaks the registration rules, or ``None`` when it is acceptable."""
    if "?" in redirect_uri or "#" in redirect_uri:
        return "redirect_uri must not carry a query or fragment"
    if "\\" in redirect_uri:
        return "redirect_uri must not contain backslashes"
    # urlsplit silently drops tab/CR/LF, so the checked value would differ from the one sent to the provider.
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in redirect_uri):
        return "redirect_uri must not contain whitespace or control characters"
    try:
        parsed = urlsplit(redirect_uri)
        host = parsed.hostname
        _ = parsed.port  # raises ValueError for an out-of-range or non-numeric port
    except ValueError as exc:
        return f"Invalid redirect_uri: {exc}"
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return "redirect_uri must use http or https"
    if not host:
        return "redirect_uri must name a host"
    if parsed.username is not None or parsed.password is not None:
        return "redirect_uri must not contain userinfo"
    path = parsed.path or "/"
    if any(unquote(segment) in (".", "..") for segment in path.split("/")):
        return "redirect_uri must not contain dot segments"
    if "%" in path:
        return "redirect_uri path must not contain percent-encoding"
    if scheme != "https" and host not in LOOPBACK_HOSTS:
        return f"redirect_uri must use https for non-loopback host: {host}"
    return None


def registered_redirect_uris(entries: Iterable[str]) -> list[str]:
    """Return the trimmed entries that pass the registration rules; invalid entries are dropped rather than widened."""
    registered: list[str] = []
    for entry in entries:
        if not entry or not entry.strip():
            continue
        rejection = _rejection(entry.strip())
        if rejection is None:
            registered.append(entry.strip())
        elif entry not in _warned_invalid_entries:
            _warned_invalid_entries.add(entry)
            LOG.warning("Ignoring invalid registered OAuth redirect URI", redirect_uri=entry, reason=rejection)
    return registered


def registration_rejection(entry: str) -> str | None:
    """Return why ``entry`` cannot be registered as an exact redirect URI, or ``None`` when it can."""
    return _rejection(entry)


def redirect_uri_rejection(redirect_uri: str, registered: Iterable[str]) -> str | None:
    """Return why ``redirect_uri`` is not one of ``registered``, or ``None`` when it matches exactly."""
    rejection = _rejection(redirect_uri)
    if rejection:
        return rejection
    if redirect_uri in registered_redirect_uris(registered):
        return None
    return "redirect_uri is not a registered redirect URI"


def warn_removed_redirect_host_settings() -> None:
    """Warn when a removed ``*_REDIRECT_HOSTS`` variable is set but its exact-URI replacement is not."""
    for removed, replacement, configured_uris in (
        ("GOOGLE_OAUTH_REDIRECT_HOSTS", "GOOGLE_OAUTH_REDIRECT_URIS", settings.GOOGLE_OAUTH_REDIRECT_URIS),
        ("MICROSOFT_OAUTH_REDIRECT_HOSTS", "MICROSOFT_OAUTH_REDIRECT_URIS", settings.MICROSOFT_OAUTH_REDIRECT_URIS),
    ):
        if os.environ.get(removed) and not configured_uris:
            LOG.warning("Removed OAuth redirect host setting is still set", setting=removed, replacement=replacement)
