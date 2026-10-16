from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from skyvern.cli.mcp_tools._localhost import is_localhost_url
from skyvern.config import settings
from skyvern.exceptions import BlockedHost, SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.cache.base import BaseCache
from skyvern.forge.sdk.copilot.config import ALL_BLOCK_FAMILIES, AuthoringCapability
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_pause import apply_connected_credential_to_policy
from skyvern.forge.sdk.copilot.credential_resolution import safe_admitted_url
from skyvern.forge.sdk.copilot.demonstration_tap import DEMONSTRATION_MAX_EVENTS, PageTap, TapEvent
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    ScoutedInteraction,
    record_attached_browser_driver,
    resolve_persistent_browser_state,
    start_browser_driver_release,
    wait_for_driver_releases,
)
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.tools.credential_fill import fill_credential_field
from skyvern.forge.sdk.core.event_source_stream import EventSourceStream
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotDemonstrationControlRequest
from skyvern.utils.url_validators import validate_fetch_url

LOG = structlog.get_logger()
_SHIELDED_TASKS: set[asyncio.Task[Any]] = set()


async def finish_shielded(coro: Coroutine[Any, Any, Any]) -> Any:
    # The loop keeps only weak task references, so a shielded task outliving its cancelled caller needs a strong one.
    task = asyncio.ensure_future(coro)
    _SHIELDED_TASKS.add(task)
    task.add_done_callback(_SHIELDED_TASKS.discard)
    return await asyncio.shield(task)


DEMONSTRATION_TTL = timedelta(hours=2)
DEMONSTRATION_CANCELLED_TTL = timedelta(seconds=60)
DEMONSTRATION_MAX_CONTROLS = 200
DEMONSTRATION_MAX_VALUE_CHARS = 100_000
DEMONSTRATION_POLL_SECONDS = 0.25
DEMONSTRATION_OWNER_REFRESH_SECONDS = 5
DEMONSTRATION_OWNER_LIVE_SECONDS = 20
DEMONSTRATION_MAX_SECONDS = 30 * 60
DEMONSTRATION_REPLAY_LEASE = timedelta(minutes=5)

DemonstrationStatus = Literal["recording", "stopping", "stopped", "replaying", "consumed", "cancelled"]
DemonstrationStopReason = Literal[
    "stopped", "cancelled", "disconnected", "browser_gone", "time_limit", "event_limit", "consumed"
]


@dataclass
class DemonstrationRecord:
    workflow_permanent_id: str
    browser_session_id: str
    status: DemonstrationStatus
    event_count: int
    owner: str | None
    owner_seen_at: datetime | None
    final_url: str | None
    stop_reason: DemonstrationStopReason | None = None
    insertion: dict[str, str | None] | None = None
    replay_started_at: datetime | None = None
    replay_token: str | None = None
    recording_started_at: datetime | None = None
    value_chars: int = 0


@dataclass(frozen=True)
class DemonstrationClaim:
    demonstration_id: str
    owner: str
    resumed: bool
    control_cursor: int


