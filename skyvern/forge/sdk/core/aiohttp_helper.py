import asyncio
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

import aiofiles
import aiohttp
import structlog
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from skyvern.exceptions import HttpException, InvalidUrl
from skyvern.utils.url_validators import (
    MAX_SAFE_REDIRECTS,
    SAFE_REDIRECT_STATUS_CODES,
    resolve_fetch_host_ips,
    validate_fetch_url_with_resolved_ips,
    validate_redirect_url_with_resolved_ips,
)

LOG = structlog.get_logger()
DEFAULT_REQUEST_TIMEOUT = 30
_REDIRECT_CREDENTIAL_HEADERS = {"authorization", "cookie"}


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").lower().rstrip("."), port


def strip_cross_origin_redirect_credentials(
    headers: dict[str, str],
    cookies: dict[str, str] | None,
    current_url: str,
    next_url: str,
) -> tuple[dict[str, str], dict[str, str] | None]:
    if _url_origin(current_url) == _url_origin(next_url):
        return headers, cookies
    return {key: value for key, value in headers.items() if key.lower() not in _REDIRECT_CREDENTIAL_HEADERS}, None


class SSRFGuardedResolver(AbstractResolver):
    def __init__(self) -> None:
        self._pinned_host_ips: dict[str, tuple[str, ...]] = {}
        self._trusted_proxy_hosts: set[str] = set()
        self._default_resolver = DefaultResolver()

    @staticmethod
    def _host_key(host: str) -> str:
        return host.strip().lower().rstrip(".")

    def pin_host_ips(self, host: str, ips: tuple[str, ...]) -> None:
        if not ips:
            raise OSError(f"No safe addresses resolved for host: {host}")
        self._pinned_host_ips[self._host_key(host)] = ips

    def pin_url_ips(self, url: str, ips: tuple[str, ...]) -> None:
        host = urlparse(url).hostname
        if not host:
            raise InvalidUrl(url=url)
        self.pin_host_ips(host, ips)

    def trust_proxy_url(self, proxy: str) -> None:
        host = urlparse(proxy).hostname
        if host:
            self._trusted_proxy_hosts.add(self._host_key(host))

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        results: list[ResolveResult] = []
        host_key = self._host_key(host)
        if host_key in self._trusted_proxy_hosts:
            return await self._default_resolver.resolve(host, port, family)

        ips = self._pinned_host_ips.get(host_key)
        resolved_ips = await asyncio.to_thread(resolve_fetch_host_ips, host) if ips is None else ips
        for ip in resolved_ips:
            ip_family = (
                socket.AF_INET6 if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address) else socket.AF_INET
            )
            if family not in (socket.AF_UNSPEC, ip_family):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": ip_family,
                    "proto": 0,
                    "flags": 0,
                }
            )
        if not results:
            raise OSError(f"No safe addresses resolved for host: {host}")
        return results

    async def close(self) -> None:
        await self._default_resolver.close()


async def validate_and_pin_fetch_url(url: str, resolver: SSRFGuardedResolver) -> str:
    validated_url, ips = await asyncio.to_thread(validate_fetch_url_with_resolved_ips, url)
    resolver.pin_url_ips(validated_url, ips)
    return validated_url


async def validate_and_pin_redirect_url(url: str, location: str, resolver: SSRFGuardedResolver) -> str:
    validated_url, ips = await asyncio.to_thread(validate_redirect_url_with_resolved_ips, url, location)
    resolver.pin_url_ips(validated_url, ips)
    return validated_url


def ssrf_guarded_tcp_connector(resolver: SSRFGuardedResolver | None = None) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(resolver=resolver or SSRFGuardedResolver(), use_dns_cache=False)


