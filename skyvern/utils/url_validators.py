import ipaddress
import socket
from http import HTTPStatus
from typing import Annotated, Any
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from pydantic import AfterValidator, AnyHttpUrl, HttpUrl, ValidationError

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, InvalidUrl, SkyvernHTTPException, UnresolvableHost

SAFE_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_SAFE_REDIRECTS = 10

_BLOCKED_INTERNAL_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "kubernetes.default.svc"})
_BLOCKED_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".cluster.local")
_LOCAL_BROWSER_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})
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


def _prepend_scheme(url: str) -> str:
    if not url:
        return url

    try:
        parsed_url = urlparse(url=url)
    except ValueError as error:
        # Malformed authorities (e.g. an unterminated IPv6 literal like ``http://[``) make
        # stdlib urlparse raise a raw ValueError; surface it as the typed InvalidUrl so
        # callers get one contract instead of a leaking parser error.
        raise InvalidUrl(url=url) from error
    if parsed_url.scheme and parsed_url.scheme not in ["http", "https"]:
        raise InvalidUrl(url=url)

    # if url doesn't contain any scheme, we prepend `https` to it by default
    if not parsed_url.scheme:
        url = f"https://{url}"

    return collapse_duplicate_www_prefix(url)


def prepend_scheme_and_validate_url(url: str) -> str:
    url = _prepend_scheme(url)
    if not url:
        return url

    try:
        HttpUrl(url)
    except ValidationError:
        raise InvalidUrl(url=url)

    return url


def canonical_navigation_host(url: str) -> str | None:
    """Host a browser resolves ``url`` against, via pydantic's WHATWG URL model.

    The WHATWG parser (what the browser uses) canonicalizes numeric IPv4 literals
    (decimal/octal/hex/shortened) and backslash authority tricks to the host the
    browser truly connects to, unlike stdlib ``urlparse`` which can diverge. Raises
    ``InvalidUrl`` for non-http(s) schemes and malformed URLs; returns ``None`` when
    there is no host.
    """
    # _prepend_scheme (not prepend_scheme_and_validate_url) plus AnyHttpUrl: both parse with the
    # same WHATWG canonicalization, but HttpUrl's 2083-char ceiling makes a long, ordinary public
    # link fail to parse and so read as a blocked internal host. _prepend_scheme still rejects
    # non-http(s) schemes.
    validated_url = _prepend_scheme(url)
    if not validated_url:
        return None
    try:
        return AnyHttpUrl(validated_url).host
    except ValidationError:
        return None


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


def is_allowed_local_browser_host(host: str) -> bool:
    if settings.ENV != "local":
        return False
    normalized = _normalize_host(host)
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return normalized in _LOCAL_BROWSER_HOSTNAMES


def validate_browser_host(host: str, *, resolve_dns: bool = False) -> None:
    if not is_allowed_local_browser_host(host) and is_blocked_host(host, resolve_dns=resolve_dns):
        raise BlockedHost(host=host)


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
    except UnresolvableHost:
        # UnresolvableHost subclasses BlockedHost, so it must be caught first. The browser resolves
        # through the run proxy and may reach hosts the worker cannot; worker resolution failure is
        # not a policy signal. Literal internal IPs and internal names are refused above, before DNS.
        return False
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
        raise UnresolvableHost(host=host)

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
        raise UnresolvableHost(host=host)
    return tuple(resolved_ips)


def _raise_if_best_effort_fetch_host_is_blocked(url: str) -> None:
    # Browsers treat a backslash in the authority as a separator; urlsplit does not, which would
    # otherwise let "http://<blocked-ip>\.example.com" read as an unrelated host here while the
    # browser still navigates to the blocked one. Only ever used to block, never to permit.
    candidate = url.replace("\\", "/")
    try:
        parsed = urlsplit(candidate)
        # Non-http(s) schemes are already refused by the caller's parse error; resolving their
        # hosts would block the caller's event loop on DNS for a URL that gets refused anyway.
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return
        if not parsed.scheme:
            parsed = urlsplit(f"https://{candidate}")
        host = parsed.hostname
    except (UnicodeError, ValueError):
        return

    if not host:
        return

    # A non-http(s) scheme is refused on scheme alone, so resolving its host decides nothing and
    # would emit a DNS query for an attacker-supplied name on every rejected URL.
    if parsed.scheme not in ("http", "https"):
        return

    try:
        resolve_fetch_host_ips(host)
    except UnresolvableHost:
        return
    except BlockedHost:
        raise
    except Exception:
        return


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


def _is_aws_load_balancer_host(host: str) -> bool:
    labels = host.split(".")
    if host.endswith(".amazonaws.com.cn"):
        service_labels = labels[:-3]
    elif host.endswith(".amazonaws.com"):
        service_labels = labels[:-2]
    else:
        return False

    if len(service_labels) < 3:
        return False

    def is_region(label: str) -> bool:
        prefix, separator, number = label.rpartition("-")
        return bool(separator and number.isdigit() and "-" in prefix)

    return (service_labels[-2] == "elb" and is_region(service_labels[-1])) or (
        service_labels[-1] == "elb" and is_region(service_labels[-2])
    )


def validate_webhook_url(url: str) -> str:
    if not url:
        return url

    validated_url = validate_url(url)
    if not validated_url:
        raise InvalidUrl(url=url)

    host = _normalize_host(urlparse(validated_url).hostname or "")
    if _is_aws_load_balancer_host(host):
        raise SkyvernHTTPException(
            message="Webhook URL must use a stable custom hostname instead of an AWS load balancer DNS name.",
            status_code=HTTPStatus.BAD_REQUEST,
        )
    return validated_url


WebhookUrl = Annotated[str, AfterValidator(validate_webhook_url)]


def validate_fetch_url_with_resolved_ips(url: str) -> tuple[str, tuple[str, ...]]:
    try:
        url = _prepend_scheme(url=url)
        v = AnyHttpUrl(url=url)
    except Exception as e:
        _raise_if_best_effort_fetch_host_is_blocked(url)
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


class _PinnedIPTransport(httpx.AsyncHTTPTransport):
    """Connect only to already-validated IPs, keeping SNI, Host, and cert verification on the hostname.

    httpx resolves again at connect time, so a rebinding host can answer with a private
    address after validation passed. Addresses are tried in resolution order so a host
    whose first address is unreachable still behaves like an unpinned client.
    """

    def __init__(self, resolved_ips: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._resolved_ips = resolved_ips

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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


def pinned_ip_client(resolved_ips: tuple[str, ...] | None, **kwargs: Any) -> httpx.AsyncClient:
    """Client pinned to the IPs a caller already validated, so DNS cannot be re-answered at connect time.

    Pass the IPs from `validate_fetch_url_with_resolved_ips`. Without them this is a plain
    client with no rebinding protection.
    """
    if not resolved_ips:
        return httpx.AsyncClient(**kwargs)
    return httpx.AsyncClient(transport=_PinnedIPTransport(resolved_ips), **kwargs)


def encode_url(url: str) -> str:
    parts = list(urlsplit(url))
    # Encode the path while preserving "/" and "%"
    parts[2] = quote(parts[2], safe="/%")
    parts[3] = quote(parts[3], safe="=&/%")
    return urlunsplit(parts)
