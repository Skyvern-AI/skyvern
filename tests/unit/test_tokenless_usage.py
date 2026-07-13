from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from skyvern.forge.sdk.api.llm import tokenless_usage


@pytest.mark.asyncio
async def test_tokenless_tracker_deduplicates_request_ids_and_sums_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = tokenless_usage.TokenlessUsageTracker()
    await tracker.record_request("wr_123", "req_a", call_id="call_a")
    await tracker.record_request("wr_123", "req_a", call_id="call_a")
    await tracker.record_request("wr_123", "req_b", call_id="call_b")

    costs = {
        "req_a": tokenless_usage.TokenlessRequestCost("req_a", 1_250_000_000, 10, 20),
        "req_b": tokenless_usage.TokenlessRequestCost("req_b", 750_000_000, 30, 40),
    }
    monkeypatch.setattr(
        tracker,
        "_fetch_request_cost",
        AsyncMock(side_effect=lambda _client, request_id: costs[request_id]),
    )

    summary = await tracker.resolve("wr_123")

    assert summary.agent_cost_usd == 2.0
    assert summary.input_tokens == 40
    assert summary.output_tokens == 60
    assert summary.tokenless_request_count == 2
    assert summary.cost_status == "exact"
    assert {cost.call_id for cost in summary.resolved_call_costs} == {"call_a", "call_b"}


@pytest.mark.asyncio
async def test_tokenless_tracker_keeps_concurrent_workflow_runs_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent model calls must not associate a request with the wrong run."""
    tracker = tokenless_usage.TokenlessUsageTracker()
    await asyncio.gather(
        *(tracker.record_request(run_id, request_id) for run_id, request_id in [("wr_1", "req_1"), ("wr_2", "req_2")])
    )

    costs = {
        "req_1": tokenless_usage.TokenlessRequestCost("req_1", 100, 1, 2),
        "req_2": tokenless_usage.TokenlessRequestCost("req_2", 900, 3, 4),
    }
    monkeypatch.setattr(
        tracker,
        "_fetch_request_cost",
        AsyncMock(side_effect=lambda _client, request_id: costs[request_id]),
    )

    run_1, run_2 = await asyncio.gather(tracker.resolve("wr_1"), tracker.resolve("wr_2"))

    assert (run_1.agent_cost_usd, run_1.input_tokens, run_1.output_tokens) == (0.0000001, 1, 2)
    assert (run_2.agent_cost_usd, run_2.input_tokens, run_2.output_tokens) == (0.0000009, 3, 4)


@pytest.mark.asyncio
async def test_tokenless_tracker_marks_missing_request_cost_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = tokenless_usage.TokenlessUsageTracker()
    await tracker.record_request("wr_123", "req_a")
    await tracker.record_request("wr_123", "req_missing")

    async def fetch(
        _client: httpx.AsyncClient,
        request_id: str,
    ) -> tokenless_usage.TokenlessRequestCost:
        if request_id == "req_missing":
            raise tokenless_usage.TokenlessUsageError("missing")
        return tokenless_usage.TokenlessRequestCost(request_id, 100, 2, 3)

    monkeypatch.setattr(tracker, "_fetch_request_cost", fetch)

    summary = await tracker.resolve("wr_123")

    assert summary.agent_cost_usd is None
    assert summary.input_tokens == 2
    assert summary.output_tokens == 3
    assert summary.tokenless_request_count == 2
    assert summary.cost_status == "incomplete"


@pytest.mark.asyncio
async def test_fetch_request_cost_retries_transient_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tokenless_usage.settings,
        "OPENAI_COMPATIBLE_API_BASE",
        "https://api.example.com/openai/v1",
    )
    monkeypatch.setattr(tokenless_usage.settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    requests_seen: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("GET", "https://api.example.com")),
            httpx.Response(
                200,
                json={
                    "request_id": "req_a",
                    "total_cost_nanos": 123,
                    "total_input_tokens": 4,
                    "total_output_tokens": 5,
                },
                request=httpx.Request("GET", "https://api.example.com"),
            ),
        ]
    )

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return next(responses)

    transport = httpx.MockTransport(handle_request)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(tokenless_usage.asyncio, "sleep", AsyncMock())

    try:
        result = await tokenless_usage.TokenlessUsageTracker()._fetch_request_cost(client, "req_a")
    finally:
        await client.aclose()

    assert result.cost_nanos == 123
    assert result.input_tokens == 4
    assert result.output_tokens == 5
    assert requests_seen[0].url == "https://api.example.com/v1/usage/requests/req_a"
    assert requests_seen[0].headers["authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_fetch_request_cost_retries_529_and_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tokenless_usage.settings,
        "OPENAI_COMPATIBLE_API_BASE",
        "https://api.example.com/openai/v1",
    )
    monkeypatch.setattr(tokenless_usage.settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    sleep = AsyncMock()
    monkeypatch.setattr(tokenless_usage.asyncio, "sleep", sleep)
    responses = iter(
        [
            httpx.Response(
                529,
                headers={"Retry-After": "3"},
                request=httpx.Request("GET", "https://api.example.com"),
            ),
            httpx.Response(
                200,
                json={
                    "request_id": "req_a",
                    "total_cost_nanos": 123,
                    "total_input_tokens": 4,
                    "total_output_tokens": 5,
                },
                request=httpx.Request("GET", "https://api.example.com"),
            ),
        ]
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: next(responses)))

    try:
        result = await tokenless_usage.TokenlessUsageTracker()._fetch_request_cost(client, "req_a")
    finally:
        await client.aclose()

    assert result.cost_nanos == 123
    sleep.assert_awaited_once_with(3.0)


@pytest.mark.asyncio
async def test_fetch_request_cost_treats_404_as_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tokenless_usage.settings,
        "OPENAI_COMPATIBLE_API_BASE",
        "https://api.example.com/openai/v1",
    )
    monkeypatch.setattr(tokenless_usage.settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    request = httpx.Request("GET", "https://api.example.com/v1/usage/requests/req_missing")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(404, request=request)))

    try:
        with pytest.raises(tokenless_usage.TokenlessUsageError):
            await tokenless_usage.TokenlessUsageTracker()._fetch_request_cost(client, "req_missing")
    finally:
        await client.aclose()
