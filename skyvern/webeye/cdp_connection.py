from __future__ import annotations

import json
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, Playwright

from skyvern.exceptions import CdpConnectionConfigurationError

LOG = structlog.get_logger()
DEFAULT_CDP_CONNECT_TIMEOUT_MS = 30_000

_CDP_DISCOVERY_ERROR_RE = re.compile(
    r"Unexpected status (?P<status>\d+) when connecting to (?P<url>https?://\S+/json/version/?)"
)


@dataclass(frozen=True)
class CdpConnectionCandidate:
    url: str
    label: str
    headers: dict[str, str] | None = None


def build_cdp_connect_headers(host_header: str | None) -> dict[str, str] | None:
    normalized_host_header = host_header.strip() if host_header else ""
    if not normalized_host_header:
        return None
    return {"Host": normalized_host_header}


def parse_default_cdp_connect_headers(raw_value: str | None) -> dict[str, str]:
    """Parse a JSON object of string-to-string headers; warn and return {} on malformed input."""
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        LOG.warning(
            "BROWSER_REMOTE_DEBUGGING_CONNECT_HEADERS is not valid JSON; ignoring",
            error=str(exc),
        )
        return {}
    if not isinstance(parsed, dict):
        LOG.warning(
            "BROWSER_REMOTE_DEBUGGING_CONNECT_HEADERS must be a JSON object; ignoring",
            json_type=type(parsed).__name__,
        )
        return {}
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            LOG.warning(
                "BROWSER_REMOTE_DEBUGGING_CONNECT_HEADERS contains a non-string entry; skipping",
                header_name=str(key),
            )
            continue
        result[key] = value
    return result


def merge_cdp_connect_headers(
    default_headers: dict[str, str],
    per_row_headers: dict[str, str] | None,
    managed_host_header: dict[str, str],
) -> dict[str, str]:
    """Merge headers with precedence defaults < per_row < managed; managed always wins.

    HTTP header names are case-insensitive, so keys colliding with the managed Host (on a
    lowercased compare) are dropped to avoid emitting a duplicate ``Host`` on the wire.
    """
    reserved_keys = {key.lower() for key in managed_host_header}
    filtered_defaults = {k: v for k, v in default_headers.items() if k.lower() not in reserved_keys}
    filtered_per_row = {k: v for k, v in (per_row_headers or {}).items() if k.lower() not in reserved_keys}
    return {**filtered_defaults, **filtered_per_row, **managed_host_header}


def parse_cdp_discovery_error(error: Exception) -> tuple[int, str] | None:
    """Return the HTTP status and discovery URL from a Playwright CDP discovery error."""
    match = _CDP_DISCOVERY_ERROR_RE.search(str(error))
    if not match:
        return None
    return int(match.group("status")), match.group("url")


def resolve_host_docker_internal_url(remote_browser_url: str) -> str | None:
    """Resolve host.docker.internal to IPv4 to avoid Chrome DevTools Host-header issues."""
    parsed = urlparse(remote_browser_url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname != "host.docker.internal":
        return None

    try:
        address_info = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or 9222,
            family=socket.AF_INET,
        )
    except socket.gaierror:
        return None

    if not address_info:
        return None

    resolved_host = str(address_info[0][4][0])
    if not resolved_host:
        return None

    netloc = resolved_host
    if parsed.port:
        netloc = f"{resolved_host}:{parsed.port}"

    return parsed._replace(netloc=netloc).geturl()


def build_cdp_connection_candidates(
    remote_browser_url: str,
    headers: dict[str, str] | None = None,
) -> Iterable[CdpConnectionCandidate]:
    """Yield fallback CDP endpoints after the primary connect attempt fails."""
    resolved_url = resolve_host_docker_internal_url(remote_browser_url)
    if resolved_url:
        yield CdpConnectionCandidate(
            url=resolved_url,
            label="resolved host.docker.internal IPv4",
            headers=headers,
        )


def build_cdp_configuration_error(
    remote_browser_url: str,
    error: Exception,
) -> CdpConnectionConfigurationError | None:
    discovery_error = parse_cdp_discovery_error(error)
    if discovery_error is None:
        return None

    status_code, discovery_url = discovery_error
    parsed = urlparse(remote_browser_url)
    if parsed.scheme not in {"http", "https"}:
        return None

    guidance = (
        f"Skyvern reached the configured CDP address ({remote_browser_url}), but "
        f"{discovery_url} returned HTTP {status_code}. Skyvern cdp-connect requires "
        "Chrome's classic DevTools Protocol endpoint, where /json/version returns JSON "
        "with webSocketDebuggerUrl. If you enabled chrome://inspect/#remote-debugging, "
        "set BROWSER_REMOTE_DEBUGGING_URL to the direct full "
        "ws://.../devtools/browser/... URL from Chrome's DevToolsActivePort file. "
        "On Windows Docker Desktop, run scripts/windows_chrome_inspect_cdp.ps1 to "
        "bridge Chrome's loopback-only listener before connecting."
    )

    if parsed.hostname == "host.docker.internal":
        guidance += (
            " In Docker Desktop, Chrome can also reject the host.docker.internal "
            "Host header; use the Docker host gateway IPv4 address if the classic "
            "CDP endpoint returns HTTP 500."
        )

    return CdpConnectionConfigurationError(guidance)


async def connect_over_cdp_with_diagnostics(
    playwright: Playwright,
    remote_browser_url: str,
    headers: dict[str, str] | None = None,
    timeout_ms: int = DEFAULT_CDP_CONNECT_TIMEOUT_MS,
) -> Browser:
    try:
        return await playwright.chromium.connect_over_cdp(
            remote_browser_url,
            timeout=timeout_ms,
            headers=headers,
        )
    except Exception as first_error:
        errors: list[tuple[str, Exception]] = [(remote_browser_url, first_error)]
        for candidate in build_cdp_connection_candidates(remote_browser_url, headers):
            message = (
                "Retrying CDP connection with resolved host.docker.internal IPv4"
                if candidate.label == "resolved host.docker.internal IPv4"
                else "Retrying CDP connection"
            )
            LOG.warning(
                message,
                reason=candidate.label,
                remote_browser_url=remote_browser_url,
                fallback_url=candidate.url,
            )
            try:
                return await playwright.chromium.connect_over_cdp(
                    candidate.url,
                    timeout=timeout_ms,
                    headers=candidate.headers,
                )
            except Exception as candidate_error:
                errors.append((candidate.url, candidate_error))

        for url, error in reversed(errors):
            configuration_error = build_cdp_configuration_error(url, error)
            if configuration_error:
                raise configuration_error from error

        last_url, last_error = errors[-1]
        if last_url == remote_browser_url:
            raise last_error
        raise last_error from first_error
