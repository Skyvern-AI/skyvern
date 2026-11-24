import asyncio
import codecs
import ipaddress
import json
import os
import socket
from collections.abc import Callable, Mapping
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlparse

import aiofiles
import aiohttp
import structlog
from aiohttp.abc import AbstractCookieJar, AbstractResolver, ResolveResult
from aiohttp.helpers import parse_mimetype
from aiohttp.resolver import DefaultResolver
from yarl import URL

from skyvern.exceptions import HttpException, InvalidUrl
from skyvern.forge import app
from skyvern.forge.sdk.core.http_request_authorization import GuardedHTTPHopResponse
from skyvern.utils.url_validators import (
    MAX_SAFE_REDIRECTS,
    SAFE_REDIRECT_STATUS_CODES,
    redact_url_for_display,
    resolve_fetch_host_ips,
    validate_fetch_url_with_resolved_ips,
    validate_redirect_url_with_resolved_ips,
)

LOG = structlog.get_logger()
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_GUARDED_HTTP_RESPONSE_LIMIT_BYTES = 100 * 1024 * 1024
OUTBOUND_RESPONSE_SIZE_LIMIT_FLAG = "OUTBOUND_RESPONSE_SIZE_LIMIT"
_SAFE_CROSS_ORIGIN_REDIRECT_HEADERS = frozenset({"accept", "accept-language", "user-agent"})


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
    *,
    cookie_jar: AbstractCookieJar,
) -> tuple[dict[str, str], dict[str, str] | None]:
    if _url_origin(current_url) == _url_origin(next_url):
        return headers, cookies
    cookie_jar.clear()
    return {key: value for key, value in headers.items() if key.lower() in _SAFE_CROSS_ORIGIN_REDIRECT_HEADERS}, None


def prepare_redirect_credentials(
    headers: dict[str, str],
    cookies: dict[str, str] | None,
    current_url: str,
    next_url: str,
    *,
    cookie_jar: AbstractCookieJar,
    set_cookies: tuple[str, ...] = (),
) -> tuple[dict[str, str], dict[str, str] | None]:
    for cookie in set_cookies:
        response_cookies = SimpleCookie()
        response_cookies.load(cookie)
        cookie_jar.update_cookies(response_cookies, response_url=URL(current_url))
    if _url_origin(current_url) == _url_origin(next_url):
        next_cookies = {name: morsel.value for name, morsel in cookie_jar.filter_cookies(URL(next_url)).items()}
        if next_cookies:
            cookie_header = next((name for name in headers if name.lower() == "cookie"), None)
            merged_cookies: dict[str, str] = {}
            if cookie_header is not None:
                existing_cookies = SimpleCookie()
                existing_cookies.load(headers.pop(cookie_header))
                merged_cookies.update({name: morsel.value for name, morsel in existing_cookies.items()})
            merged_cookies.update(cookies or {})
            merged_cookies.update(next_cookies)
            cookies = merged_cookies
    return strip_cross_origin_redirect_credentials(headers, cookies, current_url, next_url, cookie_jar=cookie_jar)


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

    def pinned_ips_for_url(self, url: str) -> tuple[str, ...]:
        host = urlparse(url).hostname
        if not host:
            raise InvalidUrl(url=url)
        try:
            return self._pinned_host_ips[self._host_key(host)]
        except KeyError as exc:
            raise OSError(f"No safe addresses pinned for host: {host}") from exc

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


async def outbound_response_size_limit_enabled(organization_id: str | None) -> bool:
    # Org-scoped so every request from one org resolves the same way; calls without an org share one distinct_id.
    try:
        return (
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                OUTBOUND_RESPONSE_SIZE_LIMIT_FLAG,
                organization_id or OUTBOUND_RESPONSE_SIZE_LIMIT_FLAG,
                properties={"organization_id": organization_id} if organization_id else None,
            )
            is True
        )
    except Exception:
        LOG.warning("Failed to resolve outbound response size limit flag; leaving it off", exc_info=True)
        return False


async def read_response_body_limited(response: aiohttp.ClientResponse, limit_bytes: int, url: str) -> bytes:
    # response.content yields decoded bytes, so a compressed body is counted at its expanded size.
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit_bytes:
            raise HttpException(
                413, redact_url_for_display(url) or "[redacted]", "Response body exceeds its size limit"
            )
    return bytes(body)


def declared_charset(content_type: str) -> str | None:
    charset = parse_mimetype(content_type).parameters.get("charset")
    if not charset:
        return None
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return None


def decode_bounded_response_body(content_type: str, body: bytes) -> Any:
    charset = declared_charset(content_type)
    mimetype = parse_mimetype(content_type)
    if mimetype.type == "application" and (mimetype.subtype == "json" or mimetype.suffix == "json"):
        try:
            # Decode before stripping: byte-level strip corrupts UTF-16/32. Without a charset, json.loads
            # detects the encoding and a BOM from the bytes; with one, utf-8-sig drops a UTF-8 BOM.
            document = body.decode("utf-8-sig" if charset == "utf-8" else charset) if charset else body
            if not document.strip():
                return None
            return json.loads(document)
        except (ValueError, RecursionError):
            pass
    return body.decode(charset or "utf-8", errors="replace")


