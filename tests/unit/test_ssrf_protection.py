from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.exceptions import BlockedHost, InvalidUrl
from skyvern.forge.sdk.core import aiohttp_helper
from skyvern.forge.sdk.core.aiohttp_helper import SSRFGuardedResolver
from skyvern.forge.sdk.core.ssrf import (
    create_public_network_trace_config,
    validate_public_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:45427/private",
        "http://169.254.169.254/private",
        "http://10.0.0.10/internal",
        "http://100.64.0.1/internal",
        "http://[fc00::1]/internal",
    ],
)
def test_validate_public_http_url_blocks_internal_literals(url: str) -> None:
    with pytest.raises(BlockedHost):
        validate_public_http_url(url)


def test_validate_public_http_url_allows_public_literal() -> None:
    validate_public_http_url("https://8.8.8.8/dns-query")


def test_validate_public_http_url_rejects_overlong_url() -> None:
    with pytest.raises(InvalidUrl):
        validate_public_http_url(f"https://example.com/{'a' * 2084}")


@pytest.mark.asyncio
async def test_strict_ssrf_guarded_resolver_blocks_any_non_public_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = SSRFGuardedResolver(require_public_network=True)
    monkeypatch.setattr(aiohttp_helper, "resolve_fetch_host_ips", lambda _host: ("93.184.216.34", "10.0.0.5"))

    try:
        with pytest.raises(BlockedHost, match="internal.example.test"):
            await resolver.resolve("internal.example.test", 443)
    finally:
        await resolver.close()


@pytest.mark.asyncio
async def test_strict_ssrf_guarded_resolver_allows_public_dns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = SSRFGuardedResolver(require_public_network=True)
    monkeypatch.setattr(aiohttp_helper, "resolve_fetch_host_ips", lambda _host: ("93.184.216.34",))

    try:
        resolved = await resolver.resolve("example.com", 443)
    finally:
        await resolver.close()

    assert resolved[0]["host"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_public_network_trace_config_blocks_redirect_to_internal_target() -> None:
    trace_config = create_public_network_trace_config()
    redirect_handler = trace_config.on_request_redirect[0]
    params = SimpleNamespace(
        url="https://example.com/start",
        response=SimpleNamespace(headers={"Location": "http://127.0.0.1/admin"}),
    )

    with pytest.raises(BlockedHost, match="127.0.0.1"):
        await redirect_handler(None, None, params)