class DemonstrationRejection(Exception):
    def __init__(self, status_code: HTTPStatus, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _NoopStream:
    async def send(self, data: Any) -> bool:
        return True

    async def is_disconnected(self) -> bool:
        return False

    async def close(self) -> None:
        return None


def demonstration_record_key(organization_id: str, demonstration_id: str) -> str:
    return f"copilot_demonstration:{organization_id}:{demonstration_id}"


def demonstration_event_key(organization_id: str, demonstration_id: str, seq: int) -> str:
    return f"copilot_demonstration_event:{organization_id}:{demonstration_id}:{seq}"


def demonstration_controls_key(organization_id: str, demonstration_id: str) -> str:
    return f"copilot_demonstration_controls:{organization_id}:{demonstration_id}"


def demonstration_lock_key(organization_id: str, demonstration_id: str) -> str:
    return f"copilot_demonstration_lock:{organization_id}:{demonstration_id}"


def demonstration_browser_key(organization_id: str, browser_session_id: str) -> str:
    return f"copilot_demonstration_browser:{organization_id}:{browser_session_id}"


def encode_demonstration_record(record: DemonstrationRecord) -> str:
    return json.dumps(
        {
            "workflow_permanent_id": record.workflow_permanent_id,
            "browser_session_id": record.browser_session_id,
            "status": record.status,
            "event_count": record.event_count,
            "owner": record.owner,
            "owner_seen_at": record.owner_seen_at.isoformat() if record.owner_seen_at else None,
            "final_url": record.final_url,
            "stop_reason": record.stop_reason,
            "insertion": record.insertion,
            "replay_started_at": record.replay_started_at.isoformat() if record.replay_started_at else None,
            "replay_token": record.replay_token,
            "recording_started_at": record.recording_started_at.isoformat() if record.recording_started_at else None,
            "value_chars": record.value_chars,
        },
        separators=(",", ":"),
    )


def decode_demonstration_record(raw: Any) -> DemonstrationRecord | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    workflow_permanent_id = data.get("workflow_permanent_id")
    browser_session_id = data.get("browser_session_id")
    record_status = data.get("status")
    event_count = data.get("event_count")
    if (
        not isinstance(workflow_permanent_id, str)
        or not isinstance(browser_session_id, str)
        or record_status not in {"recording", "stopping", "stopped", "replaying", "consumed", "cancelled"}
        or not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or event_count < 0
    ):
        return None
    owner = data.get("owner")
    final_url = data.get("final_url")
    owner_seen_at = _decode_timestamp(data.get("owner_seen_at"))
    replay_started_at = _decode_timestamp(data.get("replay_started_at"))
    return DemonstrationRecord(
        workflow_permanent_id=workflow_permanent_id,
        browser_session_id=browser_session_id,
        status=record_status,
        event_count=event_count,
        owner=owner if isinstance(owner, str) else None,
        owner_seen_at=owner_seen_at,
        final_url=final_url if isinstance(final_url, str) else None,
        value_chars=data["value_chars"] if isinstance(data.get("value_chars"), int) else 0,
        stop_reason=(
            data.get("stop_reason")
            if data.get("stop_reason")
            in {"stopped", "cancelled", "disconnected", "browser_gone", "time_limit", "event_limit", "consumed"}
            else None
        ),
        insertion=(
            {
                key: value
                for key, value in data["insertion"].items()
                if key in {"after_label", "before_label"} and (isinstance(value, str) or value is None)
            }
            if isinstance(data.get("insertion"), dict)
            else None
        ),
        replay_started_at=replay_started_at,
        replay_token=data.get("replay_token") if isinstance(data.get("replay_token"), str) else None,
        recording_started_at=_decode_timestamp(data.get("recording_started_at")),
    )


def _decode_timestamp(raw: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return None
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def demonstration_time_limit_reached(record: DemonstrationRecord, now: datetime | None = None) -> bool:
    started_at = record.recording_started_at
    return started_at is not None and (now or datetime.now(UTC)) - started_at >= timedelta(
        seconds=DEMONSTRATION_MAX_SECONDS
    )


def decode_demonstration_controls(raw: Any) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def demonstration_owner_is_live(record: DemonstrationRecord, now: datetime | None = None) -> bool:
    if record.owner is None or record.owner_seen_at is None:
        return False
    return (now or datetime.now(UTC)) - record.owner_seen_at < timedelta(seconds=DEMONSTRATION_OWNER_LIVE_SECONDS)


def demonstration_limit_reached(record: DemonstrationRecord) -> bool:
    return record.event_count >= DEMONSTRATION_MAX_EVENTS or record.value_chars >= DEMONSTRATION_MAX_VALUE_CHARS


def settle_abandoned_demonstration(record: DemonstrationRecord, now: datetime | None = None) -> bool:
    if record.status in {"recording", "stopping"} and not demonstration_owner_is_live(record, now):
        # A user stop whose owner died before the final drain may be missing buffered events, so replay must say so.
        if record.status == "recording" or record.stop_reason in {None, "stopped"}:
            record.stop_reason = "disconnected"
        record.status = "cancelled" if record.stop_reason == "cancelled" else "stopped"
        record.owner = None
        record.owner_seen_at = None
        return True
    if record.status != "replaying" or record.replay_started_at is None:
        return False
    if (now or datetime.now(UTC)) - record.replay_started_at < DEMONSTRATION_REPLAY_LEASE:
        return False
    record.status = "stopped"
    record.replay_started_at = None
    record.replay_token = None
    return True


async def load_demonstration_record(
    cache: BaseCache, organization_id: str, demonstration_id: str
) -> DemonstrationRecord | None:
    return decode_demonstration_record(await cache.get(demonstration_record_key(organization_id, demonstration_id)))


async def expire_demonstration_payload(
    cache: BaseCache, organization_id: str, demonstration_id: str, event_count: int, ttl: timedelta
) -> None:
    keys = [demonstration_event_key(organization_id, demonstration_id, seq) for seq in range(1, event_count + 1)]
    keys.append(demonstration_controls_key(organization_id, demonstration_id))
    values = await asyncio.gather(*(cache.get(key) for key in keys))
    await asyncio.gather(
        *(cache.set(key, value, ex=ttl) for key, value in zip(keys, values, strict=True) if value is not None)
    )


async def store_settled_demonstration(
    cache: BaseCache, organization_id: str, demonstration_id: str, record: DemonstrationRecord
) -> None:
    if not settle_abandoned_demonstration(record):
        return
    ttl = DEMONSTRATION_CANCELLED_TTL if record.status == "cancelled" else DEMONSTRATION_TTL
    await cache.set(
        demonstration_record_key(organization_id, demonstration_id), encode_demonstration_record(record), ex=ttl
    )
    if record.status == "cancelled":
        await expire_demonstration_payload(cache, organization_id, demonstration_id, record.event_count, ttl)


async def claim_demonstration_recording(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_permanent_id: str,
    browser_session_id: str,
    demonstration_id: str | None,
    insertion: dict[str, str | None] | None = None,
) -> DemonstrationClaim:
    browser_key = demonstration_browser_key(organization_id, browser_session_id)
    replaced: tuple[str, int] | None = None
    # A Redis lock stores its token under its own name, so it cannot share the index key it guards.
    async with cache.get_lock(f"{browser_key}:lock"):
        indexed_id = await cache.get(browser_key)
        if isinstance(indexed_id, bytes):
            indexed_id = indexed_id.decode()
        if demonstration_id is not None:
            if isinstance(indexed_id, str) and indexed_id and indexed_id != demonstration_id:
                async with cache.get_lock(demonstration_lock_key(organization_id, indexed_id)):
                    indexed = await load_demonstration_record(cache, organization_id, indexed_id)
                    if (
                        indexed is not None
                        and indexed.status in {"recording", "stopping"}
                        and demonstration_owner_is_live(indexed)
                    ):
                        raise DemonstrationRejection(
                            HTTPStatus.CONFLICT, "Another demonstration is recording on this browser"
                        )
            async with cache.get_lock(demonstration_lock_key(organization_id, demonstration_id)):
                record = await load_demonstration_record(cache, organization_id, demonstration_id)
                if record is None or (
                    record.workflow_permanent_id != workflow_permanent_id
                    or record.browser_session_id != browser_session_id
                ):
                    raise DemonstrationRejection(HTTPStatus.NOT_FOUND, "Unknown demonstration")
                if record.status not in {"recording", "stopped"}:
                    raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration cannot be resumed")
                if demonstration_limit_reached(record):
                    raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration has reached the event limit")
                if demonstration_time_limit_reached(record):
                    raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration has reached its time limit")
                if demonstration_owner_is_live(record):
                    raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration is already recording")
                controls = decode_demonstration_controls(
                    await cache.get(demonstration_controls_key(organization_id, demonstration_id))
                )
                owner = secrets.token_urlsafe(24)
                record.status = "recording"
                record.owner = owner
                record.owner_seen_at = datetime.now(UTC)
                record.stop_reason = None
                record.final_url = None
                await cache.set(
                    demonstration_record_key(organization_id, demonstration_id),
                    encode_demonstration_record(record),
                    ex=DEMONSTRATION_TTL,
                )
            await cache.set(browser_key, demonstration_id, ex=DEMONSTRATION_TTL)
            return DemonstrationClaim(
                demonstration_id=demonstration_id,
                owner=owner,
                resumed=True,
                control_cursor=len(controls),
            )

        if isinstance(indexed_id, str) and indexed_id:
            async with cache.get_lock(demonstration_lock_key(organization_id, indexed_id)):
                indexed = await load_demonstration_record(cache, organization_id, indexed_id)
                if (
                    indexed is not None
                    and indexed.status in {"recording", "stopping"}
                    and demonstration_owner_is_live(indexed)
                ):
                    raise DemonstrationRejection(
                        HTTPStatus.CONFLICT, "Another demonstration is recording on this browser"
                    )
                if indexed is not None and indexed.status in {"recording", "stopping", "stopped"}:
                    indexed.status = "cancelled"
                    indexed.owner = None
                    indexed.owner_seen_at = None
                    await cache.set(
                        demonstration_record_key(organization_id, indexed_id),
                        encode_demonstration_record(indexed),
                        ex=DEMONSTRATION_CANCELLED_TTL,
                    )
                    replaced = (indexed_id, indexed.event_count)

        demonstration_id = secrets.token_urlsafe(24)
        owner = secrets.token_urlsafe(24)
        record = DemonstrationRecord(
            workflow_permanent_id=workflow_permanent_id,
            browser_session_id=browser_session_id,
            status="recording",
            event_count=0,
            owner=owner,
            owner_seen_at=datetime.now(UTC),
            final_url=None,
            insertion=insertion,
            recording_started_at=datetime.now(UTC),
        )
        await cache.set(demonstration_controls_key(organization_id, demonstration_id), "[]", ex=DEMONSTRATION_TTL)
        await cache.set(
            demonstration_record_key(organization_id, demonstration_id),
            encode_demonstration_record(record),
            ex=DEMONSTRATION_TTL,
        )
        await cache.set(browser_key, demonstration_id, ex=DEMONSTRATION_TTL)
    if replaced is not None:
        await expire_demonstration_payload(
            cache, organization_id, replaced[0], replaced[1], DEMONSTRATION_CANCELLED_TTL
        )
    return DemonstrationClaim(
        demonstration_id=demonstration_id,
        owner=owner,
        resumed=False,
        control_cursor=0,
    )


async def append_demonstration_control(
    cache: BaseCache,
    *,
    organization_id: str,
    request: WorkflowCopilotDemonstrationControlRequest,
) -> int:
    record_key = demonstration_record_key(organization_id, request.demonstration_id)
    controls_key = demonstration_controls_key(organization_id, request.demonstration_id)
    async with cache.get_lock(demonstration_lock_key(organization_id, request.demonstration_id)):
        record = decode_demonstration_record(await cache.get(record_key))
        if record is None:
            raise DemonstrationRejection(HTTPStatus.NOT_FOUND, "Unknown demonstration")
        if record.status not in {"recording", "stopping", "stopped"}:
            raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration has ended")
        if request.action == "stop":
            if record.status == "recording":
                record.status = "stopping"
                record.stop_reason = "stopped"
                await cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)
            return 0
        if request.action == "cancel":
            if demonstration_owner_is_live(record):
                # The recorder commits the terminal state itself, so a stream dropped with the cancel still discards.
                record.status = "stopping"
                record.stop_reason = "cancelled"
                await cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)
                return 0
            record.status = "cancelled"
            record.stop_reason = "cancelled"
            record.owner = None
            record.owner_seen_at = None
            await cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_CANCELLED_TTL)
            discarded_events = record.event_count
        else:
            controls = decode_demonstration_controls(await cache.get(controls_key))
            if len(controls) >= DEMONSTRATION_MAX_CONTROLS:
                raise DemonstrationRejection(HTTPStatus.CONFLICT, "Too many browser controls for this demonstration")
            seq = len(controls) + 1
            entry = {"seq": seq, **request.model_dump(exclude_none=True, exclude={"demonstration_id"})}
            controls.append(entry)
            await cache.set(controls_key, json.dumps(controls, separators=(",", ":")), ex=DEMONSTRATION_TTL)
            return seq
    await expire_demonstration_payload(
        cache, organization_id, request.demonstration_id, discarded_events, DEMONSTRATION_CANCELLED_TTL
    )
    return 0


