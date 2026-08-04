from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import aiohttp

from skyvern.exceptions import BlockedHost, InvalidUrl

_HTTP_SCHEMES = {"http", "https"}
_MAX_URL_LENGTH = 2083


def _normalize_host(host: str) -> str:
    # urlparse().hostname strips brackets for normal URLs, but resolver hooks and
    # tests may pass RFC 3986 IPv6 literals through in bracketed form.
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def validate_public_ip_address(ip_address: str, host: str) -> None:
    """Reject an IP address unless it is globally routable.

    Args:
        ip_address: Candidate IPv4 or IPv6 address.
        host: Original hostname used in the rejection error.
    """
    try:
        ip = _public_ip(ipaddress.ip_address(_normalize_host(ip_address)))
    except ValueError:
        return

    if not ip.is_global:
        raise BlockedHost(host=f"{host} resolved to {ip}")


def _raise_if_numeric_host_is_blocked(host: str) -> None:
    normalized_host = _normalize_host(host)
    try:
        resolved_addresses = socket.getaddrinfo(
            normalized_host,
            None,
            0,
            0,
            0,
            socket.AI_NUMERICHOST,
        )
    except socket.gaierror:
        return

    checked: set[str] = set()
    for resolved_address in resolved_addresses:
        sockaddr = resolved_address[4]
        if not sockaddr:
            continue
        ip_address = str(sockaddr[0])
        if ip_address in checked:
            continue
        checked.add(ip_address)
        validate_public_ip_address(ip_address, host)


def validate_public_http_url(url: str) -> None:
    if len(url) > _MAX_URL_LENGTH:
        raise InvalidUrl(url=url)

    parsed = urlparse(url)
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not parsed.hostname:
        raise InvalidUrl(url=url)

    _raise_if_numeric_host_is_blocked(parsed.hostname)


def create_public_network_trace_config() -> aiohttp.TraceConfig:
    trace_config = aiohttp.TraceConfig()

    async def on_request_start(
        _session: aiohttp.ClientSession,
        _trace_config_ctx: object,
        params: aiohttp.TraceRequestStartParams,
    ) -> None:
        validate_public_http_url(str(params.url))

    async def on_request_redirect(
        _session: aiohttp.ClientSession,
        _trace_config_ctx: object,
        params: aiohttp.TraceRequestRedirectParams,
    ) -> None:
        location = params.response.headers.get("Location")
        if not location:
            return
        validate_public_http_url(urljoin(str(params.url), location))

    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_redirect.append(on_request_redirect)
    return trace_config
