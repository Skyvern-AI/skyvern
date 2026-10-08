"""The webhook test/replay endpoints fetch a caller-supplied URL server-side, so they must
validate with DNS resolution. `validate_url` skips DNS, which lets a public hostname that
resolves to a private/link-local address (wildcard resolvers such as `<ip>.nip.io`) through.
Validation alone is not enough either: the connection has to be pinned to the address that
was validated, or a rebinding host answers again with a private address at connect time.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, FailedToGetTOTPVerificationCode, SkyvernHTTPException
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.models import WorkflowRunAttemptModel
from skyvern.forge.sdk.routes import webhooks as webhook_routes
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import compute_attempt_view
from skyvern.schemas.run_enums import WebhookDeliveryStatus
from skyvern.schemas.webhooks import TestWebhookRequest as WebhookTestPayload
from skyvern.services import otp_service, webhook_delivery, webhook_service
from skyvern.services.webhook_delivery import WebhookDeliveryAttempts
from skyvern.utils.url_validators import pinned_ip_client, resolve_fetch_host_ips
from tests.unit.scoped_asyncio import ScopedAsyncio

PRIVATE_HOST_URL = "http://169.254.169.254.example.test/computeMetadata/v1/"
REBINDING_HOST_URL = "https://rebinding.example.test/webhook"
PUBLIC_IP = "93.184.216.34"
METADATA_IP = "169.254.169.254"


pytestmark = pytest.mark.usefixtures("no_env_proxy")


@pytest.mark.asyncio
async def test_replay_of_a_running_run_without_attempt_rows_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    run = WorkflowRun.model_construct(
        workflow_run_id="wr_replay", status=WorkflowRunStatus.running, failure_reason=None, finished_at=None
    )
    monkeypatch.setattr(webhook_service.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(webhook_service.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    build_payload = AsyncMock(
        return_value=webhook_service._WebhookPayload(
            run_id="wr_replay", run_type="workflow_run", payload={}, default_webhook_url=REBINDING_HOST_URL
        )
    )
    deliver = AsyncMock(return_value=(200, 1, "ok", None))
    monkeypatch.setattr(webhook_service, "_build_webhook_payload", build_payload)
    monkeypatch.setattr(webhook_service, "_deliver_webhook", deliver)
    monkeypatch.setattr(
        webhook_service, "_validate_target_url", AsyncMock(return_value=(REBINDING_HOST_URL, (PUBLIC_IP,)))
    )

    await webhook_service.replay_run_webhook("o_replay", "wr_replay", None, api_key="test-key")

    deliver.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_status", "final_decision"),
    [
        (WorkflowRunStatus.queued, "final"),
        (WorkflowRunStatus.running, "revoked"),
        (WorkflowRunStatus.running, "abandoned"),
    ],
)
async def test_replay_waits_for_logical_run_finality(
    monkeypatch: pytest.MonkeyPatch, active_status: WorkflowRunStatus, final_decision: str
) -> None:
    finished_at = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    run = WorkflowRun.model_construct(
        workflow_run_id="wr_replay",
        status=WorkflowRunStatus.failed,
        failure_reason=None,
        started_at=finished_at - timedelta(minutes=1),
        finished_at=finished_at,
    )
    first = WorkflowRunAttemptModel(
        attempt_number=1,
        status="failed",
        retry_decision="retry",
        started_at=run.started_at,
        finished_at=finished_at,
        next_attempt_at=finished_at + timedelta(seconds=1),
    )
    attempts = [first]
    monkeypatch.setattr(webhook_service.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(
        webhook_service.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=attempts)
    )
    build_payload = AsyncMock(
        return_value=webhook_service._WebhookPayload(
            run_id="wr_replay", run_type="workflow_run", payload={}, default_webhook_url=REBINDING_HOST_URL
        )
    )
    deliver = AsyncMock(return_value=(200, 1, "ok", None))
    monkeypatch.setattr(webhook_service, "_build_webhook_payload", build_payload)
    monkeypatch.setattr(webhook_service, "_deliver_webhook", deliver)
    monkeypatch.setattr(
        webhook_service, "_validate_target_url", AsyncMock(return_value=(REBINDING_HOST_URL, (PUBLIC_IP,)))
    )

    async def assert_replay_blocked() -> None:
        with pytest.raises(SkyvernHTTPException, match="unavailable until it is final") as exc_info:
            await webhook_service.replay_run_webhook("o_replay", "wr_replay", None, api_key="test-key")
        assert exc_info.value.status_code == 409
        build_payload.assert_not_awaited()
        deliver.assert_not_awaited()

    pending_view = compute_attempt_view(run, attempts)
    assert pending_view.retry_pending
    assert pending_view.next_attempt_at == first.next_attempt_at
    await assert_replay_blocked()

    first.next_attempt_prepared_at = first.next_attempt_at
    second = WorkflowRunAttemptModel(attempt_number=2, status=active_status.value)
    attempts.append(second)
    run.finished_at = None
    run.started_at = None
    run.status = active_status
    if active_status == WorkflowRunStatus.running:
        run.started_at = second.started_at = first.next_attempt_at
    prepared_view = compute_attempt_view(run, attempts)
    assert not prepared_view.retry_pending
    assert prepared_view.next_attempt_at is None
    await assert_replay_blocked()

    run.status = WorkflowRunStatus.failed
    run.finished_at = second.finished_at = finished_at + timedelta(minutes=1)
    second.status = "failed"
    assert not compute_attempt_view(run, attempts).retry_pending
    await assert_replay_blocked()

    second.retry_decision = final_decision
    response = await webhook_service.replay_run_webhook("o_replay", "wr_replay", None, api_key="test-key")
    assert response.status_code == 200
    deliver.assert_awaited_once()

    attempts.clear()
    response = await webhook_service.replay_run_webhook("o_replay", "wr_replay", None, api_key="test-key")
    assert response.status_code == 200


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


@pytest.mark.asyncio
async def test_replay_reports_a_refused_target_without_an_unexpected_error() -> None:
    async def _deliver(**kwargs: object) -> httpx.Response:
        raise BlockedHost("rebinding.example.test")

    with patch("skyvern.services.webhook_service.app.AGENT_FUNCTION.deliver_webhook", _deliver):
        status_code, _latency, _body, error = await webhook_service._deliver_webhook(
            url=REBINDING_HOST_URL, payload="{}", headers={}
        )

    assert status_code is None
    assert error == "The target host was refused by SSRF protection."


@pytest.fixture
def oss_outbound_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = AgentFunction()
    monkeypatch.setattr(webhook_delivery.app.AGENT_FUNCTION, "deliver_webhook", agent_function.deliver_webhook)
    monkeypatch.setattr(
        otp_service.app.AGENT_FUNCTION,
        "post_totp_verification_request",
        agent_function.post_totp_verification_request,
    )
    monkeypatch.setattr(webhook_delivery, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    monkeypatch.setattr(otp_service, "asyncio", ScopedAsyncio(sleep=AsyncMock()))


async def _deliver_run_webhook(url: str, attempts: WebhookDeliveryAttempts | None = None) -> httpx.Response:
    return await webhook_delivery.deliver_webhook_with_retries(
        url=url,
        payload="{}",
        headers={},
        timeout_seconds=5.0,
        organization_id="o_1",
        run_id="wr_1",
        attempts=attempts,
    )


async def _post_totp(url: str) -> object:
    return await otp_service._post_totp_verification_url(
        url=url, signed_payload="{}", headers={}, organization_id="o_1", retry_timeout=0
    )


@pytest.mark.asyncio
async def test_run_webhook_delivery_connects_to_validated_ip_after_dns_rebind(
    rebinding_dns: None, oss_outbound_seams: None, capture_connect_target: dict[str, object]
) -> None:
    response = await _deliver_run_webhook(REBINDING_HOST_URL)

    assert response.status_code == 200
    assert capture_connect_target["connect_host"] == PUBLIC_IP
    assert capture_connect_target["sni_hostname"] == "rebinding.example.test"


@pytest.mark.asyncio
async def test_totp_verification_post_connects_to_validated_ip_after_dns_rebind(
    rebinding_dns: None, oss_outbound_seams: None, capture_connect_target: dict[str, object]
) -> None:
    status_code, *_ = await _post_totp(REBINDING_HOST_URL)

    assert status_code == 200
    assert capture_connect_target["connect_host"] == PUBLIC_IP
    assert capture_connect_target["host_header"] == "rebinding.example.test"


@pytest.fixture
def internal_name_resolves_to_rfc1918(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", _resolve)


_REFUSED_TARGETS = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/hook",
    "https://internal-api.example.test/hook",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _REFUSED_TARGETS)
async def test_run_webhook_delivery_refuses_internal_targets_without_retrying(
    url: str,
    internal_name_resolves_to_rfc1918: None,
    oss_outbound_seams: None,
    capture_connect_target: dict[str, object],
) -> None:
    attempts = WebhookDeliveryAttempts()
    with pytest.raises(BlockedHost) as exc_info:
        await _deliver_run_webhook(url, attempts)

    assert capture_connect_target == {}
    assert attempts.count == 1
    failure_reason = webhook_delivery.format_no_response_failure_reason(exc_info.value)
    assert "ALLOWED_HOSTS" in failure_reason
    assert "10.0.0.5" not in failure_reason
    assert (
        webhook_delivery.refine_exhausted_webhook_delivery(
            WebhookDeliveryStatus.exhausted_unattributed, error=exc_info.value
        )
        == WebhookDeliveryStatus.exhausted_customer_config
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _REFUSED_TARGETS)
async def test_totp_verification_refuses_internal_targets(
    url: str,
    internal_name_resolves_to_rfc1918: None,
    oss_outbound_seams: None,
    capture_connect_target: dict[str, object],
) -> None:
    with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
        await otp_service._get_otp_value_from_url(organization_id="o_1", url=url, api_key="key", task_id="tsk_1")

    assert "exception_type=BlockedHost" in str(exc_info.value)
    assert "ALLOWED_HOSTS" in str(exc_info.value)
    assert "10.0.0.5" not in str(exc_info.value)
    assert capture_connect_target == {}


@pytest_asyncio.fixture
async def forward_proxy() -> AsyncIterator[list[bytes]]:
    """A local HTTP forward proxy that answers every request itself, recording each request line."""
    request_lines: list[bytes] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_lines.append(head.split(b"\r\n", 1)[0])
        length = next(
            (
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            ),
            0,
        )
        await reader.readexactly(length)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with pytest.MonkeyPatch.context() as env:
        env.setenv("http_proxy", f"http://127.0.0.1:{port}")
        yield request_lines
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_run_webhook_delivery_ignores_an_environment_proxy_by_default(
    rebinding_dns: None,
    oss_outbound_seams: None,
    forward_proxy: list[bytes],
    capture_connect_target: dict[str, object],
) -> None:
    response = await _deliver_run_webhook("http://rebinding.example.test/hook")

    assert response.status_code == 200
    assert capture_connect_target["connect_host"] == PUBLIC_IP
    assert forward_proxy == []


@pytest.mark.asyncio
async def test_run_webhook_delivery_goes_through_an_environment_proxy_when_opted_in(
    rebinding_dns: None, oss_outbound_seams: None, forward_proxy: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "OUTBOUND_TRUST_ENV_PROXY", True)

    response = await _deliver_run_webhook("http://rebinding.example.test/hook")

    assert response.status_code == 200
    assert forward_proxy == [b"POST http://rebinding.example.test/hook HTTP/1.1"]


@pytest.mark.asyncio
async def test_run_webhook_delivery_bypassing_the_environment_proxy_stays_pinned(
    rebinding_dns: None,
    oss_outbound_seams: None,
    forward_proxy: list[bytes],
    capture_connect_target: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("no_proxy", "rebinding.example.test")

    response = await _deliver_run_webhook("http://rebinding.example.test/hook")

    assert response.status_code == 200
    assert capture_connect_target["connect_host"] == PUBLIC_IP
    assert forward_proxy == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _REFUSED_TARGETS)
async def test_run_webhook_delivery_through_an_environment_proxy_still_refuses_internal_targets(
    url: str, internal_name_resolves_to_rfc1918: None, oss_outbound_seams: None, forward_proxy: list[bytes]
) -> None:
    with pytest.raises(BlockedHost):
        await _deliver_run_webhook(url)

    assert forward_proxy == []


TOTP_HOST = "totp.example.test"
REDIRECT_TARGET_IP = "93.184.216.35"


@pytest.fixture
def totp_redirect_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {TOTP_HOST: PUBLIC_IP, "next.example.test": REDIRECT_TARGET_IP, "internal.example.test": "10.0.0.5"}

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 443))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", _resolve)


@pytest.fixture
def redirecting_endpoints(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, httpx.Response], list[tuple]]:
    """Answer each request by its Host header, recording (method, connect host, Host header, body)."""
    responses: dict[str, httpx.Response] = {}
    requests: list[tuple] = []

    async def _answer(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        host_header = request.headers["host"]
        requests.append((request.method, request.url.host, host_header, await request.aread()))
        return responses[host_header]

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _answer)
    return responses, requests


def _redirect(status_code: int, location: str) -> httpx.Response:
    return httpx.Response(status_code, headers={"Location": location})


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "next_method", "next_body"), [(303, "GET", b""), (302, "GET", b"")])
async def test_totp_redirect_to_a_public_host_is_followed_and_pinned(
    status_code: int,
    next_method: str,
    next_body: bytes,
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    redirecting_endpoints: tuple[dict[str, httpx.Response], list[tuple]],
) -> None:
    responses, requests = redirecting_endpoints
    responses[TOTP_HOST] = _redirect(status_code, "https://next.example.test/code")
    responses["next.example.test"] = httpx.Response(200, json={"verification_code": "123456"})

    status, _headers, body, _is_json = await _post_totp(f"https://{TOTP_HOST}/totp")

    assert (status, body) == (200, {"verification_code": "123456"})
    assert requests == [
        ("POST", PUBLIC_IP, TOTP_HOST, b"{}"),
        (next_method, REDIRECT_TARGET_IP, "next.example.test", next_body),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "expected_reason"),
    [
        ("https://127.0.0.1:8000/code", "exception_type=BlockedHost"),
        ("https://internal.example.test/code", "exception_type=BlockedHost"),
        ("http://next.example.test/code", "detail=redirect from https to http refused"),
    ],
)
async def test_totp_redirect_to_a_refused_target_is_not_followed(
    location: str,
    expected_reason: str,
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    redirecting_endpoints: tuple[dict[str, httpx.Response], list[tuple]],
) -> None:
    responses, requests = redirecting_endpoints
    responses[TOTP_HOST] = _redirect(302, location)

    with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
        await otp_service._get_otp_value_from_url(
            organization_id="o_1", url=f"https://{TOTP_HOST}/totp", api_key="key", task_id="tsk_1"
        )

    assert expected_reason in str(exc_info.value)
    assert [request[2] for request in requests] == [TOTP_HOST]


@pytest.mark.asyncio
async def test_totp_redirect_loop_stops_after_three_hops(
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    redirecting_endpoints: tuple[dict[str, httpx.Response], list[tuple]],
) -> None:
    responses, requests = redirecting_endpoints
    responses[TOTP_HOST] = _redirect(307, "/totp")

    with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
        await otp_service._get_otp_value_from_url(
            organization_id="o_1", url=f"https://{TOTP_HOST}/totp", api_key="key", task_id="tsk_1"
        )

    assert "detail=more than 3 redirects" in str(exc_info.value)
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "location", "signed_on_next_hop"),
    [(303, "https://next.example.test/code", False), (307, "/code", True)],
)
async def test_totp_redirect_drops_the_signature_when_the_origin_changes(
    status_code: int,
    location: str,
    signed_on_next_hop: bool,
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, bool]] = []
    custom_header_seen: list[bool] = []

    async def _answer(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["host"], "x-skyvern-signature" in request.headers))
        custom_header_seen.append("x-api-key" in request.headers)
        if len(seen) == 1:
            return _redirect(status_code, location)
        return httpx.Response(200, json={"verification_code": "123456"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _answer)

    await otp_service._post_totp_verification_url(
        url=f"https://{TOTP_HOST}/totp",
        signed_payload="{}",
        headers={"x-skyvern-signature": "sig", "x-skyvern-timestamp": "1", "x-api-key": "secret"},
        organization_id="o_1",
        retry_timeout=0,
    )

    assert seen[0] == (TOTP_HOST, True)
    assert seen[1][1] is signed_on_next_hop
    assert custom_header_seen == [True, signed_on_next_hop]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [307, 308])
async def test_totp_cross_origin_redirect_that_keeps_the_body_is_refused(
    status_code: int,
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    redirecting_endpoints: tuple[dict[str, httpx.Response], list[tuple]],
) -> None:
    responses, requests = redirecting_endpoints
    responses[TOTP_HOST] = _redirect(status_code, "https://next.example.test/code")

    with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
        await otp_service._get_otp_value_from_url(
            organization_id="o_1", url=f"https://{TOTP_HOST}/totp", api_key="key", task_id="tsk_1"
        )

    assert "cross-origin 307/308 redirect refused" in str(exc_info.value)
    assert [request[2] for request in requests] == [TOTP_HOST]


@pytest.mark.asyncio
async def test_totp_redirect_to_a_refused_target_at_a_later_hop_is_not_followed(
    totp_redirect_dns: None,
    oss_outbound_seams: None,
    redirecting_endpoints: tuple[dict[str, httpx.Response], list[tuple]],
) -> None:
    responses, requests = redirecting_endpoints
    responses[TOTP_HOST] = _redirect(302, "https://next.example.test/code")
    responses["next.example.test"] = _redirect(302, "https://127.0.0.1:8000/code")

    with pytest.raises(FailedToGetTOTPVerificationCode) as exc_info:
        await otp_service._get_otp_value_from_url(
            organization_id="o_1", url=f"https://{TOTP_HOST}/totp", api_key="key", task_id="tsk_1"
        )

    assert "exception_type=BlockedHost" in str(exc_info.value)
    assert [request[2] for request in requests] == [TOTP_HOST, "next.example.test"]


@pytest.mark.asyncio
async def test_totp_request_is_capped_by_a_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _slow_drip(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _slow_drip)
    started = time.monotonic()

    with pytest.raises(httpx.ReadTimeout):
        await AgentFunction().post_totp_verification_request(
            url=f"https://{TOTP_HOST}/totp", payload="{}", headers={}, timeout_seconds=0.2, resolved_ips=(PUBLIC_IP,)
        )

    assert time.monotonic() - started < 2
