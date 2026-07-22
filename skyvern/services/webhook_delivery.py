"""Bounded-retry wrapper around ``app.AGENT_FUNCTION.deliver_webhook``."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
import structlog

from skyvern.exceptions import InvalidUrl
from skyvern.forge import app

LOG = structlog.get_logger()

NON_5XX_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset(
    {
        403,  # included for intermittent WAF/proxy denials; permanent auth failures stay observable via status_code in the retry log
        408,
        425,
        429,
    }
)

WEBHOOK_DELIVERY_MAX_ATTEMPTS = 3
WEBHOOK_DELIVERY_RETRY_BASE_DELAY_SECONDS = 1.0
WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS = 30.0


@dataclass(frozen=True)
class PreparedWorkflowWebhook:
    """Process-local signed workflow webhook request.

    Do not return this object from Temporal activities or workflows; signed_payload
    can be large and must not be persisted in Temporal history.
    """

    workflow_id: str
    workflow_run_id: str
    organization_id: str
    webhook_callback_url: str
    signed_payload: str
    headers: dict[str, str]
    payload_for_log: str


def is_retryable_status(status_code: int) -> bool:
    return status_code in NON_5XX_RETRYABLE_STATUS_CODES or 500 <= status_code < 600


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return max(float(stripped), 0.0)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)


def _compute_backoff_delay(attempt: int, base_delay_seconds: float, response: httpx.Response | None) -> float:
    hinted = _parse_retry_after(response.headers.get("Retry-After")) if response is not None else None
    if hinted is not None:
        return min(hinted, WEBHOOK_DELIVERY_MAX_RETRY_AFTER_SECONDS)
    return base_delay_seconds * (2**attempt) + random.uniform(0, base_delay_seconds)


async def deliver_webhook_with_retries(
    url: str,
    payload: str,
    headers: dict[str, str],
    timeout_seconds: float,
    organization_id: str | None,
    run_id: str | None,
    max_attempts: int = WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    base_delay_seconds: float = WEBHOOK_DELIVERY_RETRY_BASE_DELAY_SECONDS,
) -> httpx.Response:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        # Reject scheme-less/host-less targets before the outbound call so the
        # failure is classified as invalid input rather than a raw
        # httpx.UnsupportedProtocol from the transport.
        raise InvalidUrl(url)

    last_response: httpx.Response | None = None
    last_exc: Exception | None = None

    for attempt in range(max_attempts):
        try:
            response = await app.AGENT_FUNCTION.deliver_webhook(
                url=url,
                payload=payload,
                headers=headers,
                timeout_seconds=timeout_seconds,
                organization_id=organization_id,
                run_id=run_id,
            )
            last_response = response
            last_exc = None
            if 200 <= response.status_code < 300:
                return response
            if not is_retryable_status(response.status_code):
                return response
        except httpx.HTTPStatusError as exc:
            # NAT egress proxy client calls resp.raise_for_status(), so a proxy-side
            # 5xx surfaces here rather than as a returned Response.
            last_response = exc.response
            last_exc = exc
            if not is_retryable_status(exc.response.status_code):
                raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_response = None
            last_exc = exc

        if attempt < max_attempts - 1:
            delay = _compute_backoff_delay(attempt, base_delay_seconds, last_response)
            status_code = last_response.status_code if last_response is not None else None
            log_fn = LOG.warning if status_code == 403 else LOG.info
            log_fn(
                "Retrying webhook delivery after transient failure",
                url=url,
                run_id=run_id,
                organization_id=organization_id,
                attempt=attempt + 1,
                max_attempts=max_attempts,
                status_code=status_code,
                error=str(last_exc) if last_exc is not None else None,
                sleep_seconds=delay,
                retry_after_present=last_response is not None and "Retry-After" in last_response.headers,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    if last_response is None:
        raise RuntimeError("deliver_webhook_with_retries exited without a response or exception")
    return last_response
