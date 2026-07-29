"""The webhook test/replay endpoints fetch a caller-supplied URL server-side, so they must
validate with DNS resolution. `validate_url` skips DNS, which lets a public hostname that
resolves to a private/link-local address (wildcard resolvers such as `<ip>.nip.io`) through.
Validation alone is not enough either: the connection has to be pinned to the address that
was validated, or a rebinding host answers again with a private address at connect time.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

from skyvern.exceptions import BlockedHost, SkyvernHTTPException
from skyvern.forge.sdk.routes import webhooks as webhook_routes
from skyvern.schemas.webhooks import TestWebhookRequest as WebhookTestPayload
from skyvern.services import webhook_service
from skyvern.utils.url_validators import pinned_ip_client, resolve_fetch_host_ips

PRIVATE_HOST_URL = "http://169.254.169.254.example.test/computeMetadata/v1/"
REBINDING_HOST_URL = "https://rebinding.example.test/webhook"
PUBLIC_IP = "93.184.216.34"
METADATA_IP = "169.254.169.254"


@pytest.fixture
def resolves_to_metadata_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", _resolve)


@pytest.mark.asyncio
async def test_test_webhook_blocks_hostname_resolving_to_private_ip(
    resolves_to_metadata_ip: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_requests(*args: object, **kwargs: object) -> None:
        raise AssertionError("test_webhook issued an HTTP request to a blocked host")

    monkeypatch.setattr(webhook_routes.httpx, "AsyncClient", _no_requests)

    response = await webhook_routes.test_webhook(
        request=WebhookTestPayload(webhook_url=PRIVATE_HOST_URL, run_type="task"),
        current_org=MagicMock(organization_id="o_1"),
    )

    assert response.status_code is None
    assert "SSRF protection" in (response.error or "")


@pytest.mark.asyncio
async def test_replay_target_url_blocks_hostname_resolving_to_private_ip(resolves_to_metadata_ip: None) -> None:
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await webhook_service._validate_target_url(PRIVATE_HOST_URL)

    assert not isinstance(exc_info.value, BlockedHost)
    assert "SSRF protection" in str(exc_info.value)


@pytest.fixture
def capture_connect_target(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Intercept the request where httpx would open the socket, recording the connect target."""
    captured: dict[str, object] = {}

    async def _capture(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        captured["connect_host"] = request.url.host
        captured["sni_hostname"] = request.extensions.get("sni_hostname")
        captured["host_header"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _capture)
    return captured


@pytest.fixture
def rebinding_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer with a public address once, then with the metadata address forever after."""
    answers = iter([PUBLIC_IP])

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        ip = next(answers, METADATA_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", _resolve)


@pytest.mark.asyncio
async def test_pinned_client_keeps_sni_and_host_on_the_original_hostname(
    capture_connect_target: dict[str, object],
) -> None:
    async with pinned_ip_client((PUBLIC_IP,)) as client:
        await client.post(REBINDING_HOST_URL, content=b"{}")

    assert capture_connect_target["connect_host"] == PUBLIC_IP
    assert capture_connect_target["sni_hostname"] == "rebinding.example.test"
    assert capture_connect_target["host_header"] == "rebinding.example.test"


@pytest.mark.asyncio
async def test_test_webhook_connects_to_validated_ip_after_dns_rebind(
    rebinding_dns: None,
    capture_connect_target: dict[str, object],
) -> None:
    with patch("skyvern.forge.sdk.routes.webhooks.app.DATABASE.organizations.get_valid_org_auth_token") as get_token:
        get_token.return_value = None
        response = await webhook_routes.test_webhook(
            request=WebhookTestPayload(webhook_url=REBINDING_HOST_URL, run_type="task"),
            current_org=MagicMock(organization_id="o_1"),
        )

    assert response.status_code == 200
    assert capture_connect_target["connect_host"] == PUBLIC_IP

    # The host has since rebound to the metadata address, so an unpinned connect would land there.
    with pytest.raises(BlockedHost):
        resolve_fetch_host_ips("rebinding.example.test")


@pytest.mark.asyncio
async def test_replay_delivery_pins_the_validated_ips(rebinding_dns: None) -> None:
    validated_url, resolved_ips = await webhook_service._validate_target_url(REBINDING_HOST_URL)
    assert resolved_ips == (PUBLIC_IP,)

    delivered: dict[str, object] = {}

    async def _deliver(**kwargs: object) -> httpx.Response:
        delivered.update(kwargs)
        return httpx.Response(200, text="ok")

    with patch("skyvern.services.webhook_service.app.AGENT_FUNCTION.deliver_webhook", _deliver):
        await webhook_service._deliver_webhook(
            url=validated_url,
            payload="{}",
            headers={},
            resolved_ips=resolved_ips,
        )

    assert delivered["resolved_ips"] == (PUBLIC_IP,)