async def active_demonstration(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_permanent_id: str,
    browser_session_id: str,
) -> tuple[str, DemonstrationRecord] | None:
    demonstration_id = await cache.get(demonstration_browser_key(organization_id, browser_session_id))
    if isinstance(demonstration_id, bytes):
        demonstration_id = demonstration_id.decode()
    if not isinstance(demonstration_id, str) or not demonstration_id:
        return None
    record_key = demonstration_record_key(organization_id, demonstration_id)
    async with cache.get_lock(demonstration_lock_key(organization_id, demonstration_id)):
        record = decode_demonstration_record(await cache.get(record_key))
        if record is not None:
            await store_settled_demonstration(cache, organization_id, demonstration_id, record)
    if (
        record is None
        or record.workflow_permanent_id != workflow_permanent_id
        or record.browser_session_id != browser_session_id
        or record.status not in {"recording", "stopping", "stopped"}
    ):
        return None
    return demonstration_id, record


async def validate_demonstration_for_replay(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_permanent_id: str,
    demonstration_id: str,
) -> DemonstrationRecord:
    record_key = demonstration_record_key(organization_id, demonstration_id)
    async with cache.get_lock(demonstration_lock_key(organization_id, demonstration_id)):
        record = decode_demonstration_record(await cache.get(record_key))
        if record is None or record.workflow_permanent_id != workflow_permanent_id:
            raise DemonstrationRejection(HTTPStatus.NOT_FOUND, "Unknown demonstration")
        await store_settled_demonstration(cache, organization_id, demonstration_id, record)
        if record.status in {"recording", "stopping"}:
            raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration is still recording")
        if record.status != "stopped":
            raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration has already ended")
        return record


