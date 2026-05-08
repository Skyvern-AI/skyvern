from __future__ import annotations

import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, Playwright

from skyvern.exceptions import CdpConnectionConfigurationError

LOG = structlog.get_logger()

_CDP_DISCOVERY_ERROR_RE = re.compile(
    r"Unexpected status (?P<status>\d+) when connecting to (?P<url>https?://\S+/json/version/?)"
)


@dataclass(frozen=True)
class CdpConnectionCandidate:
    url: str
    label: str
    headers: dict[str, str] | None = None


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


def build_chrome_inspect_ws_url(remote_browser_url: str) -> str | None:
    """Return the chrome://inspect WebSocket endpoint candidate for a CDP HTTP URL."""
    parsed = urlparse(remote_browser_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return parsed._replace(scheme=ws_scheme, path="/devtools/browser", params="", query="", fragment="").geturl()


def _host_header_for_chrome_loopback(remote_browser_url: str) -> dict[str, str] | None:
    """Build a loopback Host header for Chrome's DevTools host-header allowlist."""
    parsed = urlparse(remote_browser_url)
    if parsed.hostname != "host.docker.internal":
        return None

    port = parsed.port or 9222
    return {"Host": f"127.0.0.1:{port}"}


def _merge_headers(
    base_headers: dict[str, str] | None,
    candidate_headers: dict[str, str] | None,
) -> dict[str, str] | None:
    if not base_headers and not candidate_headers:
        return None

    headers = dict(base_headers or {})
    headers.update(candidate_headers or {})
    return headers


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

    ws_url = build_chrome_inspect_ws_url(remote_browser_url)
    if ws_url:
        host_header = _host_header_for_chrome_loopback(ws_url)
        if host_header:
            yield CdpConnectionCandidate(
                url=ws_url,
                label="chrome://inspect WebSocket endpoint with loopback Host header",
                headers=_merge_headers(headers, host_header),
            )

        yield CdpConnectionCandidate(
            url=ws_url,
            label="chrome://inspect WebSocket endpoint",
            headers=headers,
        )

        resolved_ws_url = resolve_host_docker_internal_url(ws_url)
        if resolved_ws_url:
            yield CdpConnectionCandidate(
                url=resolved_ws_url,
                label="resolved chrome://inspect WebSocket endpoint",
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
        "that MCP-style remote debugging server is not compatible with cdp-connect. "
        "Start Chrome with --remote-debugging-port=9222 and a non-default "
        "--user-data-dir, or set BROWSER_REMOTE_DEBUGGING_URL to the direct "
        "ws://.../devtools/browser/... URL from /json/version."
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
) -> Browser:
    async def connect(url: str, attempt_headers: dict[str, str] | None = None) -> Browser:
        if attempt_headers is None:
            return await playwright.chromium.connect_over_cdp(url)
        return await playwright.chromium.connect_over_cdp(url, headers=attempt_headers)

    try:
        return await connect(remote_browser_url, headers)
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
                return await connect(candidate.url, candidate.headers)
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
