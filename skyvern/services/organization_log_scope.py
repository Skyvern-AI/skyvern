import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from time import monotonic

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.organization_age_cache import is_organization_age_cached
from skyvern.forge.sdk.forge_log import has_log_organization_fields, log_organization_fields

LOG = structlog.get_logger()

# An organization read here only adds log fields, so a hung one must not hold the work it wraps (a Stripe sync
# holds its lock across a warm-up).
_ORGANIZATION_READ_TIMEOUT_SECONDS = 2.0

# An org id with no row (deleted, or never existed) is not read again for ten minutes. A failed or timed-out
# read backs off for a minute, so a database slowdown costs one bounded wait per org per minute, not one per
# activity start; after that the next caller retries.
_MISSING_ORGANIZATION_TTL_SECONDS = 600.0
_FAILED_READ_BACKOFF_SECONDS = 60.0
_MAX_SKIPPED_ORGANIZATIONS = 10_000
_missing_organization_until: dict[str, float] = {}
_failed_read_until: dict[str, float] = {}
# Concurrent warm-ups of one org share a single read. A task belongs to its event loop, so a caller on
# another loop starts its own read rather than awaiting a foreign task.
_warmups_in_flight: dict[str, asyncio.Task[None]] = {}
# Writers take the lock, as in organization_age_cache: activities may run on more than one thread.
_lock = threading.Lock()


@asynccontextmanager
async def organization_log_scope(organization_id: str | None) -> AsyncIterator[None]:
    """Stamp an organization's id, name and age on log lines of work outside any context, as log fields only:
    no SkyvernContext is created, so code that branches on having one behaves as before."""
    organization = None
    if organization_id is not None and skyvern_context.current() is None and not has_log_organization_fields():
        try:
            async with asyncio.timeout(_ORGANIZATION_READ_TIMEOUT_SECONDS):
                organization = await app.DATABASE.organizations.get_organization(organization_id)
        except Exception:
            LOG.warning("Failed to load organization for log fields", organization_id=organization_id, exc_info=True)
    if organization_id is None or organization is None:
        yield
        return
    with log_organization_fields(
        organization_id, organization.organization_name, skyvern_context.compute_org_age(organization.created_at)
    ):
        yield


async def warm_organization_age(organization_id: str | None) -> None:
    """Load an organization once per process so log lines that only name it can carry its age."""
    if organization_id is None or is_organization_age_cached(organization_id):
        return
    now = monotonic()
    if now < _missing_organization_until.get(organization_id, 0.0) or now < _failed_read_until.get(
        organization_id, 0.0
    ):
        return
    loop = asyncio.get_running_loop()
    with _lock:
        warmup = _warmups_in_flight.get(organization_id)
        if warmup is None or warmup.get_loop() is not loop:
            warmup = loop.create_task(_load_organization(organization_id))
            _warmups_in_flight[organization_id] = warmup
            warmup.add_done_callback(partial(_forget_warmup, organization_id))
    # One caller's cancellation must not cancel the read the other callers are waiting on.
    await asyncio.shield(warmup)


async def _load_organization(organization_id: str) -> None:
    try:
        # The row converter records a found org's creation time, so a hit needs nothing more here.
        async with asyncio.timeout(_ORGANIZATION_READ_TIMEOUT_SECONDS):
            organization = await app.DATABASE.organizations.get_organization(organization_id)
    except Exception:
        LOG.warning("Failed to load organization for log fields", organization_id=organization_id, exc_info=True)
        _skip_reads_until(_failed_read_until, organization_id, _FAILED_READ_BACKOFF_SECONDS)
        return
    if organization is None:
        _skip_reads_until(_missing_organization_until, organization_id, _MISSING_ORGANIZATION_TTL_SECONDS)


def _forget_warmup(organization_id: str, warmup: asyncio.Task[None]) -> None:
    with _lock:
        if _warmups_in_flight.get(organization_id) is warmup:
            del _warmups_in_flight[organization_id]


def _skip_reads_until(deadlines: dict[str, float], organization_id: str, seconds: float) -> None:
    with _lock:
        deadlines.pop(organization_id, None)
        if len(deadlines) >= _MAX_SKIPPED_ORGANIZATIONS:
            del deadlines[next(iter(deadlines))]
        deadlines[organization_id] = monotonic() + seconds
