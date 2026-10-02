"""IP checks and pinning for outbound requests. Imports only stdlib, httpx and structlog, so the NAT proxy image can ship it alone."""

import ipaddress
import urllib.request
from typing import Any

import httpx
import structlog

LOG = structlog.get_logger(__name__)
_env_proxy_warning_logged = False

BLOCKED_IP_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    )
)
BLOCKED_METADATA_IPS = frozenset(
    ipaddress.ip_address(ip) for ip in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254")
)


def normalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = normalize_ip(ip)
    if ip in BLOCKED_METADATA_IPS:
        return True
    if any(ip.version == network.version and ip in network for network in BLOCKED_IP_NETWORKS):
        return True
    return bool(
        ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def environment_proxy_for(url: httpx.URL) -> str | None:
    """The HTTP(S)_PROXY / ALL_PROXY URL that applies to ``url``, or None when unset or bypassed by NO_PROXY."""
    proxies = urllib.request.getproxies()
    proxy = proxies.get(url.scheme) or proxies.get("all")
    if not proxy or urllib.request.proxy_bypass(url.host):
        return None
    return proxy


def _warn_env_proxy_once() -> None:
    # Transports are built per request, so warn once per process; the proxy URL can carry credentials.
    global _env_proxy_warning_logged
    if not _env_proxy_warning_logged:
        _env_proxy_warning_logged = True
        LOG.warning("Outbound requests are routed through an environment proxy without IP pinning")


class PinnedIPTransport(httpx.AsyncHTTPTransport):
    """Connect only to already-validated IPs, keeping SNI, Host, and cert verification on the hostname.

    httpx resolves again at connect time, so a rebinding host can answer with a private
    address after validation passed. Addresses are tried in resolution order so a host
    whose first address is unreachable still behaves like an unpinned client.
    """

    def __init__(self, resolved_ips: tuple[str, ...], *, trust_env: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport_kwargs = kwargs
        self._resolved_ips = resolved_ips
        self._trust_env = trust_env
        self._proxy_transports: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # httpx skips env proxies whenever a transport is passed. A forward proxy resolves the host
        # itself, so pinning cannot apply there; the caller has already validated the URL.
        proxy_url = environment_proxy_for(request.url) if self._trust_env else None
        if proxy_url:
            proxy_transport = self._proxy_transports.get(proxy_url)
            if proxy_transport is None:
                _warn_env_proxy_once()
                proxy_transport = self._proxy_transports[proxy_url] = httpx.AsyncHTTPTransport(
                    proxy=proxy_url, **self._transport_kwargs
                )
            return await proxy_transport.handle_async_request(request)

        original_url = request.url
        request.extensions = {**request.extensions, "sni_hostname": original_url.host}
        last_index = len(self._resolved_ips) - 1
        for index, ip in enumerate(self._resolved_ips):
            request.url = original_url.copy_with(host=ip)
            try:
                return await super().handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == last_index:
                    raise
        raise httpx.ConnectError(f"No validated address for {original_url.host} could be reached")

    async def aclose(self) -> None:
        for proxy_transport in self._proxy_transports.values():
            await proxy_transport.aclose()
        await super().aclose()
