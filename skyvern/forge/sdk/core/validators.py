import ipaddress
from urllib.parse import urlparse

from fastapi import status
from pydantic import HttpUrl, ValidationError

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, InvalidUrl, SkyvernHTTPException


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
    if host.lower() in (h.lower() for h in settings.ALLOWED_HOSTS):
        return False
    try:
        ip = ipaddress.ip_address(host)
        # Check if the IP is private, link-local, loopback, or reserved
        return ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved
    except ValueError:
        # If the host is not a valid IP address (e.g., it's a domain name like localhost), handle it here
        for blocked_host in settings.BLOCKED_HOSTS:
            if blocked_host == host:
                return True
        return False
    except Exception:
        return False


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
