import ipaddress
from urllib.parse import urlparse

from pydantic import HttpUrl, ValidationError, parse_obj_as

from skyvern.config import settings
from skyvern.exceptions import InvalidUrl


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


def validate_url(url: str) -> str:
    try:
        if url:
            # Use parse_obj_as to validate the string as an HttpUrl
            parse_obj_as(HttpUrl, url)
        return url
    except ValidationError:
        # Handle the validation error
        raise InvalidUrl(url=url)


def is_blocked_host(host: str) -> bool:
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
