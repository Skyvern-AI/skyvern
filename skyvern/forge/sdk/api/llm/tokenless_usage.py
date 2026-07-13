"""Tokenless routed-request cost tracking.

Tokenless reports the cost of a complete routed request through its usage API.  The
OpenAI-compatible response carries the request identifier in ``X-Request-Id``;
callers must retain that identifier and resolve it after the workflow finishes.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import structlog

from skyvern.config import settings

TOKENLESS_NANOS_PER_USD = 1_000_000_000
_MAX_CONCURRENT_LOOKUPS = 8
_MAX_LOOKUP_ATTEMPTS = 3
_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504, 529}

LOG = structlog.get_logger()


class TokenlessUsageError(RuntimeError):
    """Raised when a Tokenless routed-request cost cannot be resolved."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TokenlessRequestCost:
    """Cost and usage returned for one Tokenless routed request."""

    request_id: str
    cost_nanos: int
    input_tokens: int
    output_tokens: int
    call_id: str | None = None


@dataclass(frozen=True)
class TokenlessRunCost:
    """Aggregated Tokenless cost for one workflow run."""

    agent_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    tokenless_request_count: int
    cost_status: str
    resolved_call_costs: tuple[TokenlessRequestCost, ...] = ()


def _usage_api_base_url(api_base_url: str | None) -> str:
    """Derive the Tokenless usage API origin from the model API base URL."""
    if not api_base_url:
        raise TokenlessUsageError("OPENAI_COMPATIBLE_API_BASE is required for Tokenless cost lookup")

    parsed = urlsplit(api_base_url)
    if not parsed.scheme or not parsed.netloc:
        raise TokenlessUsageError("OPENAI_COMPATIBLE_API_BASE must be an absolute URL")
    return f"{parsed.scheme}://{parsed.netloc}/v1/usage"


def _integer_field(payload: dict[str, Any], field_name: str) -> int:
    """Read a non-negative integer usage field from a Tokenless response."""
    value = payload.get(field_name)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TokenlessUsageError(f"Tokenless usage response omitted {field_name}") from exc
    if parsed < 0:
        raise TokenlessUsageError(f"Tokenless usage response returned negative {field_name}")
    return parsed