async def aiohttp_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    *,
    data: Any | None = None,
    json_data: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    follow_redirects: bool = True,
    proxy: str | None = None,
) -> tuple[int, dict[str, str], Any]:
    """
    Generic HTTP request function that supports all HTTP methods.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        headers: Request headers
        data: Request body data (dict for form data, or other types)
        json_data: JSON data to send (takes precedence over data)
        files: Dictionary mapping field names to file paths for multipart file uploads
        cookies: Request cookies
        timeout: Request timeout in seconds
        follow_redirects: Whether to follow redirects
        proxy: Proxy URL

    Returns:
        Tuple of (status_code, response_headers, response_body)
        where response_body can be dict (for JSON) or str (for text)
    """
    resolver = SSRFGuardedResolver()
    if proxy:
        resolver.trust_proxy_url(proxy)
    current_url = await validate_and_pin_fetch_url(url, resolver)
    request_method = method.upper()
    request_headers = dict(headers or {})
    request_cookies = cookies
    strip_body_headers = False

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), connector=ssrf_guarded_tcp_connector(resolver)
    ) as session:

        async def build_request_kwargs() -> dict[str, Any]:
            headers_dict = dict(request_headers)
            if strip_body_headers:
                headers_dict.pop("Content-Length", None)
                headers_dict.pop("content-length", None)
            request_kwargs: dict[str, Any] = {
                "headers": headers_dict,
                "cookies": request_cookies,
                "proxy": proxy,
                "allow_redirects": False,
            }

            if request_method == "GET":
                return request_kwargs

            if files:
                form = aiohttp.FormData()
                for field_name, file_path in files.items():
                    if not os.path.exists(file_path):
                        raise FileNotFoundError(f"File not found: {file_path}")

                    filename = os.path.basename(file_path)
                    async with aiofiles.open(file_path, "rb") as f:
                        form.add_field(field_name, await f.read(), filename=filename)

                if data is not None and isinstance(data, dict):
                    for key, value in data.items():
                        form.add_field(key, str(value))

                request_kwargs["data"] = form
                headers_dict.pop("Content-Type", None)
                headers_dict.pop("content-type", None)
            elif json_data is not None:
                request_kwargs["json"] = json_data
            elif data is not None:
                content_type = headers_dict.get("Content-Type") or headers_dict.get("content-type") or ""
                request_kwargs["json" if "application/json" in content_type.lower() else "data"] = data
            return request_kwargs

        for _ in range(MAX_SAFE_REDIRECTS + 1):
            request_kwargs = await build_request_kwargs()
            request_kwargs["url"] = current_url
            async with session.request(request_method, **request_kwargs) as response:
                if (
                    follow_redirects
                    and response.status in SAFE_REDIRECT_STATUS_CODES
                    and response.headers.get("Location")
                ):
                    next_url = await validate_and_pin_redirect_url(current_url, response.headers["Location"], resolver)
                    request_headers, request_cookies = strip_cross_origin_redirect_credentials(
                        request_headers, request_cookies, current_url, next_url
                    )
                    current_url = next_url
                    if response.status == 303 or (response.status in (301, 302) and request_method == "POST"):
                        request_method = "GET"
                        strip_body_headers = True
                    continue

                response_headers = dict(response.headers)

                try:
                    response_body = await response.json()
                except (aiohttp.ContentTypeError, Exception):
                    response_body = await response.text()

                return response.status, response_headers, response_body
        raise HttpException(400, current_url, "Too many redirects while making HTTP request")


async def aiohttp_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    retry: int = 0,
    proxy: str | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    raise_exception: bool = True,
    retry_timeout: float = 0,
) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        count = 0
        while count <= retry:
            try:
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    proxy=proxy,
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    if raise_exception:
                        raise HttpException(response.status, url)
                    LOG.error(f"Failed to fetch data from {url}", status_code=response.status)
                    return {}
            except Exception:
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed to fetch data from {url}")


async def aiohttp_get_text(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    retry: int = 0,
    proxy: str | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    raise_exception: bool = True,
    retry_timeout: float = 0,
) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        count = 0
        while count <= retry:
            try:
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    proxy=proxy,
                ) as response:
                    if response.status == 200:
                        return await response.text()
                    if raise_exception:
                        raise HttpException(response.status, url)
                    LOG.error(f"Failed to fetch data from {url}", status_code=response.status)
                    return ""
            except Exception:
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed to fetch data from {url}")


async def aiohttp_post(
    url: str,
    data: dict[str, Any] | None = None,
    str_data: str | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    retry: int = 0,
    proxy: str | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    raise_exception: bool = True,
    retry_timeout: float = 0,
) -> dict[str, Any] | None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        count = 0
        while count <= retry:
            try:
                async with session.post(
                    url,
                    # TODO: make sure to test this out
                    data=str_data,
                    json=data,
                    headers=headers,
                    cookies=cookies,
                    proxy=proxy,
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    if raise_exception:
                        raise HttpException(response.status, url)
                    response_text = await response.text()
                    LOG.error(
                        "Non 200 async post response",
                        url=url,
                        status_code=response.status,
                        method="POST",
                        response=response_text,
                    )
                    return {}
            except Exception:
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed post request url={url}")


async def aiohttp_delete(
    url: str,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    retry: int = 0,
    proxy: str | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    raise_exception: bool = True,
    retry_timeout: float = 0,
) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        count = 0
        while count <= retry:
            try:
                async with session.delete(
                    url,
                    headers=headers,
                    cookies=cookies,
                    proxy=proxy,
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    if raise_exception:
                        raise HttpException(response.status, url)
                    LOG.error(f"Failed to delete data from {url}", status_code=response.status)
                    return {}
            except Exception:
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed to delete data from {url}")
