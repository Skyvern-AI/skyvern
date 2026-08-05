from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse, urlsplit, urlunparse, urlunsplit

import structlog
from playwright.async_api import Browser, Playwright

from skyvern.config import settings
from skyvern.exceptions import CdpConnectionConfigurationError
from skyvern.utils.url_validators import validate_browser_host
from skyvern.webeye.cdp_credentials import LIVE_VIEW_PATH_SEGMENT, marked_credential_segment

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


def strip_browser_address_discriminator(url: str) -> str:
    """Remove local PBS URL fragments used only for browser_address DB uniqueness."""
    parsed = urlparse(url)
    if parsed.fragment.startswith("pbs_"):
        return urlunparse(parsed._replace(fragment=""))
    return url


REDACTED = "[REDACTED]"


def redact_cdp_url(url: str | None) -> str:
    """A CDP or live-view address with every credential position masked.

    The single place that decides what of an address may be written down. Five positions carry a
    secret, and each is one an edge reads back out:

    - the query (a session token, a vendor api key) and userinfo;
    - the segment after a path credential marker, ``/<marker>/<secret>/<session_id>``, which is
      how a header-less client authenticates to the router;
    - the routing token in ``/{session_id}/{token}/devtools/...`` (legacy CDP);
    - the token trailing ``/vnc/{session_id}`` (legacy live view).

    The markers and the live-view prefix come from ``cdp_credentials``, which owns them; the
    router's ``_parse_request`` restates them (it may not import this package) and a contract
    test pins the two equal, so this cannot fall behind what the router will accept. Scheme,
    host, port, parameter names and the session id survive, so a redacted line still identifies
    the session and the endpoint it was dialing.
    """
    if not url:
        return ""
    try:
        split = urlsplit(url)
    except ValueError:
        return REDACTED

    netloc = split.netloc
    if "@" in netloc:
        netloc = f"{REDACTED}@{netloc.rsplit('@', 1)[1]}"

    query = "&".join(f"{name}={REDACTED}" for name, _ in parse_qsl(split.query, keep_blank_values=True))

    path = split.path
    segments = [segment for segment in path.split("/") if segment]
    credential_indices: set[int] = set()
    marked = marked_credential_segment(segments)
    if marked is not None:
        credential_indices.add(marked)
    if len(segments) >= 4 and segments[2] == "devtools":
        credential_indices.add(1)
    if segments and segments[0].lower() == LIVE_VIEW_PATH_SEGMENT:
        # /vnc/{session_id} carries its token in the query; anything past the session id is the
        # legacy edge's trailing token.
        credential_indices.update(range(2, len(segments)))
    if credential_indices:
        path = "/" + "/".join(
            REDACTED if index in credential_indices else segment for index, segment in enumerate(segments)
        )

    return urlunsplit((split.scheme, netloc, path, query, split.fragment))


_LOCAL_CONTAINER_CDP_PORT = 9222


def parse_local_cdp_host_port_env() -> int | None:
    raw = os.environ.get("LOCAL_CDP_HOST_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def local_pbs_cdp_host_port(url: str) -> int | None:
    """Host-published CDP port when url targets the local PBS openresty proxy."""
    if settings.ENV != "local":
        return None

    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port is None:
        return None

    host_port = parse_local_cdp_host_port_env()
    if host_port is not None:
        if parsed.port in (_LOCAL_CONTAINER_CDP_PORT, host_port):
            return host_port
        return None

    if parsed.port == _LOCAL_CONTAINER_CDP_PORT:
        return None
    return parsed.port


def resolve_local_pbs_cdp_url(url: str) -> str:
    """Strip DB-only fragments and rewrite container :9222 to host-published PBS port."""
    url = strip_browser_address_discriminator(url)
    host_port = local_pbs_cdp_host_port(url)
    if host_port is None:
        return url

    parsed = urlparse(url)
    if parsed.port == host_port:
        return url
    if parsed.port != _LOCAL_CONTAINER_CDP_PORT:
        return url

    netloc = f"{parsed.hostname}:{host_port}"
    return urlunparse(parsed._replace(netloc=netloc))


def is_local_pbs_cdp_url(url: str) -> bool:
    return local_pbs_cdp_host_port(url) is not None


def is_managed_session_router_cdp_url(url: str, browser_session_id: str | None) -> bool:
    if not browser_session_id:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "wss":
        return False
    segments = [segment for segment in parsed.path.split("/") if segment]
    return (
        len(segments) == 5
        and segments[0] == browser_session_id
        and bool(segments[1])
        and segments[2:4] == ["devtools", "browser"]
        and bool(segments[4])
    )


def prepare_persistent_browser_cdp_connect(
    browser_address: str,
    *,
    browser_session_id: str | None = None,
    x_api_key: str | None = None,
    cdp_connect_headers: dict[str, str] | None = None,
    is_resolved_runner_cdp_proxy: bool = False,
    is_managed_session_router: bool = False,
) -> tuple[str, dict[str, str] | None]:
    """Normalize CDP URL and headers for connections to managed destinations."""
    connect_url = resolve_local_pbs_cdp_url(browser_address)
    headers: dict[str, str] = {}
    if cdp_connect_headers:
        headers.update(cdp_connect_headers)
    is_managed_destination = (
        is_local_pbs_cdp_url(connect_url) or is_resolved_runner_cdp_proxy or is_managed_session_router
    )
    if x_api_key and is_managed_destination:
        headers["x-api-key"] = x_api_key
    if browser_session_id and is_managed_destination:
        headers["X-Session-Id"] = browser_session_id
    return connect_url, headers or None


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
        f"Skyvern reached the configured CDP address ({redact_cdp_url(remote_browser_url)}), but "
        f"{redact_cdp_url(discovery_url)} returned HTTP {status_code}. Skyvern cdp-connect requires "
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
    validate_browser_address: bool = True,
) -> Browser:
    remote_browser_url = strip_browser_address_discriminator(remote_browser_url)
    if validate_browser_address:
        host = urlparse(remote_browser_url).hostname
        if host:
            await asyncio.to_thread(validate_browser_host, host, resolve_dns=True)
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
                remote_browser_url=redact_cdp_url(remote_browser_url),
                fallback_url=redact_cdp_url(candidate.url),
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