class TokenlessUsageTracker:
    """Track and resolve Tokenless request IDs grouped by workflow run.

    The in-memory map supports the hot path; callers may also supply mappings read
    from Skyvern's internal call ledger so resolution survives worker boundaries
    and process restarts.
    """

    def __init__(self) -> None:
        self._request_ids_by_workflow_run: dict[str, set[str]] = {}
        self._call_ids_by_workflow_run: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def record_request(self, workflow_run_id: str, request_id: str, call_id: str | None = None) -> None:
        """Associate a Tokenless request with a workflow run, deduplicating IDs."""
        if not workflow_run_id or not request_id:
            return
        async with self._lock:
            self._request_ids_by_workflow_run.setdefault(workflow_run_id, set()).add(request_id)
            if call_id:
                self._call_ids_by_workflow_run.setdefault(workflow_run_id, {})[request_id] = call_id

    async def request_ids(self, workflow_run_id: str) -> set[str]:
        """Return a copy of the request IDs recorded for a workflow run."""
        async with self._lock:
            return set(self._request_ids_by_workflow_run.get(workflow_run_id, set()))

    async def clear(self, workflow_run_id: str) -> None:
        """Discard request IDs after an evaluation report has been written."""
        async with self._lock:
            self._request_ids_by_workflow_run.pop(workflow_run_id, None)
            self._call_ids_by_workflow_run.pop(workflow_run_id, None)

    async def _fetch_request_cost(
        self,
        client: httpx.AsyncClient,
        request_id: str,
    ) -> TokenlessRequestCost:
        """Fetch one routed-request cost with bounded transient retries."""
        usage_base_url = _usage_api_base_url(settings.OPENAI_COMPATIBLE_API_BASE)
        request_url = f"{usage_base_url}/requests/{quote(request_id, safe='')}"
        if not settings.OPENAI_COMPATIBLE_API_KEY:
            raise TokenlessUsageError("OPENAI_COMPATIBLE_API_KEY is required for Tokenless cost lookup")
        headers = {"Authorization": f"Bearer {settings.OPENAI_COMPATIBLE_API_KEY}"}

        for attempt in range(_MAX_LOOKUP_ATTEMPTS):
            try:
                response = await client.get(request_url, headers=headers)
            except httpx.TransportError as exc:
                if attempt + 1 == _MAX_LOOKUP_ATTEMPTS:
                    raise TokenlessUsageError("Tokenless usage request failed") from exc
                await self._sleep_before_retry(attempt, retry_after=None)
                continue

            if response.status_code in _TRANSIENT_STATUS_CODES:
                if attempt + 1 == _MAX_LOOKUP_ATTEMPTS:
                    raise TokenlessUsageError(
                        "Tokenless usage request remained transiently unavailable",
                        status_code=response.status_code,
                    )
                await self._sleep_before_retry(attempt, retry_after=response.headers.get("retry-after"))
                continue

            if response.status_code == 404:
                raise TokenlessUsageError("Tokenless routed request was not found", status_code=404)

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise TokenlessUsageError(
                    "Tokenless usage request was rejected",
                    status_code=response.status_code,
                ) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise TokenlessUsageError("Tokenless usage response was not valid JSON") from exc

            if not isinstance(payload, dict):
                raise TokenlessUsageError("Tokenless usage response was not an object")

            returned_request_id = payload.get("request_id")
            if returned_request_id and returned_request_id != request_id:
                raise TokenlessUsageError("Tokenless usage response request ID did not match the lookup")

            return TokenlessRequestCost(
                request_id=request_id,
                cost_nanos=_integer_field(payload, "total_cost_nanos"),
                input_tokens=_integer_field(payload, "total_input_tokens"),
                output_tokens=_integer_field(payload, "total_output_tokens"),
            )

        raise TokenlessUsageError("Tokenless usage request could not be resolved")

    @staticmethod
    async def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
        """Honor Retry-After with bounded exponential backoff and jitter."""
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                delay = None
        if delay is None:
            delay = 1.0 * (2**attempt) + random.uniform(0.0, 0.25)
        await asyncio.sleep(min(delay, 10.0))

    async def resolve(
        self,
        workflow_run_id: str,
        persisted_request_call_ids: dict[str, str] | None = None,
    ) -> TokenlessRunCost:
        """Resolve all known request IDs for a workflow run into an exact total."""
        request_ids = await self.request_ids(workflow_run_id)
        async with self._lock:
            call_ids = dict(self._call_ids_by_workflow_run.get(workflow_run_id, {}))
        if persisted_request_call_ids:
            request_ids.update(persisted_request_call_ids)
            call_ids.update(persisted_request_call_ids)
        if not request_ids:
            return TokenlessRunCost(
                agent_cost_usd=None,
                input_tokens=0,
                output_tokens=0,
                tokenless_request_count=0,
                cost_status="incomplete",
            )

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)
        unresolved_statuses: dict[int | None, int] = {}

        async def fetch(client: httpx.AsyncClient, request_id: str) -> TokenlessRequestCost | None:
            async with semaphore:
                try:
                    return await self._fetch_request_cost(client, request_id)
                except TokenlessUsageError as exc:
                    unresolved_statuses[exc.status_code] = unresolved_statuses.get(exc.status_code, 0) + 1
                    return None

        limits = httpx.Limits(
            max_connections=_MAX_CONCURRENT_LOOKUPS,
            max_keepalive_connections=_MAX_CONCURRENT_LOOKUPS,
        )
        async with httpx.AsyncClient(timeout=settings.LLM_CONFIG_TIMEOUT, limits=limits) as client:
            resolved = [
                cost
                for cost in await asyncio.gather(*(fetch(client, request_id) for request_id in request_ids))
                if cost
            ]
        status = "exact" if len(resolved) == len(request_ids) else "incomplete"
        if status != "exact":
            LOG.warning(
                "Tokenless usage cost resolution incomplete",
                workflow_run_id=workflow_run_id,
                request_count=len(request_ids),
                resolved_count=len(resolved),
                unresolved_statuses=unresolved_statuses,
            )
            return TokenlessRunCost(
                agent_cost_usd=None,
                input_tokens=sum(cost.input_tokens for cost in resolved),
                output_tokens=sum(cost.output_tokens for cost in resolved),
                tokenless_request_count=len(request_ids),
                cost_status=status,
                resolved_call_costs=tuple(
                    replace(cost, call_id=call_ids.get(cost.request_id))
                    for cost in resolved
                ),
            )

        total_cost_nanos = sum(cost.cost_nanos for cost in resolved)
        return TokenlessRunCost(
            agent_cost_usd=total_cost_nanos / TOKENLESS_NANOS_PER_USD,
            input_tokens=sum(cost.input_tokens for cost in resolved),
            output_tokens=sum(cost.output_tokens for cost in resolved),
            tokenless_request_count=len(request_ids),
            cost_status=status,
            resolved_call_costs=tuple(
                replace(cost, call_id=call_ids.get(cost.request_id)) for cost in resolved
            ),
        )


tokenless_usage_tracker = TokenlessUsageTracker()
