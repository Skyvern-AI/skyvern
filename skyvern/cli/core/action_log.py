from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import structlog

from skyvern.client.core.request_options import RequestOptions
from skyvern.library.skyvern import Skyvern
from skyvern.schemas.action_log import (
    ACTION_LOG_MAX_BODY_BYTES,
    ActionLogBatchRequest,
    ActionLogEvent,
    ActionLogOutcome,
    project_action_event,
)

from .client import _resolve_self_base_url, get_active_api_key, get_skyvern
from .result import BrowserContext
from .session_manager import is_stateless_http_mode

LOG = structlog.get_logger(__name__)

ACTION_LOG_QUEUE_MAX_ENTRIES = 256
ACTION_LOG_QUEUE_MAX_BYTES = 1024 * 1024
ACTION_LOG_HTTP_TIMEOUT_SECONDS = 2
ACTION_LOG_SHUTDOWN_TIMEOUT_SECONDS = 2.5
ACTION_LOG_CLIENT_MAX_EVENTS_PER_BATCH = 10
ACTION_LOG_DROP_LOG_INTERVAL_SECONDS = 30.0
ACTION_LOG_SESSION_INDEX_CAP = 1_024
_RESOURCE_NOT_FOUND_CODE = "browser_session_not_found"


@dataclass(frozen=True, slots=True)
class ActionLogQueueEntry:
    browser_session_id: str
    event: ActionLogEvent
    principal: Skyvern
    origin: str
    encoded_bytes: int