async def demonstration_action_labels(
    cache: BaseCache, organization_id: str, demonstration_id: str, event_count: int
) -> list[str]:
    labels: list[str] = []
    events = await asyncio.gather(
        *(
            cache.get(demonstration_event_key(organization_id, demonstration_id, seq))
            for seq in range(1, event_count + 1)
        )
    )
    for raw in events:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("kind") == "type":
            packet = event.get("packet")
            role_name = packet.get("role_name") if isinstance(packet, dict) else None
            accessible_name = role_name.get("accessible_name") if isinstance(role_name, dict) else None
            target = safe_label(accessible_name) or safe_label(str(event.get("selector") or "field"))
            labels.append(f'Type into "{target}"'[:60])
            continue
        label = event.get("label") if isinstance(event, dict) else None
        if isinstance(label, str) and label:
            labels.append(label)
    return labels


class DemonstrationRecorder:
    def __init__(
        self,
        *,
        cache: BaseCache,
        organization_id: str,
        workflow_permanent_id: str,
        browser_session_id: str,
        claim: DemonstrationClaim,
        stream: EventSourceStream,
        api_key: str | None = None,
        authoring_capability: AuthoringCapability = ALL_BLOCK_FAMILIES,
    ) -> None:
        self.cache = cache
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.browser_session_id = browser_session_id
        self.demonstration_id = claim.demonstration_id
        self.owner = claim.owner
        self.resumed = claim.resumed
        self.control_cursor = claim.control_cursor
        self.stream = stream
        self.tap: PageTap | None = None
        self.page: Page | None = None
        self.sensitive_count = 0
        self.event_limit_reached = False
        self.credential_filled_selectors: set[str] = set()
        self.started_at = time.monotonic()
        self.recording_ctx = CopilotContext(
            organization_id=organization_id,
            workflow_id="",
            workflow_permanent_id=workflow_permanent_id,
            workflow_yaml=None,
            browser_session_id=browser_session_id,
            stream=_NoopStream(),
            request_policy=RequestPolicy(),
            api_key=api_key,
        )
        self.recording_ctx.authoring_capability = authoring_capability

    async def run(self) -> None:
        record = await load_demonstration_record(self.cache, self.organization_id, self.demonstration_id)
        action_count = record.event_count if record is not None else 0
        LOG.info("copilot_demonstration_started", resumed=self.resumed)
        reason: DemonstrationStopReason = "browser_gone"
        next_refresh = time.monotonic() + DEMONSTRATION_OWNER_REFRESH_SECONDS
        try:
            browser_state = await resolve_persistent_browser_state(
                session_id=self.browser_session_id,
                organization_id=self.organization_id,
            )
            if browser_state is None or browser_state.browser_context is None:
                return
            record_attached_browser_driver(self.recording_ctx, self.browser_session_id, browser_state)
            browser_context = browser_state.browser_context
            pages = list(browser_context.pages)
            self.page = await browser_state.get_working_page(prune_excess_pages=False)
            if self.page is None and pages:
                self.page = pages[-1]
            self.tap = PageTap(browser_context, max_events=DEMONSTRATION_MAX_EVENTS)
            await self.tap.start(pages)
            with contextlib.suppress(Exception):
                await self.tap.set_intercept_file_chooser_dialog(True)
            await self.stream.send(
                {
                    "type": "demonstration_started",
                    "demonstration_id": self.demonstration_id,
                    "browser_session_id": self.browser_session_id,
                    "action_count": action_count,
                    "resumed": self.resumed,
                    "can_fill_credentials": self.recording_ctx.authoring_capability.code_blocks,
                }
            )
            while True:
                record = await load_demonstration_record(self.cache, self.organization_id, self.demonstration_id)
                if record is None or record.owner != self.owner:
                    reason = (
                        record.stop_reason if record is not None and record.stop_reason is not None else "cancelled"
                    )
                    break
                if record.status in {"stopping", "stopped"}:
                    reason = await self._run_controls() or record.stop_reason or "stopped"
                    break
                if record.status == "consumed":
                    reason = "consumed"
                    break
                if record.status == "cancelled":
                    reason = "cancelled"
                    break
                if await self.stream.is_disconnected():
                    reason = record.stop_reason or "disconnected"
                    break
                working_pages = [page for page in browser_context.pages if not page.is_closed()]
                if self.page is None or self.page.is_closed():
                    self.page = working_pages[-1] if working_pages else None
                if self.page is None:
                    reason = "browser_gone"
                    break
                if demonstration_time_limit_reached(record):
                    reason = "time_limit"
                    break
                stop_reason = await self._drain_tap()
                if stop_reason is not None:
                    reason = stop_reason
                    break
                stop_reason = await self._run_controls()
                if stop_reason is not None:
                    reason = stop_reason
                    break
                if time.monotonic() >= next_refresh:
                    await self._refresh_owner()
                    next_refresh = time.monotonic() + DEMONSTRATION_OWNER_REFRESH_SECONDS
                await asyncio.sleep(DEMONSTRATION_POLL_SECONDS)
        finally:
            await finish_shielded(self._stop(reason))

    async def _append_event(self, event: dict[str, Any]) -> int | None:
        record_key = demonstration_record_key(self.organization_id, self.demonstration_id)
        async with self.cache.get_lock(demonstration_lock_key(self.organization_id, self.demonstration_id)):
            record = decode_demonstration_record(await self.cache.get(record_key))
            if record is None or record.owner != self.owner or record.status not in {"recording", "stopping"}:
                return None
            if demonstration_limit_reached(record):
                self.event_limit_reached = True
                if record.status == "recording":
                    record.status = "stopping"
                    record.stop_reason = "event_limit"
                    await self.cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)
                return None
            seq = record.event_count + 1
            stored = {"seq": seq, **event}
            await self.cache.set(
                demonstration_event_key(self.organization_id, self.demonstration_id, seq),
                json.dumps(stored, separators=(",", ":")),
                ex=DEMONSTRATION_TTL,
            )
            record.event_count = seq
            value = event.get("value")
            record.value_chars += len(value) if isinstance(value, str) else 0
            if demonstration_limit_reached(record):
                self.event_limit_reached = True
                if record.status == "recording":
                    record.status = "stopping"
                    record.stop_reason = "event_limit"
            await self.cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)
            return seq

    async def _drain_tap(self) -> DemonstrationStopReason | None:
        if self.tap is None:
            return None
        for event in self.tap.drain_events():
            await self._record_tap_event(event)
            if self.event_limit_reached:
                return "event_limit"
        return "event_limit" if self.tap.event_limit_reached else None

    async def _record_tap_event(self, event: TapEvent) -> None:
        self.page = event.page
        payload = event.payload
        frame: dict[str, Any] = {"is_main": event.is_main_frame, "name": event.frame_name}
        if not event.is_main_frame:
            path = await self.tap.frame_path(event) if self.tap is not None else None
            if path is None or len(path) > 1:
                LOG.info("copilot_demonstration_nested_frame_unsupported")
                await self._record_gap_event(event, frame, "nested_frame")
                return
            frame["path"] = path
        kind = payload.get("kind")
        selector = payload.get("selector")
        if kind == "secret_focus":
            field = payload.get("field")
            await self.stream.send(
                {
                    "type": "demonstration_sensitive_field",
                    "field": field if field in {"password", "totp", "other"} else "other",
                    "selector": selector if isinstance(selector, str) else "",
                    "url": redact_raw_secrets_for_prompt(
                        safe_admitted_url(str(payload.get("url") or event.page.url or ""))
                    ),
                }
            )
            return
        event_kind = "secret" if kind in {"secret", "secret_select"} else kind
        if event_kind not in {"click", "type", "select", "key", "navigate", "secret"}:
            gap_reason = (
                "file_chooser" if kind in {"file_click", "upload"} else f"unsupported_event:{kind or 'unknown'}"
            )
            await self._record_gap_event(event, frame, gap_reason)
            return
        source_url = str(payload.get("url") or "") if event.is_main_frame else str(event.page.url or "")
        if event_kind == "navigate":
            if not event.is_main_frame:
                await self._record_gap_event(event, frame, "frame_navigation")
                return
            source_url = str(payload.get("source_url") or "")
        stored: dict[str, Any] = {
            "ts": payload.get("ts"),
            "kind": event_kind,
            "source_url": source_url,
            "frame": frame,
        }
        for key in ("selector", "packet", "value", "key", "url", "field", "field_label", "label"):
            value = payload.get(key)
            if value is not None and not (event_kind == "secret" and key == "value"):
                stored[key] = value
        if event_kind == "secret":
            if isinstance(selector, str) and selector in self.credential_filled_selectors:
                return
            self.sensitive_count += 1
            if kind == "secret_select":
                stored["control"] = "select"
                stored["label"] = "Select a sensitive option"
            seq = await self._append_event(stored)
            if seq is not None and kind == "secret_select":
                await self.stream.send(
                    {"type": "demonstration_action", "seq": seq, "label": stored["label"], "tool_name": "select_option"}
                )
            return
        label = _event_label(stored)
        stored["label"] = label
        seq = await self._append_event(stored)
        if seq is not None:
            await self.stream.send(
                {
                    "type": "demonstration_action",
                    "seq": seq,
                    "label": label,
                    "tool_name": {
                        "click": "click",
                        "type": "type_text",
                        "select": "select_option",
                        "key": "press_key",
                        "navigate": "navigate_browser",
                    }[event_kind],
                }
            )

    async def _record_gap_event(self, event: TapEvent, frame: dict[str, Any], reason: str) -> None:
        stored = {
            "ts": event.payload.get("ts"),
            "kind": "gap",
            "reason": reason,
            "source_url": (str(event.payload.get("url") or "") if event.is_main_frame else str(event.page.url or "")),
            "frame": frame,
            "label": "Step not recorded",
        }
        seq = await self._append_event(stored)
        if seq is not None:
            await self.stream.send(
                {"type": "demonstration_action", "seq": seq, "label": stored["label"], "tool_name": "gap"}
            )

    async def _run_controls(self) -> DemonstrationStopReason | None:
        controls = decode_demonstration_controls(
            await self.cache.get(demonstration_controls_key(self.organization_id, self.demonstration_id))
        )
        for entry in controls[self.control_cursor :]:
            self.control_cursor += 1
            action = entry.get("action")
            if action == "cancel":
                return "cancelled"
            if action == "navigate":
                await self._run_control_with_owner_heartbeat(self._navigate(entry))
            elif action == "fill_credential":
                await self._run_control_with_owner_heartbeat(self._fill_credential(entry))
        return None

    async def _run_control_with_owner_heartbeat(self, control: Awaitable[None]) -> None:
        owner_heartbeat = asyncio.create_task(self._owner_heartbeat())
        try:
            await control
        finally:
            owner_heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner_heartbeat

    async def _owner_heartbeat(self) -> None:
        while True:
            await self._refresh_owner()
            await asyncio.sleep(DEMONSTRATION_OWNER_REFRESH_SECONDS)

    async def _navigate(self, entry: dict[str, Any]) -> None:
        url = entry.get("url")
        if not isinstance(url, str) or self.page is None:
            return
        try:
            validated_url = await asyncio.to_thread(validate_fetch_url, url)
        except BlockedHost:
            # Same rule as skyvern_navigate for a cloud session: localhost only in local development.
            if not (settings.ENV == "local" and is_localhost_url(url)):
                await self._notice("That URL cannot be opened.", level="error")
                return
            validated_url = url
        except SkyvernHTTPException:
            await self._notice("That URL cannot be opened.", level="error")
            return
        source_url = str(self.page.url or "")
        if self.tap is not None:
            self.tap.begin_control_navigation()
        try:
            await self.page.goto(validated_url)
        except PlaywrightError:
            await self._notice("The page could not be opened.", level="error")
            return
        finally:
            if self.tap is not None:
                self.tap.end_control_navigation()
        event = {
            "ts": datetime.now(UTC).timestamp() * 1000,
            "kind": "navigate",
            "url": url,
            "source_url": source_url,
            "frame": {"is_main": True, "name": ""},
        }
        event["label"] = _event_label(event)
        seq = await self._append_event(event)
        if seq is not None:
            await self.stream.send(
                {
                    "type": "demonstration_action",
                    "seq": seq,
                    "label": event["label"],
                    "tool_name": "navigate_browser",
                }
            )

    async def _fill_credential(self, entry: dict[str, Any]) -> None:
        credential_id = entry.get("credential_id")
        selector = entry.get("selector")
        field = entry.get("field")
        if (
            not isinstance(credential_id, str)
            or not isinstance(selector, str)
            or field not in {"password", "totp"}
            or self.page is None
        ):
            return
        credentials = await app.DATABASE.credentials.get_credentials_by_ids(
            [credential_id], organization_id=self.organization_id
        )
        if not credentials:
            await self._notice("The saved credential is no longer available.", level="error")
            return
        credential = credentials[0]
        policy = self.recording_ctx.request_policy
        if not isinstance(policy, RequestPolicy):
            return
        source_url = str(self.page.url or "")
        policy.credential_ask_login_page_urls = [credential.tested_url or source_url]
        apply_connected_credential_to_policy(self.recording_ctx, policy, credential)
        before = len(self.recording_ctx.scout_trajectory)
        self.credential_filled_selectors.add(selector)
        try:
            result = await fill_credential_field(self.recording_ctx, selector, credential_id, field)
        finally:
            if self.tap is not None:
                with contextlib.suppress(Exception):
                    await self.tap.flush()
                    await self._drain_tap()
            self.credential_filled_selectors.discard(selector)
        scrubbed = scrub_secrets_from_structure(self.recording_ctx, result)
        if not isinstance(scrubbed, dict) or scrubbed.get("ok") is not True:
            await self._notice("The saved credential could not be filled.", level="error")
            return
        scout_fact: ScoutedInteraction | None = None
        if len(self.recording_ctx.scout_trajectory) > before:
            scout_fact = cast(ScoutedInteraction, dict(self.recording_ctx.scout_trajectory[-1]))
            scout_fact["actor"] = "user"
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).timestamp() * 1000,
            "kind": "credential_fill",
            "selector": selector,
            "source_url": source_url,
            "frame": {"is_main": True, "name": ""},
            "credential_id": credential_id,
            "credential_name": credential.name,
            "field": field,
            "result": scrubbed,
            "label": f'Fill {field} from "{safe_label(credential.name)}"'[:60],
        }
        if scout_fact is not None:
            event["scout_fact"] = scout_fact
        seq = await self._append_event(event)
        if seq is not None:
            await self.stream.send(
                {
                    "type": "demonstration_action",
                    "seq": seq,
                    "label": event["label"],
                    "tool_name": "fill_credential_field",
                }
            )

    async def _notice(self, message: str, *, level: Literal["info", "error"] = "info") -> None:
        await self.stream.send({"type": "demonstration_notice", "message": message, "level": level})

    async def _refresh_owner(self) -> None:
        record_key = demonstration_record_key(self.organization_id, self.demonstration_id)
        async with self.cache.get_lock(demonstration_lock_key(self.organization_id, self.demonstration_id)):
            record = decode_demonstration_record(await self.cache.get(record_key))
            if record is None or record.owner != self.owner or record.status not in {"recording", "stopping"}:
                return
            record.owner_seen_at = datetime.now(UTC)
            await self.cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)

    async def _stop(self, reason: DemonstrationStopReason) -> None:
        await self._run_control_with_owner_heartbeat(self._flush_and_stop(reason))

    async def _refresh_browser_index(self) -> None:
        browser_key = demonstration_browser_key(self.organization_id, self.browser_session_id)
        async with self.cache.get_lock(f"{browser_key}:lock"):
            indexed_id = await self.cache.get(browser_key)
            if isinstance(indexed_id, bytes):
                indexed_id = indexed_id.decode()
            if indexed_id == self.demonstration_id:
                await self.cache.set(browser_key, self.demonstration_id, ex=DEMONSTRATION_TTL)

    async def _flush_and_stop(self, reason: DemonstrationStopReason) -> None:
        tap = self.tap
        if tap is not None:
            with contextlib.suppress(Exception):
                await tap.flush()
                reason = await self._drain_tap() or reason
            with contextlib.suppress(Exception):
                await tap.stop()
                reason = await self._drain_tap() or reason
            with contextlib.suppress(Exception):
                await tap.set_intercept_file_chooser_dialog(False)
        final_url = str(self.page.url or "") if self.page is not None and not self.page.is_closed() else None
        record_key = demonstration_record_key(self.organization_id, self.demonstration_id)
        event_ttl: timedelta | None = None
        async with self.cache.get_lock(demonstration_lock_key(self.organization_id, self.demonstration_id)):
            record = decode_demonstration_record(await self.cache.get(record_key))
            if record is not None and record.owner == self.owner and record.status in {"recording", "stopping"}:
                reason = reason if reason == "cancelled" else record.stop_reason or reason
                record.status = "cancelled" if reason == "cancelled" else "stopped"
                record.stop_reason = reason
                record.owner = None
                record.owner_seen_at = None
                record.final_url = final_url
                event_ttl = DEMONSTRATION_CANCELLED_TTL if record.status == "cancelled" else DEMONSTRATION_TTL
                await self.cache.set(record_key, encode_demonstration_record(record), ex=event_ttl)
                action_count = record.event_count
            else:
                action_count = record.event_count if record is not None else 0
        if event_ttl is not None:
            await expire_demonstration_payload(
                self.cache, self.organization_id, self.demonstration_id, action_count, event_ttl
            )
        if event_ttl == DEMONSTRATION_TTL:
            await self._refresh_browser_index()
        await wait_for_driver_releases(
            [
                start_browser_driver_release(self.organization_id, attached)
                for attached in self.recording_ctx.attached_browser_drivers.values()
            ]
        )
        await self.stream.send(
            {
                "type": "demonstration_stopped",
                "demonstration_id": self.demonstration_id,
                "reason": reason,
                "action_count": action_count,
            }
        )
        LOG.info(
            "copilot_demonstration_stopped",
            reason=reason,
            action_count=action_count,
            sensitive_count=self.sensitive_count,
            duration_s=round(time.monotonic() - self.started_at, 3),
        )


