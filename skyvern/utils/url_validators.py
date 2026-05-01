import ipaddress
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from fastapi import status
from pydantic import HttpUrl, ValidationError

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, InvalidUrl, SkyvernHTTPException


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


def prepend_scheme_and_validate_url(url: str) -> str:
    if not url:
        return url

    parsed_url = urlparse(url=url)
    if parsed_url.scheme and parsed_url.scheme not in ["http", "https"]:
        raise InvalidUrl(url=url)

    # if url doesn't contain any scheme, we prepend `https` to it by default
    if not parsed_url.scheme:
        url = f"https://{url}"

    try:
        HttpUrl(url)
    except ValidationError:
        raise InvalidUrl(url=url)

    return url


def is_blocked_host(host: str) -> bool:
    # RFC 3986 wraps IPv6 literals in [...]; ip_address() only accepts the bare form.
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(bare)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
    except ValueError:
        ip = None
    except Exception:
        return False

    candidate_forms = {host.lower(), bare.lower()}
    if ip is not None:
        candidate_forms.add(str(ip).lower())

    allowed = {h.lower() for h in settings.ALLOWED_HOSTS}
    if candidate_forms & allowed:
        return False

    if ip is not None:
        return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved

    blocked = {b.lower() for b in settings.BLOCKED_HOSTS}
    return host.lower() in blocked


def validate_url(url: str) -> str | None:
    try:
        url = prepend_scheme_and_validate_url(url=url)
        v = HttpUrl(url=url)
    except Exception as e:
        raise SkyvernHTTPException(message=str(e), status_code=status.HTTP_400_BAD_REQUEST)

    if not v.host:
        return None
    host = v.host
    blocked = is_blocked_host(host)
    if blocked:
        raise BlockedHost(host=host)
    return str(v)


def encode_url(url: str) -> str:
    parts = list(urlsplit(url))
    # Encode the path while preserving "/" and "%"
    parts[2] = quote(parts[2], safe="/%")
    parts[3] = quote(parts[3], safe="=&/%")
    return urlunsplit(parts)
