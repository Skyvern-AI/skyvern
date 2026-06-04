from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.cache.factory import CacheFactory

try:
    from redis.exceptions import LockError as _RedisLockError
except ImportError:

    class _RedisLockError(Exception):  # type: ignore[no-redef]
        pass


LOG = structlog.get_logger()

CapGetter = Callable[[str | None], Awaitable[int]]
ReviewerVersion = Literal["v2", "v3"]

__all__ = [
    "CapGetter",
    "ReviewerVersion",
    "check_and_increment_cap_v3",
    "get_script_review_cap",
    "increment_script_review_counter_v2",
    "is_script_review_cap_exceeded_v2",
    "is_script_review_cap_exceeded_v3",
    "try_increment_script_review_counter_v3",
    "v2_script_review_cap_key",
    "v3_script_review_cap_key",
]


def v2_script_review_cap_key(workflow_permanent_id: str) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"script_reviewer:daily_cap:{workflow_permanent_id}:{today}"


def v3_script_review_cap_key(workflow_permanent_id: str) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"script_review_counter:v3:{workflow_permanent_id}:{today}"


async def get_script_review_cap(organization_id: str | None) -> int:
    default_cap: int = settings.SCRIPT_REVIEW_DAILY_CAP
    if not organization_id or not app.EXPERIMENTATION_PROVIDER:
        return default_cap

    try:
        payload = await app.EXPERIMENTATION_PROVIDER.get_payload_cached(
            "script_review_daily_cap",
            organization_id,
            properties={"organization_id": organization_id},
        )
        if payload is not None:
            custom_cap = int(payload)
            if custom_cap > 0:
                LOG.info(
                    "Using custom script review daily cap from experimentation provider",
                    cap=custom_cap,
                    organization_id=organization_id,
                )
                return custom_cap
    except (ValueError, TypeError):
        LOG.warning(
            "Invalid script_review_daily_cap payload, using default",
            organization_id=organization_id,
            exc_info=True,
        )
    except Exception:
        LOG.debug(
            "Failed to fetch script_review_daily_cap payload, using default",
            organization_id=organization_id,
            exc_info=True,
        )
    return default_cap


def _cap_key_for_version(workflow_permanent_id: str, reviewer_version: ReviewerVersion) -> str:
    if reviewer_version == "v2":
        return v2_script_review_cap_key(workflow_permanent_id)
    if reviewer_version == "v3":
        return v3_script_review_cap_key(workflow_permanent_id)
    raise ValueError(f"Unsupported script reviewer version: {reviewer_version!r}")


async def _is_script_review_cap_key_exceeded(
    *,
    cap_key: str,
    organization_id: str | None = None,
    fail_closed: bool = False,
    cap_getter: CapGetter | None = None,
) -> bool:
    try:
        cache = CacheFactory.get_cache()
        if cache is None:
            return fail_closed
        raw_count = await cache.get(cap_key)
        if raw_count is None:
            return False
        cap = await (cap_getter or get_script_review_cap)(organization_id)
        return int(raw_count) >= cap
    except Exception:
        LOG.debug("Failed to check script review cap", cap_key=cap_key, exc_info=True)
        return fail_closed


async def _is_script_review_cap_exceeded(
    *,
    workflow_permanent_id: str,
    reviewer_version: ReviewerVersion,
    organization_id: str | None = None,
    fail_closed: bool = False,
    cap_getter: CapGetter | None = None,
) -> bool:
    return await _is_script_review_cap_key_exceeded(
        cap_key=_cap_key_for_version(workflow_permanent_id, reviewer_version),
        organization_id=organization_id,
        fail_closed=fail_closed,
        cap_getter=cap_getter,
    )


async def is_script_review_cap_exceeded_v2(
    *,
    workflow_permanent_id: str,
    organization_id: str | None = None,
    fail_closed: bool = False,
    cap_getter: CapGetter | None = None,
) -> bool:
    return await _is_script_review_cap_exceeded(
        workflow_permanent_id=workflow_permanent_id,
        reviewer_version="v2",
        organization_id=organization_id,
        fail_closed=fail_closed,
        cap_getter=cap_getter,
    )


async def is_script_review_cap_exceeded_v3(
    *,
    workflow_permanent_id: str,
    organization_id: str | None = None,
    fail_closed: bool = False,
    cap_getter: CapGetter | None = None,
) -> bool:
    return await _is_script_review_cap_exceeded(
        workflow_permanent_id=workflow_permanent_id,
        reviewer_version="v3",
        organization_id=organization_id,
        fail_closed=fail_closed,
        cap_getter=cap_getter,
    )


async def _increment_script_review_counter_for_key(cap_key: str) -> None:
    """Increment the legacy v2 counter on a best-effort, non-atomic path.

    v2 keeps historical fail-open behavior; v3 uses `check_and_increment_cap_v3`
    for the locked check-and-reserve path.
    """
    try:
        cache = CacheFactory.get_cache()
        if cache is None:
            return
        raw_count = await cache.get(cap_key)
        new_count = (int(raw_count) + 1) if raw_count is not None else 1
        await cache.set(cap_key, str(new_count), ex=timedelta(hours=48))
    except Exception:
        LOG.debug("Failed to increment script review counter", cap_key=cap_key, exc_info=True)


async def _increment_script_review_counter(
    *,
    workflow_permanent_id: str,
    reviewer_version: ReviewerVersion,
) -> None:
    await _increment_script_review_counter_for_key(_cap_key_for_version(workflow_permanent_id, reviewer_version))


async def increment_script_review_counter_v2(workflow_permanent_id: str) -> None:
    await _increment_script_review_counter(workflow_permanent_id=workflow_permanent_id, reviewer_version="v2")


async def try_increment_script_review_counter_v3(
    workflow_permanent_id: str,
    organization_id: str | None = None,
) -> None:
    """Try to consume one v3 no-persist review slot.

    Unlike the v2 spam-guard increment, v3 is conditional: it reuses the
    locked check-and-increment path and silently no-ops when the cap is
    exceeded, cache is unavailable, or the lock cannot be acquired. Callers
    that need to distinguish "slot acquired" from "slot denied" should call
    `check_and_increment_cap_v3` directly.
    """
    await check_and_increment_cap_v3(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )


async def check_and_increment_cap_v3(
    *,
    workflow_permanent_id: str,
    organization_id: str | None = None,
    cap_getter: CapGetter | None = None,
) -> int | None:
    """Atomically acquire one v3 review-cap slot.

    Returns the new counter value on success, or None when the cap is exceeded
    or unavailable. v3 callers fail closed to avoid unbounded reviewer spend.
    """
    try:
        cache = CacheFactory.get_cache()
        if cache is None:
            return None
        cap = await (cap_getter or get_script_review_cap)(organization_id)
        cap_key = v3_script_review_cap_key(workflow_permanent_id)
        lock = cache.get_lock(f"v3_cap:{workflow_permanent_id}", blocking_timeout=2, timeout=5)
        async with lock:
            raw_count = await cache.get(cap_key)
            current = int(raw_count) if raw_count is not None else 0
            if current >= cap:
                return None
            new_count = current + 1
            await cache.set(cap_key, str(new_count), ex=timedelta(hours=48))
            return new_count
    except _RedisLockError:
        LOG.warning(
            "v3 cap lock acquisition failed; failing closed",
            workflow_permanent_id=workflow_permanent_id,
            exc_info=True,
        )
        return None
    except Exception:
        LOG.warning("v3 cap check_and_increment failed; failing closed", exc_info=True)
        return None
