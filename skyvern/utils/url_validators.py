import ipaddress
import socket
from http import HTTPStatus
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

from pydantic import HttpUrl, ValidationError

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, InvalidUrl, SkyvernHTTPException

SAFE_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_SAFE_REDIRECTS = 10

_BLOCKED_INTERNAL_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "kubernetes.default.svc"})
_BLOCKED_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".cluster.local")
_BLOCKED_IP_NETWORKS = tuple(
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
_BLOCKED_METADATA_IPS = frozenset(
    ipaddress.ip_address(ip) for ip in ("169.254.169.254", "100.100.100.200", "fd00:ec2::254")
)


def strip_query_params(url: str) -> str:
    """Return scheme://host/path with query string, fragment, and userinfo removed.

    Used for span attributes where we want page identity without leaking PII.
    Strips: query params, fragments, and userinfo (user:password@) from netloc.
    Returns empty string for empty or unparseable input.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    port_str = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port_str}{parsed.path}"


def collapse_duplicate_www_prefix(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.netloc:
        return url

    userinfo, separator, host_port = parts.netloc.rpartition("@")
    if not host_port.lower().startswith("www.www."):
        return url

    host_port = host_port[4:]
    netloc = f"{userinfo}{separator}{host_port}" if separator else host_port
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def prepend_scheme_and_validate_url(url: str) -> str:
    if not url:
        return url

    parsed_url = urlparse(url=url)
    if parsed_url.scheme and parsed_url.scheme not in ["http", "https"]:
        raise InvalidUrl(url=url)

    # if url doesn't contain any scheme, we prepend `https` to it by default
    if not parsed_url.scheme:
        url = f"https://{url}"

    url = collapse_duplicate_www_prefix(url)

    try:
        HttpUrl(url)
    except ValidationError:
        raise InvalidUrl(url=url)

    return url


def _normalize_host(host: str) -> str:
    # RFC 3986 wraps IPv6 literals in [...]; ip_address() only accepts the bare form.
    return (host[1:-1] if host.startswith("[") and host.endswith("]") else host).strip().lower().rstrip(".")


def _normalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = _normalize_ip(ip)
    if ip in _BLOCKED_METADATA_IPS:
        return True
    if any(ip.version == network.version and ip in network for network in _BLOCKED_IP_NETWORKS):
        return True
    return bool(
        ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _is_allowed_host(host: str) -> bool:
    normalized = _normalize_host(host)
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = _normalize_ip(ipaddress.ip_address(normalized))
    except ValueError:
        ip = None
    except Exception:
        return False

    candidate_forms = {host.lower(), normalized}
    if ip is not None:
        candidate_forms.add(str(ip).lower())

    allowed = {h.lower() for h in settings.ALLOWED_HOSTS}
    return bool(candidate_forms & allowed)


def _is_internal_hostname(host: str) -> bool:
    normalized = _normalize_host(host)
    if normalized in _BLOCKED_INTERNAL_HOSTNAMES:
        return True
    if normalized.endswith(_BLOCKED_INTERNAL_SUFFIXES):
        return True
    return normalized.endswith(".svc")


def is_blocked_host(host: str, *, resolve_dns: bool = False) -> bool:
    normalized = _normalize_host(host)
    if not normalized:
        return True

    if _is_allowed_host(host):
        return False

    blocked = {b.lower().rstrip(".") for b in settings.BLOCKED_HOSTS}
    if normalized in blocked or _is_internal_hostname(normalized):
        return True

    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        ip = None
    except Exception:
        return True

    if ip is not None:
        return _is_blocked_ip(ip)

    if not resolve_dns:
        return False

    try:
        resolve_fetch_host_ips(normalized)
    except BlockedHost:
        return True
    return False


def resolve_fetch_host_ips(host: str) -> tuple[str, ...]:
    normalized = _normalize_host(host)
    if not normalized:
        raise BlockedHost(host=host)

    allowed = _is_allowed_host(host)
    if not allowed and (normalized in {b.lower().rstrip(".") for b in settings.BLOCKED_HOSTS}):
        raise BlockedHost(host=host)
    if not allowed and _is_internal_hostname(normalized):
        raise BlockedHost(host=host)

    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        ip = None
    except Exception:
        raise BlockedHost(host=host)

    if ip is not None:
        normalized_ip = _normalize_ip(ip)
        if not allowed and _is_blocked_ip(normalized_ip):
            raise BlockedHost(host=host)
        return (str(normalized_ip),)

    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise BlockedHost(host=host)

    resolved_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else None
        if not ip_str:
            continue
        try:
            resolved_ip = _normalize_ip(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
        if not allowed and _is_blocked_ip(resolved_ip):
            raise BlockedHost(host=host)
        resolved_ip_str = str(resolved_ip)
        if resolved_ip_str not in resolved_ips:
            resolved_ips.append(resolved_ip_str)

    if not resolved_ips:
        raise BlockedHost(host=host)
    return tuple(resolved_ips)


def validate_url(url: str) -> str | None:
    try:
        url = prepend_scheme_and_validate_url(url=url)
        v = HttpUrl(url=url)
    except Exception as e:
        raise SkyvernHTTPException(message=str(e), status_code=HTTPStatus.BAD_REQUEST)

    if not v.host:
        return None
    host = v.host
    blocked = is_blocked_host(host, resolve_dns=False)
    if blocked:
        raise BlockedHost(host=host)
    return str(v)


def validate_fetch_url_with_resolved_ips(url: str) -> tuple[str, tuple[str, ...]]:
    try:
        url = prepend_scheme_and_validate_url(url=url)
        v = HttpUrl(url=url)
    except Exception as e:
        raise SkyvernHTTPException(message=str(e), status_code=HTTPStatus.BAD_REQUEST)

    if not v.host:
        raise InvalidUrl(url=url)
    return str(v), resolve_fetch_host_ips(v.host)


def validate_fetch_url(url: str) -> str:
    return validate_fetch_url_with_resolved_ips(url)[0]


def validate_redirect_url_with_resolved_ips(url: str, location: str) -> tuple[str, tuple[str, ...]]:
    return validate_fetch_url_with_resolved_ips(urljoin(url, location))


def validate_redirect_url(url: str, location: str) -> str:
    return validate_redirect_url_with_resolved_ips(url, location)[0]


def encode_url(url: str) -> str:
    parts = list(urlsplit(url))
    # Encode the path while preserving "/" and "%"
    parts[2] = quote(parts[2], safe="/%")
    parts[3] = quote(parts[3], safe="=&/%")
    return urlunsplit(parts)
