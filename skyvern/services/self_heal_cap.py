from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.cache.factory import CacheFactory

try:
    from redis.exceptions import LockError as _RedisLockError
except ImportError:

    class _RedisLockError(Exception):  # type: ignore[no-redef]
        pass


LOG = structlog.get_logger()

__all__ = ["check_and_increment_self_heal_cap", "self_heal_daily_cap_key"]


def self_heal_daily_cap_key(workflow_permanent_id: str, organization_id: str | None = None) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    org_scope = organization_id or "global"
    return f"self_heal:daily_cap:{org_scope}:{workflow_permanent_id}:{today}"


async def check_and_increment_self_heal_cap(
    *, workflow_permanent_id: str, organization_id: str | None = None
) -> int | None:
    try:
        cache = CacheFactory.get_cache()
        if cache is None:
            # Unconfigured cache (OSS/local) fails open — the heal toggle still gates; a configured
            # cache that errors fails closed below, preserving the atomic reservation in prod.
            LOG.warning(
                "self-heal cap cache is not configured; allowing heal uncapped",
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            return 1
        cap = int(settings.SELF_HEAL_DAILY_CAP)
        cap_key = self_heal_daily_cap_key(workflow_permanent_id, organization_id)
        org_scope = organization_id or "global"
        lock = cache.get_lock(f"self_heal_cap:{org_scope}:{workflow_permanent_id}", blocking_timeout=2, timeout=5)
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
            "self-heal cap lock acquisition failed; failing closed",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None
    except Exception:
        LOG.warning(
            "self-heal cap check_and_increment failed; failing closed",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None
