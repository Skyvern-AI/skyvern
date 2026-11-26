from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import structlog
from pydantic import TypeAdapter, ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.ember.context import LiveTurnNarrative

LOG = structlog.get_logger()

LIVE_TURN_NARRATIVE_TTL_SECONDS = 7200
LIVE_TURN_NARRATIVE_POLL_SECONDS = 2.0
LIVE_TURN_NARRATIVE_SET_TIMEOUT_SECONDS = 5.0
LIVE_TURN_NARRATIVE_GET_TIMEOUT_SECONDS = 1.0

_LIVE_TURN_NARRATIVE_ADAPTER = TypeAdapter(LiveTurnNarrative)


def live_turn_narrative_key(organization_id: str, chat_id: str, turn_id: str) -> str:
    return f"ember:turn_narrative:{organization_id}:{chat_id}:{turn_id}"


async def write_live_turn_narrative(
    key: str,
    build: Callable[[], LiveTurnNarrative],
    wake: asyncio.Event,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Copy the running turn's narrative into the cache until cancelled, logging the first failure of each kind."""
    last_written: str | None = None
    last_written_at = 0.0
    encode_failure_logged = False
    write_failure_logged = False
    while True:
        # Cleared before building, so a wake set while the write below is in flight starts the next pass at once.
        wake.clear()
        try:
            encoded: str | None = json.dumps(build())
        except Exception:
            encoded = None
            if not encode_failure_logged:
                LOG.warning("Live turn narrative encode failed", cache_key=key, exc_info=True)
                encode_failure_logged = True
        now = clock()
        if encoded is not None and (
            encoded != last_written or now - last_written_at >= LIVE_TURN_NARRATIVE_TTL_SECONDS / 2
        ):
            try:
                async with asyncio.timeout(LIVE_TURN_NARRATIVE_SET_TIMEOUT_SECONDS):
                    await app.CACHE.set(key, encoded, ex=LIVE_TURN_NARRATIVE_TTL_SECONDS)
                last_written, last_written_at = encoded, now
            except Exception:
                if not write_failure_logged:
                    LOG.warning("Live turn narrative write failed", cache_key=key, exc_info=True)
                    write_failure_logged = True
        # asyncio.timeout rather than wait_for: on Python 3.11 wait_for can swallow a cancel that races the
        # inner await finishing, which would leave this loop writing after its turn ended.
        try:
            async with asyncio.timeout(LIVE_TURN_NARRATIVE_POLL_SECONDS):
                await wake.wait()
        except TimeoutError:
            pass


async def read_live_turn_narrative(organization_id: str, chat_id: str, turn_id: str) -> LiveTurnNarrative | None:
    key = live_turn_narrative_key(organization_id, chat_id, turn_id)
    try:
        async with asyncio.timeout(LIVE_TURN_NARRATIVE_GET_TIMEOUT_SECONDS):
            raw = await app.CACHE.get(key)
    except Exception:
        LOG.warning("Live turn narrative read failed", cache_key=key, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return _LIVE_TURN_NARRATIVE_ADAPTER.validate_json(raw)
    except ValidationError:
        LOG.warning("Live turn narrative did not match its schema", cache_key=key, exc_info=True)
        return None
