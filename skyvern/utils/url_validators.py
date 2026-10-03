import ipaddress
import socket
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Any
from urllib.parse import parse_qsl, quote, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from pydantic import AfterValidator, AnyHttpUrl, HttpUrl, ValidationError, ValidationInfo

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, InvalidUrl, SkyvernHTTPException, UnresolvableHost
from skyvern.utils.pinned_transport import PinnedIPTransport, is_blocked_ip, normalize_ip

SAFE_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
BLOCKED_HOST_ALLOWLIST_HINT = (
    "The host is blocked by SSRF protection. Self-hosted deployments can add the host to ALLOWED_HOSTS."
)
MAX_SAFE_REDIRECTS = 10

# getaddrinfo codes that mean the resolver answered "this name has no address", as opposed to
# EAI_AGAIN/EAI_FAIL, which mean the resolver could not answer at all. EAI_NODATA is absent on
# some platforms and folded into EAI_NONAME on others.
_NO_SUCH_HOST_DNS_ERRNOS = frozenset(
    getattr(socket, name) for name in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, name)
)

_BLOCKED_INTERNAL_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "kubernetes.default.svc"})
_BLOCKED_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".cluster.local")
_LOCAL_BROWSER_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})


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


def redact_url_query(url: str) -> str:
    """Remove the query string while preserving the other URL components."""
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    return urlunsplit(parsed._replace(query=""))


def redact_url_for_display(url: str | None) -> str | None:
    """Remove URL secrets while preserving enough routing context for display."""
    if not url:
        return url

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "[invalid URL]"

    if not parsed.scheme or not hostname:
        return "[invalid URL]"

    display_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        display_host = f"{display_host}:{port}"
    path_marker = parsed.path if parsed.path in {"", "/"} else "/…"
    query_marker = "?…" if "?" in url.partition("#")[0] else ""
    return f"{parsed.scheme}://{display_host}{path_marker}{query_marker}"


def signed_url_ttl_remaining_seconds(url: str, now: datetime) -> float | None:
    try:
        query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
        expires_at: datetime | None = None
        for prefix in ("X-Amz", "X-Goog"):
            date_key = f"{prefix}-Date"
            expires_key = f"{prefix}-Expires"
            if date_key in query and expires_key in query:
                signed_at = datetime.strptime(query[date_key], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                expires_at = signed_at + timedelta(seconds=float(query[expires_key]))
                break
        if expires_at is None:
            if "Expires" not in query or not any(
                signer in query for signer in ("Signature", "AWSAccessKeyId", "Key-Pair-Id")
            ):
                return None
            expires_epoch = float(query["Expires"])
            if expires_epoch <= 1_000_000_000:
                return None
            expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)
        return (expires_at - now).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def redacted_url_origin(url: str) -> str:
    try:
        parsed = urlparse(url)
        if not parsed.scheme or parsed.hostname is None:
            return "<redacted>"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "<redacted>"


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


def _prepend_scheme(url: str, *, field_name: str = "url") -> str:
    if not url:
        return url

    try:
        parsed_url = urlparse(url=url)
    except ValueError:
        # Malformed authorities (e.g. an unterminated IPv6 literal like ``http://[``) make
        # stdlib urlparse raise a raw ValueError; surface it as the typed InvalidUrl so
        # callers get one contract instead of a leaking parser error.
        raise InvalidUrl(url=url, field_name=field_name) from None
    if parsed_url.scheme and parsed_url.scheme not in ["http", "https"]:
        raise InvalidUrl(url=url, field_name=field_name, reason="unsupported scheme")

    # if url doesn't contain any scheme, we prepend `https` to it by default
    if not parsed_url.scheme:
        url = f"https://{url}"

    return collapse_duplicate_www_prefix(url)


