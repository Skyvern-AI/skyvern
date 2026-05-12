"""Tests for webhook delivery retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from skyvern.services.webhook_delivery import (
    WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS,
    deliver_webhook_with_retries,
    is_retryable_status,
)


def _response(status_code: int, body: str = "", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code=status_code, content=body.encode("utf-8"), headers=headers or {})


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep with a no-op recorder so tests don't actually wait."""
    recorded: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("skyvern.services.webhook_delivery.asyncio.sleep", _sleep)
    return recorded


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin random.uniform to 0 so backoff delays are deterministic."""
    monkeypatch.setattr("skyvern.services.webhook_delivery.random.uniform", lambda a, b: 0.0)


@pytest.mark.asyncio
async def test_returns_immediately_on_success(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(200, "ok"))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 1
    assert fake_sleep == []


@pytest.mark.asyncio
async def test_retries_on_403_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[_response(403, "forbidden"), _response(200, "ok")])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2
    assert len(fake_sleep) == 1


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[_response(503, "unavailable"), _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[_response(429), _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_does_not_retry_on_400_bad_request(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(400, "bad request"))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 400
    assert deliver.await_count == 1
    assert fake_sleep == []


@pytest.mark.asyncio
async def test_does_not_retry_on_401_unauthorized(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(401))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 401
    assert deliver.await_count == 1


@pytest.mark.asyncio
async def test_does_not_retry_on_404(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(404))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 404
    assert deliver.await_count == 1


@pytest.mark.asyncio
async def test_returns_final_failure_when_all_attempts_fail(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(503, "still down"))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 503
    assert deliver.await_count == WEBHOOK_DELIVERY_MAX_ATTEMPTS
    assert len(fake_sleep) == WEBHOOK_DELIVERY_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_retries_on_network_error_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[httpx.ConnectError("conn refused"), _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_retries_on_timeout_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_retries_on_remote_protocol_error_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(
        side_effect=[
            httpx.RemoteProtocolError("Server disconnected without sending a response"),
            _response(200),
        ],
    )
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_raises_when_all_network_attempts_fail(fake_sleep: list[float]) -> None:
    err = httpx.ConnectError("conn refused")
    deliver = AsyncMock(side_effect=[err, err, err])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        with pytest.raises(httpx.ConnectError):
            await deliver_webhook_with_retries(
                url="https://example.com/hook",
                payload="{}",
                headers={},
                timeout_seconds=30.0,
                organization_id="o_1",
                run_id="wr_1",
            )

    assert deliver.await_count == WEBHOOK_DELIVERY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_backoff_is_exponential(fake_sleep: list[float], no_jitter: None) -> None:
    deliver = AsyncMock(return_value=_response(503))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    assert fake_sleep == pytest.approx([1.0, 2.0])


def test_is_retryable_status_covers_full_5xx_range_and_curated_4xx() -> None:
    for code in (403, 408, 425, 429):
        assert is_retryable_status(code)
    for code in (500, 501, 502, 503, 504, 520, 522, 599):
        assert is_retryable_status(code)
    for code in (200, 201, 301, 400, 401, 404, 405, 410, 418, 422):
        assert not is_retryable_status(code)


@pytest.mark.asyncio
async def test_retries_on_521_then_succeeds(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=[_response(521, "origin down"), _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_retries_on_http_status_error_5xx_then_succeeds(fake_sleep: list[float]) -> None:
    """NAT proxy client raises HTTPStatusError on proxy-side 5xx via raise_for_status()."""
    request = httpx.Request("POST", "https://proxy.example/proxy/webhook")
    err = httpx.HTTPStatusError("503", request=request, response=_response(503))
    deliver = AsyncMock(side_effect=[err, _response(200)])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
        )

    assert resp.status_code == 200
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_re_raises_http_status_error_on_non_retryable(fake_sleep: list[float]) -> None:
    request = httpx.Request("POST", "https://proxy.example/proxy/webhook")
    err = httpx.HTTPStatusError("400", request=request, response=_response(400))
    deliver = AsyncMock(side_effect=err)
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        with pytest.raises(httpx.HTTPStatusError):
            await deliver_webhook_with_retries(
                url="https://example.com/hook",
                payload="{}",
                headers={},
                timeout_seconds=30.0,
                organization_id="o_1",
                run_id="wr_1",
            )

    assert deliver.await_count == 1
    assert fake_sleep == []


@pytest.mark.asyncio
async def test_raises_last_http_status_error_after_exhaustion(fake_sleep: list[float]) -> None:
    request = httpx.Request("POST", "https://proxy.example/proxy/webhook")
    err = httpx.HTTPStatusError("502", request=request, response=_response(502))
    deliver = AsyncMock(side_effect=[err, err, err])
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        with pytest.raises(httpx.HTTPStatusError):
            await deliver_webhook_with_retries(
                url="https://example.com/hook",
                payload="{}",
                headers={},
                timeout_seconds=30.0,
                organization_id="o_1",
                run_id="wr_1",
            )

    assert deliver.await_count == WEBHOOK_DELIVERY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_backoff_adds_jitter_within_base_window(fake_sleep: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.services.webhook_delivery.random.uniform", lambda a, b: 0.7)
    deliver = AsyncMock(return_value=_response(503))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    # attempt 0 -> 1.0 * 2**0 + 0.7 = 1.7; attempt 1 -> 1.0 * 2**1 + 0.7 = 2.7
    assert fake_sleep == pytest.approx([1.7, 2.7])


@pytest.mark.asyncio
async def test_backoff_jitter_keeps_delays_non_decreasing(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(503))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    for attempt, delay in enumerate(fake_sleep):
        base = 1.0 * (2**attempt)
        assert base <= delay <= base + 1.0
    for prev, curr in zip(fake_sleep, fake_sleep[1:]):
        assert curr >= prev


@pytest.mark.asyncio
async def test_honors_numeric_retry_after_header(fake_sleep: list[float], no_jitter: None) -> None:
    deliver = AsyncMock(
        side_effect=[_response(429, headers={"Retry-After": "5"}), _response(200)],
    )
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    assert resp.status_code == 200
    assert fake_sleep == [5.0]


@pytest.mark.asyncio
async def test_caps_retry_after_at_max(fake_sleep: list[float], no_jitter: None) -> None:
    deliver = AsyncMock(
        side_effect=[
            _response(429, headers={"Retry-After": str(int(WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS * 10))}),
            _response(200),
        ],
    )
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    assert fake_sleep == [WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS]


@pytest.mark.asyncio
async def test_honors_http_date_retry_after(fake_sleep: list[float], no_jitter: None) -> None:
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(seconds=4)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    deliver = AsyncMock(
        side_effect=[_response(429, headers={"Retry-After": http_date}), _response(200)],
    )
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    assert len(fake_sleep) == 1
    assert 0.0 <= fake_sleep[0] <= WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS


@pytest.mark.asyncio
async def test_ignores_unparseable_retry_after(fake_sleep: list[float], no_jitter: None) -> None:
    deliver = AsyncMock(
        side_effect=[_response(429, headers={"Retry-After": "soon"}), _response(200)],
    )
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            base_delay_seconds=1.0,
        )

    assert fake_sleep == [1.0]


@pytest.mark.asyncio
async def test_max_attempts_one_returns_retryable_failure_without_retry(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(return_value=_response(503, "still down"))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        resp = await deliver_webhook_with_retries(
            url="https://example.com/hook",
            payload="{}",
            headers={},
            timeout_seconds=30.0,
            organization_id="o_1",
            run_id="wr_1",
            max_attempts=1,
        )

    assert resp.status_code == 503
    assert deliver.await_count == 1
    assert fake_sleep == []


@pytest.mark.asyncio
async def test_max_attempts_one_raises_network_error_without_retry(fake_sleep: list[float]) -> None:
    deliver = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("skyvern.services.webhook_delivery.app.AGENT_FUNCTION.deliver_webhook", deliver):
        with pytest.raises(httpx.ConnectError):
            await deliver_webhook_with_retries(
                url="https://example.com/hook",
                payload="{}",
                headers={},
                timeout_seconds=30.0,
                organization_id="o_1",
                run_id="wr_1",
                max_attempts=1,
            )

    assert deliver.await_count == 1
    assert fake_sleep == []
