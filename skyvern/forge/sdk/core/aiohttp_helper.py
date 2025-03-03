import asyncio
from typing import Any

import aiohttp
import structlog

from skyvern.exceptions import HttpException

LOG = structlog.get_logger()
DEFAULT_REQUEST_TIMEOUT = 30


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
                    LOG.error("None 200 async post response", url=url, status_code=response.status, method="POST")
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
