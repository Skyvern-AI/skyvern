"""Canonical browser destination policy shared by broker and launch proxy."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

# Unicode code points UTS-46/IDNA host processing treats as label separators.
# NFKC folds most of these plus fullwidth digits, but U+3002 is left as-is, so we
# map the separators explicitly to match how the browser resolves the host.
_HOST_LABEL_SEPARATORS: dict[int, str] = {0x3002: ".", 0xFF0E: ".", 0xFF61: "."}

NETWORK_SCHEMES: frozenset[str] = frozenset({"http", "https", "ws", "wss"})
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443, "": 80}
_DESTINATION_RESOLVER_WORKERS = 4
_DESTINATION_RESOLVER_EXECUTOR = ThreadPoolExecutor(
    max_workers=_DESTINATION_RESOLVER_WORKERS,
    thread_name_prefix="browser-egress-dns",
)
_DESTINATION_RESOLVER_SLOTS = threading.BoundedSemaphore(_DESTINATION_RESOLVER_WORKERS)

# Bare "metadata" is deliberate: GCE/GKE resolvers route the single-label name to
# the metadata service, so the exact-match block also claims that name externally.
BLOCKED_HOST_NAMES: frozenset[str] = frozenset({"localhost", "metadata", "metadata.google.internal"})
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".internal",
    ".local",
    ".localdomain",
    ".cluster.local",
)

_BROWSER_EGRESS_ENROLLED_ATTR = "_skyvern_browser_egress_enrolled"


class DestinationBlockedError(ValueError):
    """A destination is malformed or violates browser egress policy."""


class DestinationResolutionError(ValueError):
    """A destination cannot be resolved to a usable public peer."""


class ResolvedDestination(NamedTuple):
    host: str
    port: int
    addresses: tuple[str, ...]


def set_browser_egress_enrolled(context: object, *, enrolled: bool) -> None:
    setattr(context, _BROWSER_EGRESS_ENROLLED_ATTR, enrolled is True)


def is_browser_egress_enrolled(context: object) -> bool:
    return getattr(context, _BROWSER_EGRESS_ENROLLED_ATTR, False) is True


def normalize_host(host: str) -> str:
    """Fold a URL host to the form the browser resolves against before connecting.

    Percent-decodes (the WHATWG host parser decodes once, before domain-to-ASCII),
    applies NFKC (fullwidth digits/dots -> ASCII), maps the remaining IDNA label
    separators to '.', and strips the optional fully-qualified trailing dot, so a
    host that the browser would treat as an internal IP literal is classified as
    one instead of slipping through as an opaque hostname.
    """
    return unicodedata.normalize("NFKC", unquote(host)).translate(_HOST_LABEL_SEPARATORS).rstrip(".")


def to_ascii_host(host: str) -> str:
    """IDNA/UTS-46 ToASCII so a Unicode hostname resolves the way the browser does.

    Without this, ``getaddrinfo`` on a raw Unicode IDN host fails and hits the
    resolve fail-open, while the browser IDNA-encodes and can still reach an
    internal A-record. Falls back to the input when encoding is not applicable.
    """
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return host


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its IP addresses (A and AAAA). Monkeypatched in tests."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


_IPV4_RADIX_DIGITS: dict[int, frozenset[str]] = {
    8: frozenset("01234567"),
    10: frozenset("0123456789"),
    16: frozenset("0123456789abcdef"),
}


def _parse_ipv4_number(part: str) -> int | None:
    """One label of a WHATWG IPv4 host: decimal, 0-prefixed octal, or 0x-prefixed hex."""
    if not part:
        return None
    radix = 10
    if part[:2].lower() == "0x":
        radix, part = 16, part[2:]
    elif len(part) >= 2 and part[0] == "0":
        radix, part = 8, part[1:]
    if not part:
        return 0
    if any(char not in _IPV4_RADIX_DIGITS[radix] for char in part.lower()):
        return None
    return int(part, radix)


def _parse_whatwg_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """IPv4 literal forms the browser normalizes but ``ipaddress.ip_address`` rejects.

    The WHATWG URL parser accepts hex (0xa9fea9fe), octal (0251.0376.0251.0376),
    plain-integer (2852039166), and shortened dotted (169.254.43518) hosts as IPv4;
    without this they would fall through to the DNS check and its fail-open.
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        number = _parse_ipv4_number(part)
        if number is None:
            return None
        numbers.append(number)
    if any(number > 255 for number in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number << (8 * (3 - index))
    return ipaddress.IPv4Address(value)


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_WELL_KNOWN_PREFIX:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if not ip.is_global:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def resolve_public_destination(url: str) -> ResolvedDestination | None:
    """Resolve one browser destination and reject every unusable or non-public peer."""
    # urlsplit matches the WHATWG parser's whitespace handling (strips C0/space,
    # drops tab/newline; gh-102153, bpo-43882), so wrapped URLs parse as the
    # browser sees them. WHATWG also folds "\\" to "/" in special-scheme URLs,
    # where urlsplit keeps it in the authority.
    try:
        parts = urlsplit(url.replace("\\", "/"))
    except ValueError as exc:
        raise DestinationBlockedError("blocked egress: malformed URL") from exc

    scheme = parts.scheme.lower()
    if scheme and scheme not in NETWORK_SCHEMES:
        return None

    try:
        raw_host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise DestinationBlockedError("blocked egress: malformed host or port") from exc
    if not raw_host:
        raise DestinationBlockedError("blocked egress: URL has no host")
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    if port is None or not 1 <= port <= 65535:
        raise DestinationBlockedError("blocked egress: URL has invalid port")

    host = normalize_host(raw_host)
    if not host:
        raise DestinationBlockedError("blocked egress: URL has no host")

    literal_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = _parse_whatwg_ipv4(host)
    if literal_ip is not None:
        if not _ip_is_public(literal_ip):
            raise DestinationBlockedError(f"blocked egress to internal address {raw_host}")
        return ResolvedDestination(host, port, (str(literal_ip),))

    normalized_host = to_ascii_host(host).lower()
    if normalized_host in BLOCKED_HOST_NAMES or normalized_host.endswith(BLOCKED_HOST_SUFFIXES):
        raise DestinationBlockedError(f"blocked egress to internal hostname {raw_host}")

    try:
        resolved_addresses = resolve_host(normalized_host)
    except OSError as exc:
        raise DestinationResolutionError("browser destination resolution failed") from exc

    addresses: list[str] = []
    for address in resolved_addresses:
        try:
            resolved_ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not _ip_is_public(resolved_ip):
            raise DestinationBlockedError(f"blocked egress to internal hostname {host} (resolved {address})")
        numeric_address = str(resolved_ip)
        if numeric_address not in addresses:
            addresses.append(numeric_address)
    if not addresses:
        raise DestinationResolutionError("browser destination has no usable public resolution")

    return ResolvedDestination(normalized_host, port, tuple(addresses))


def _resolve_public_destination_with_slot(url: str) -> ResolvedDestination | None:
    try:
        return resolve_public_destination(url)
    finally:
        _DESTINATION_RESOLVER_SLOTS.release()


async def resolve_public_destination_async(url: str, *, timeout_seconds: float) -> ResolvedDestination | None:
    """Resolve a direct-proxy destination off-loop with isolated, bounded worker concurrency."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not _DESTINATION_RESOLVER_SLOTS.acquire(blocking=False):
        if deadline <= loop.time():
            raise DestinationResolutionError("browser destination resolution timed out")
        await asyncio.sleep(min(0.01, deadline - loop.time()))
    future: asyncio.Future[ResolvedDestination | None] | None = None
    try:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        future = loop.run_in_executor(
            _DESTINATION_RESOLVER_EXECUTOR,
            _resolve_public_destination_with_slot,
            url,
        )
        return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
    except TimeoutError as exc:
        raise DestinationResolutionError("browser destination resolution timed out") from exc
    except BaseException:
        if future is None:
            _DESTINATION_RESOLVER_SLOTS.release()
        raise


def classify_url(url: str) -> str | None:
    """Return a block reason for an internal destination, or None if allowed."""
    try:
        resolve_public_destination(url)
    except DestinationBlockedError as exc:
        return str(exc)
    except DestinationResolutionError:
        return None
    return None


async def classify_url_async(url: str) -> str | None:
    """classify_url off the event loop so blocking DNS never stalls the worker."""
    return await asyncio.get_running_loop().run_in_executor(None, classify_url, url)