async def aiohttp_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    *,
    organization_id: str | None = None,
    data: Any | None = None,
    json_data: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    follow_redirects: bool = True,
    proxy: str | None = None,
    authorize_redirect: Callable[[str], bool] | None = None,
    use_controlled_egress: bool = False,
    response_limit_bytes: int = DEFAULT_GUARDED_HTTP_RESPONSE_LIMIT_BYTES,
    read_body: bool = True,
) -> tuple[int, dict[str, str], Any]:
    """
    Generic HTTP request function that supports all HTTP methods.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        headers: Request headers
        organization_id: Organization used to resolve managed egress routing and the size limit flag cohort
        data: Request body data (dict for form data, or other types)
        json_data: JSON data to send (takes precedence over data)
        files: Dictionary mapping field names to file paths for multipart file uploads
        cookies: Request cookies
        timeout: Request timeout in seconds
        follow_redirects: Whether to follow redirects
        proxy: Proxy URL
        authorize_redirect: Refuses a redirect (rather than following it) when it returns False for
            the next URL; the caller decides which destinations the request's contents may reach
        use_controlled_egress: Route each validated hop through the deployment's controlled relay
        response_limit_bytes: Most body bytes accepted; the relay always applies it, the direct path only
            when the size limit flag is on
        read_body: When False, skip reading the body on the direct path and return None for it

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

        def headers_for_hop() -> dict[str, str]:
            headers_dict = dict(request_headers)
            if strip_body_headers:
                headers_dict.pop("Content-Length", None)
                headers_dict.pop("content-length", None)
            if files and request_method != "GET":
                headers_dict.pop("Content-Type", None)
                headers_dict.pop("content-type", None)
            return headers_dict

        async def build_request_kwargs() -> dict[str, Any]:
            headers_dict = headers_for_hop()
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
            elif json_data is not None:
                request_kwargs["json"] = json_data
            elif data is not None:
                content_type = headers_dict.get("Content-Type") or headers_dict.get("content-type") or ""
                request_kwargs["json" if "application/json" in content_type.lower() else "data"] = data
            return request_kwargs

        def response_header(response_headers: Mapping[str, str], name: str) -> str | None:
            normalized = name.lower()
            return next((value for key, value in response_headers.items() if key.lower() == normalized), None)

        async def follow_response_redirect(
            status: int,
            response_headers: Mapping[str, str],
            set_cookies: tuple[str, ...] = (),
        ) -> bool:
            nonlocal current_url, request_cookies, request_headers, request_method, strip_body_headers
            location = response_header(response_headers, "location")
            if not follow_redirects or status not in SAFE_REDIRECT_STATUS_CODES or not location:
                return False
            next_url = await validate_and_pin_redirect_url(current_url, location, resolver)
            if authorize_redirect is not None and not authorize_redirect(next_url):
                raise HttpException(400, current_url, "Redirect blocked by policy")
            redirects_to_get = status == 303 or (status in (301, 302) and request_method == "POST")
            data_is_json = (
                "application/json"
                in (request_headers.get("Content-Type") or request_headers.get("content-type") or "").lower()
            )
            replays_entity = (
                not redirects_to_get
                and request_method != "GET"
                and (json_data is not None or bool(files) or (data is not None and (bool(data) or data_is_json)))
            )
            if _url_origin(current_url) != _url_origin(next_url) and replays_entity:
                raise HttpException(400, "[redacted]", "Cross-origin redirect would replay a request entity")
            request_headers, request_cookies = prepare_redirect_credentials(
                request_headers,
                request_cookies,
                current_url,
                next_url,
                cookie_jar=session.cookie_jar,
                set_cookies=set_cookies,
            )
            current_url = next_url
            if redirects_to_get:
                request_method = "GET"
                strip_body_headers = True
            return True

        def decode_relay_response(response: GuardedHTTPHopResponse) -> Any:
            content_type = response.header("content-type") or ""
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type == "application/json" or media_type.endswith("+json"):
                if not response.body:
                    return None
                try:
                    return json.loads(response.body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            charset = parse_mimetype(content_type).parameters.get("charset", "utf-8")
            try:
                return response.body.decode(charset, errors="replace")
            except LookupError:
                return response.body.decode("utf-8", errors="replace")

        for _ in range(MAX_SAFE_REDIRECTS + 1):
            if use_controlled_egress:
                relay_response = await app.AGENT_FUNCTION.relay_http_request_hop(
                    organization_id=organization_id,
                    method=request_method,
                    target_url=current_url,
                    headers=headers_for_hop(),
                    data=data,
                    json_data=json_data,
                    files=files,
                    cookies=request_cookies,
                    timeout_seconds=timeout,
                    response_limit_bytes=response_limit_bytes,
                    resolved_ips=lambda: resolver.pinned_ips_for_url(current_url),
                )
                if relay_response is not None:
                    if await follow_response_redirect(
                        relay_response.status_code,
                        relay_response.headers,
                        relay_response.set_cookies,
                    ):
                        continue
                    return relay_response.status_code, relay_response.headers, decode_relay_response(relay_response)

            request_kwargs = await build_request_kwargs()
            request_kwargs["url"] = current_url
            async with session.request(request_method, **request_kwargs) as response:
                if await follow_response_redirect(response.status, response.headers):
                    continue

                response_headers = dict(response.headers)

                if not read_body:
                    return response.status, response_headers, None

                if await outbound_response_size_limit_enabled(organization_id):
                    limited_body = await read_response_body_limited(response, response_limit_bytes, current_url)
                    return (
                        response.status,
                        response_headers,
                        decode_bounded_response_body(response.headers.get("Content-Type", ""), limited_body),
                    )

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
        last_error: Exception | None = None
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
            except Exception as e:
                last_error = e
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed to fetch data from {url}") from last_error


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
        last_error: Exception | None = None
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
            except Exception as e:
                last_error = e
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        raise Exception(f"Failed post request url={url}") from last_error


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
        last_exception: Exception | None = None
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
            except Exception as exc:
                last_exception = exc
                if retry_timeout > 0:
                    await asyncio.sleep(retry_timeout)
                count += 1
        if last_exception is not None:
            raise last_exception
        raise Exception(f"Failed to delete data from {url}")
