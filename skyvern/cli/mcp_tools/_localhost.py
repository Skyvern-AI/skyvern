"""Localhost URL detection for cloud browser sessions."""

from __future__ import annotations

from urllib.parse import urlparse

_LOCALHOST_HOSTNAMES = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",  # noqa: S104 — detection, not binding
        "::1",
        "[::1]",
    }
)


def is_localhost_url(url: str) -> bool:
    """Return True if *url* points to a loopback address."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return hostname in _LOCALHOST_HOSTNAMES
    except Exception:
        return False