def safe_label(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(redact_raw_secrets_for_prompt(value).replace('"', "").split())[:200]


def _event_label(event: dict[str, Any]) -> str:
    kind = event.get("kind")
    packet = event.get("packet")
    role_name = packet.get("role_name") if isinstance(packet, dict) else None
    accessible_name = role_name.get("accessible_name") if isinstance(role_name, dict) else None
    if kind == "click":
        return f'Click "{safe_label(accessible_name) or "element"}"'[:60]
    if kind == "type":
        value = event.get("value")
        text = redact_raw_secrets_for_prompt(value) if isinstance(value, str) else ""
        return f'Type "{text}"'[:60]
    if kind == "select":
        label = event.get("label") or event.get("value") or "option"
        return f'Select "{safe_label(str(label))}"'[:60]
    if kind == "key":
        return f"Press {event.get('key') or 'key'}"[:60]
    if kind == "navigate":
        url = event.get("url")
        parsed = None
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            host = parsed.hostname if parsed is not None else None
            port = f":{parsed.port}" if parsed is not None and parsed.port is not None else ""
        except ValueError:
            host = None
            port = ""
        destination = f"{host}{port}{parsed.path}" if parsed is not None and host is not None else "page"
        destination = redact_raw_secrets_for_prompt(destination)
        return f"Go to {destination}"[:60]
    return "Action"