class ActionLogWorker:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[ActionLogQueueEntry] = asyncio.Queue(maxsize=ACTION_LOG_QUEUE_MAX_ENTRIES)
        self._task: asyncio.Task[None] | None = None
        self._queued_bytes = 0
        self._unsupported_origins: set[str] = set()
        self._last_drop_log_at = 0.0
        self.drop_count = 0

    def enqueue(self, entry: ActionLogQueueEntry) -> None:
        if entry.origin in self._unsupported_origins:
            self._drop("route_unsupported")
            return
        if entry.encoded_bytes > ACTION_LOG_MAX_BODY_BYTES or (
            self._queued_bytes + entry.encoded_bytes > ACTION_LOG_QUEUE_MAX_BYTES
        ):
            self._drop("queue_bytes")
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._drop("queue_entries")
            return
        self._queued_bytes += entry.encoded_bytes
        self._ensure_task()

    def capture_drop(self, reason: str) -> None:
        self._drop(reason)

    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="skyvern-action-log")
        except RuntimeError:
            self._drop("no_event_loop")
            return
        self._task.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._drop("worker_failure")
            LOG.debug("Action-log worker stopped", exception_type=type(exc).__name__)

    async def _run(self) -> None:
        pending: deque[ActionLogQueueEntry] = deque()
        while True:
            if pending:
                entry = pending.popleft()
            else:
                entry = await self._queue.get()
                self._queued_bytes -= entry.encoded_bytes
            batch = [entry]
            while len(batch) < ACTION_LOG_CLIENT_MAX_EVENTS_PER_BATCH:
                try:
                    candidate = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queued_bytes -= candidate.encoded_bytes
                same_target = (
                    candidate.browser_session_id == entry.browser_session_id
                    and candidate.origin == entry.origin
                    and candidate.principal is entry.principal
                )
                candidate_batch = [item.event for item in (*batch, candidate)]
                encoded_batch = ActionLogBatchRequest(events=candidate_batch).model_dump_json().encode()
                if not same_target or len(encoded_batch) > ACTION_LOG_MAX_BODY_BYTES:
                    pending.append(candidate)
                    break
                batch.append(candidate)

            try:
                await self._send_batch(batch)
            except Exception as exc:
                self._drop("transport_error", count=len(batch))
                LOG.debug("Action-log batch dropped", exception_type=type(exc).__name__)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _send_batch(self, batch: list[ActionLogQueueEntry]) -> None:
        first = batch[0]
        payload = ActionLogBatchRequest(events=[entry.event for entry in batch])
        request_options: RequestOptions = {
            "timeout_in_seconds": ACTION_LOG_HTTP_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
        response = await first.principal._client_wrapper.httpx_client.request(
            f"v1/browser_sessions/{first.browser_session_id}/action_logs",
            method="POST",
            base_url=first.origin,
            json=payload.model_dump(mode="json"),
            retries=0,
            request_options=request_options,
        )
        if response.status_code == 404:
            if self._is_resource_missing(response):
                self._drop("session_missing", count=len(batch))
            else:
                self._unsupported_origins.add(first.origin)
                self._drop("route_missing", count=len(batch))
            return
        if response.status_code >= 400:
            self._drop("http_error", count=len(batch))

    @staticmethod
    def _is_resource_missing(response: httpx.Response) -> bool:
        try:
            detail = response.json().get("detail")
        except Exception:
            return False
        return isinstance(detail, dict) and detail.get("code") == _RESOURCE_NOT_FOUND_CODE

    async def drain(self) -> None:
        if self._task is None:
            return
        await asyncio.wait_for(self._queue.join(), timeout=ACTION_LOG_SHUTDOWN_TIMEOUT_SECONDS)

    async def _shutdown_on_worker_loop(self) -> None:
        task = self._task
        if task is None:
            return
        try:
            try:
                await self.drain()
            except TimeoutError:
                self._drop("shutdown_timeout")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None

    async def shutdown(self) -> None:
        task = self._task
        if task is None:
            return
        if task.get_loop() is not asyncio.get_running_loop():
            self._drop("shutdown_loop_unavailable")
            self._task = None
            return
        await self._shutdown_on_worker_loop()

    def _drop(self, reason: str, *, count: int = 1) -> None:
        self.drop_count += count
        now = time.monotonic()
        if now - self._last_drop_log_at < ACTION_LOG_DROP_LOG_INTERVAL_SECONDS:
            return
        self._last_drop_log_at = now
        LOG.warning("Action-log events dropped", reason=reason, dropped=self.drop_count)


action_log_worker = ActionLogWorker()
_event_indexes: OrderedDict[str, int] = OrderedDict()


def _next_event_index(browser_session_id: str) -> int:
    index = _event_indexes.pop(browser_session_id, 0)
    _event_indexes[browser_session_id] = index + 1
    if len(_event_indexes) > ACTION_LOG_SESSION_INDEX_CAP:
        _event_indexes.popitem(last=False)
    return index


def enqueue_action_event(
    browser_context: BrowserContext,
    *,
    tool: str,
    timing_ms: dict[str, int],
    ok: bool,
    selector: str | None = None,
    typed_text: str | None = None,
    value: str | None = None,
    key: str | None = None,
    source_url: str | None = None,
    error_code: str | None = None,
) -> None:
    if browser_context.mode != "cloud_session" or not browser_context.session_id:
        return
    try:
        if get_active_api_key() is None:
            return
        principal = get_skyvern()
        origin = _resolve_self_base_url() if is_stateless_http_mode() else principal._client_wrapper.get_base_url()
        event = project_action_event(
            event_id=uuid4(),
            tool=tool,
            selector=selector,
            typed_text=typed_text,
            value=value,
            key=key,
            source_url=source_url,
            occurred_at=datetime.now(timezone.utc),
            timing_ms=timing_ms,
            outcome=ActionLogOutcome.SUCCESS if ok else ActionLogOutcome.ERROR,
            error_code=None if ok else (error_code or "ACTION_FAILED"),
            index=_next_event_index(browser_context.session_id),
        )
        encoded_bytes = len(event.model_dump_json().encode())
        action_log_worker.enqueue(
            ActionLogQueueEntry(
                browser_session_id=browser_context.session_id,
                event=event,
                principal=principal,
                origin=origin,
                encoded_bytes=encoded_bytes,
            )
        )
    except Exception as exc:
        action_log_worker.capture_drop("projection_error")
        LOG.debug("Action-log capture dropped", exception_type=type(exc).__name__)


async def drain_action_log_events() -> None:
    try:
        await action_log_worker.drain()
    except Exception:
        action_log_worker.capture_drop("drain_error")


async def shutdown_action_log_worker() -> None:
    try:
        await action_log_worker.shutdown()
    except Exception:
        action_log_worker.capture_drop("shutdown_error")
    finally:
        _event_indexes.clear()