def prepend_scheme_and_validate_url(url: str, *, field_name: str = "url") -> str:
    url = _prepend_scheme(url, field_name=field_name)
    if not url:
        return url

    try:
        HttpUrl(url)
    except ValidationError:
        raise InvalidUrl(url=url, field_name=field_name) from None

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
        ip = normalize_ip(ipaddress.ip_address(normalized))
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
        return is_blocked_ip(ip)

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
        normalized_ip = normalize_ip(ip)
        if not allowed and is_blocked_ip(normalized_ip):
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
            resolved_ip = normalize_ip(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
        if not allowed and is_blocked_ip(resolved_ip):
            raise BlockedHost(host=host)
        resolved_ip_str = str(resolved_ip)
        if resolved_ip_str not in resolved_ips:
            resolved_ips.append(resolved_ip_str)

    if not resolved_ips:
        raise UnresolvableHost(host=host)
    return tuple(resolved_ips)


def host_has_no_address_record(host: str) -> bool:
    """True only when the resolver answers definitively that ``host`` has no address.

    Deliberately narrower than ``UnresolvableHost``: a timeout or SERVFAIL means the resolver could
    not answer, and reading that as a dead host would misattribute every navigation failure during
    a resolver outage. Callers use this to attribute a navigation that already failed, never to
    decide whether one is allowed -- ``is_blocked_host`` ignores worker-side resolution failures
    for that reason, since the browser resolves through the run proxy.
    """
    normalized = _normalize_host(host)
    if not normalized:
        return False

    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        # A literal address needs no resolution, so DNS can say nothing about it.
        return False

    try:
        socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        # gaierror subclasses OSError, so it must be caught first.
        return error.errno in _NO_SUCH_HOST_DNS_ERRNOS
    except (OSError, UnicodeError):
        return False
    return False


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


def validate_url(url: str, *, field_name: str = "url") -> str | None:
    try:
        url = prepend_scheme_and_validate_url(url=url, field_name=field_name)
        v = HttpUrl(url=url)
    except InvalidUrl as e:
        raise SkyvernHTTPException(message=str(e), status_code=HTTPStatus.BAD_REQUEST) from None
    except Exception:
        raise SkyvernHTTPException(
            message=f"Invalid {field_name}: malformed.", status_code=HTTPStatus.BAD_REQUEST
        ) from None

    if not v.host:
        return None
    host = v.host
    blocked = is_blocked_host(host, resolve_dns=False)
    if blocked:
        raise BlockedHost(host=host, field_name=field_name)
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


def validate_webhook_url(url: str, info: ValidationInfo | None = None, *, field_name: str = "webhook_url") -> str:
    if not url:
        return url

    field_name = (info.field_name if info is not None else None) or field_name
    validated_url = validate_url(url, field_name=field_name)
    if not validated_url:
        raise InvalidUrl(url=url, field_name=field_name)

    host = _normalize_host(urlparse(validated_url).hostname or "")
    if _is_aws_load_balancer_host(host):
        raise SkyvernHTTPException(
            message=(
                f"Invalid {field_name}: unsupported host. "
                "Use a stable custom hostname instead of an AWS load balancer DNS name."
            ),
            status_code=HTTPStatus.BAD_REQUEST,
        )
    return validated_url


def _validate_webhook_field_url(url: str, info: ValidationInfo) -> str:
    return validate_webhook_url(url, info)


WebhookUrl = Annotated[str, AfterValidator(_validate_webhook_field_url)]


def validate_fetch_url_with_resolved_ips(url: str) -> tuple[str, tuple[str, ...]]:
    try:
        url = _prepend_scheme(url=url)
        v = AnyHttpUrl(url=url)
    except InvalidUrl as e:
        _raise_if_best_effort_fetch_host_is_blocked(url)
        raise SkyvernHTTPException(message=str(e), status_code=HTTPStatus.BAD_REQUEST) from None
    except Exception:
        _raise_if_best_effort_fetch_host_is_blocked(url)
        raise SkyvernHTTPException(message="Invalid url: malformed.", status_code=HTTPStatus.BAD_REQUEST) from None

    if not v.host:
        raise InvalidUrl(url=url)
    return str(v), resolve_fetch_host_ips(v.host)


def validate_fetch_url(url: str) -> str:
    return validate_fetch_url_with_resolved_ips(url)[0]


def validate_redirect_url_with_resolved_ips(url: str, location: str) -> tuple[str, tuple[str, ...]]:
    return validate_fetch_url_with_resolved_ips(urljoin(url, location))


def validate_redirect_url(url: str, location: str) -> str:
    return validate_redirect_url_with_resolved_ips(url, location)[0]


def pinned_ip_client(resolved_ips: tuple[str, ...] | None, **kwargs: Any) -> httpx.AsyncClient:
    """Client pinned to the IPs a caller already validated, so DNS cannot be re-answered at connect time.

    Pass the IPs from `validate_fetch_url_with_resolved_ips`. Without them this is a plain
    client with no rebinding protection. Environment proxies are ignored unless OUTBOUND_TRUST_ENV_PROXY
    is set, because a forward proxy re-resolves the host and the pin no longer applies.
    """
    if not resolved_ips:
        return httpx.AsyncClient(**kwargs)
    transport = PinnedIPTransport(resolved_ips, trust_env=kwargs.get("trust_env", settings.OUTBOUND_TRUST_ENV_PROXY))
    return httpx.AsyncClient(transport=transport, **kwargs)


def encode_url(url: str) -> str:
    parts = list(urlsplit(url))
    # Encode the path while preserving "/" and "%"
    parts[2] = quote(parts[2], safe="/%")
    parts[3] = quote(parts[3], safe="=&/%")
    return urlunsplit(parts)
