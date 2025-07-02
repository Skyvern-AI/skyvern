import asyncio
from typing import Any

import aiohttp
import structlog

from skyvern.exceptions import HttpException

LOG = structlog.get_logger()
DEFAULT_REQUEST_TIMEOUT = 30


async def aiohttp_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    json_data: dict[str, Any] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
    follow_redirects: bool = True,
    proxy: str | None = None,
) -> tuple[int, dict[str, str], Any]:
    """
    Generic HTTP request function that supports all HTTP methods.

    Returns:
        Tuple of (status_code, response_headers, response_body)
        where response_body can be dict (for JSON) or str (for text)
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        request_kwargs = {
            "url": url,
            "headers": headers or {},
            "cookies": cookies,
            "proxy": proxy,
            "allow_redirects": follow_redirects,
        }

        # Handle body based on content type and method
        if method.upper() != "GET":
            if json_data is not None:
                request_kwargs["json"] = json_data
            elif data is not None:
                request_kwargs["data"] = data

        async with session.request(method.upper(), **request_kwargs) as response:
            response_headers = dict(response.headers)

            # Try to parse response as JSON
            try:
                response_body = await response.json()
            except (aiohttp.ContentTypeError, Exception):
                # If not JSON, get as text
                response_body = await response.text()

            return response.status, response_headers, response_body


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
                async with session.post(
                    url,
                    # TODO: make sure to test this out
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
