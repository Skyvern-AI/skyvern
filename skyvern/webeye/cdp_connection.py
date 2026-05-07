from __future__ import annotations

import re
import socket
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, Playwright

from skyvern.exceptions import CdpConnectionConfigurationError

LOG = structlog.get_logger()

_CDP_DISCOVERY_ERROR_RE = re.compile(
    r"Unexpected status (?P<status>\d+) when connecting to (?P<url>https?://\S+/json/version/?)"
)


def parse_cdp_discovery_error(error: Exception) -> tuple[int, str] | None:
    """Return the HTTP status and discovery URL from a Playwright CDP discovery error."""
    match = _CDP_DISCOVERY_ERROR_RE.search(str(error))
    if not match:
        return None
    return int(match.group("status")), match.group("url")


def resolve_host_docker_internal_url(remote_browser_url: str) -> str | None:
    """Resolve host.docker.internal to IPv4 to avoid Chrome DevTools Host-header issues."""
    parsed = urlparse(remote_browser_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "host.docker.internal":
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
    async def connect(url: str) -> Browser:
        if headers is None:
            return await playwright.chromium.connect_over_cdp(url)
        return await playwright.chromium.connect_over_cdp(url, headers=headers)

    try:
        return await connect(remote_browser_url)
    except Exception as first_error:
        fallback_url = resolve_host_docker_internal_url(remote_browser_url)
        if fallback_url:
            LOG.warning(
                "Retrying CDP connection with resolved host.docker.internal IPv4",
                remote_browser_url=remote_browser_url,
                fallback_url=fallback_url,
            )
            try:
                return await connect(fallback_url)
            except Exception as fallback_error:
                configuration_error = build_cdp_configuration_error(fallback_url, fallback_error)
                if configuration_error:
                    raise configuration_error from fallback_error
                configuration_error = build_cdp_configuration_error(remote_browser_url, first_error)
                if configuration_error:
                    raise configuration_error from first_error
                raise fallback_error from first_error

        configuration_error = build_cdp_configuration_error(remote_browser_url, first_error)
        if configuration_error:
            raise configuration_error from first_error
        raise
