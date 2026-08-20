from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from pydantic import BaseModel

from skyvern.forge import app

ACTIVE_RUN_SESSION_TTL = timedelta(minutes=20)
_CLEAR_SENTINEL = ""


@dataclass
class _LocalLockEntry:
    lock: asyncio.Lock
    users: int = 0


_LOCAL_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], _LocalLockEntry] = {}


class ActiveRunSessionAssociation(BaseModel):
    organization_id: str
    workflow_permanent_id: str
    debug_browser_session_id: str
    run_browser_session_id: str
    workflow_run_id: str
    turn_id: str
    generation: str
    expires_at: datetime


def active_run_session_cache_key(organization_id: str, debug_browser_session_id: str) -> str:
    return f"copilot_active_run_session:{organization_id}:{debug_browser_session_id}"


def _lock_key(organization_id: str, debug_browser_session_id: str) -> str:
    return f"copilot_active_run_session_lock:{organization_id}:{debug_browser_session_id}"


@asynccontextmanager
async def _association_lock(organization_id: str, debug_browser_session_id: str) -> AsyncIterator[None]:
    cache_key = active_run_session_cache_key(organization_id, debug_browser_session_id)
    local_key = (asyncio.get_running_loop(), cache_key)
    entry = _LOCAL_LOCKS.setdefault(local_key, _LocalLockEntry(lock=asyncio.Lock()))
    entry.users += 1
    try:
        async with entry.lock:
            async with app.CACHE.get_lock(_lock_key(organization_id, debug_browser_session_id)):
                yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _LOCAL_LOCKS.get(local_key) is entry:
            _LOCAL_LOCKS.pop(local_key, None)


def _decode_association(raw: bytes | str | None) -> ActiveRunSessionAssociation | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        association = ActiveRunSessionAssociation.model_validate(payload)
    except (TypeError, ValueError):
        return None
    expires_at = association.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return association


async def publish_active_run_session(
    *,
    organization_id: str,
    workflow_permanent_id: str,
    debug_browser_session_id: str,
    run_browser_session_id: str,
    workflow_run_id: str,
    turn_id: str,
) -> ActiveRunSessionAssociation:
    expires_at = datetime.now(timezone.utc) + ACTIVE_RUN_SESSION_TTL
    association = ActiveRunSessionAssociation(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        debug_browser_session_id=debug_browser_session_id,
        run_browser_session_id=run_browser_session_id,
        workflow_run_id=workflow_run_id,
        turn_id=turn_id,
        generation=uuid.uuid4().hex,
        expires_at=expires_at,
    )
    async with _association_lock(organization_id, debug_browser_session_id):
        await app.CACHE.set(
            active_run_session_cache_key(organization_id, debug_browser_session_id),
            association.model_dump_json(),
            ex=ACTIVE_RUN_SESSION_TTL,
        )
    return association


async def get_active_run_session(
    *,
    organization_id: str,
    debug_browser_session_id: str,
) -> ActiveRunSessionAssociation | None:
    raw = await app.CACHE.get(active_run_session_cache_key(organization_id, debug_browser_session_id))
    return _decode_association(raw)


async def clear_active_run_session(
    *,
    organization_id: str,
    debug_browser_session_id: str,
    generation: str,
) -> bool:
    async with _association_lock(organization_id, debug_browser_session_id):
        key = active_run_session_cache_key(organization_id, debug_browser_session_id)
        current = _decode_association(await app.CACHE.get(key))
        if current is None or current.generation != generation:
            return False
        await app.CACHE.set(key, _CLEAR_SENTINEL, ex=1)
        return True
