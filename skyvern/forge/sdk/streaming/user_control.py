from __future__ import annotations

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.cache.base import BaseCache
from skyvern.forge.sdk.cache.server_processes import server_runs_one_process
from skyvern.schemas.browser_session_timeouts import MAX_LIFETIME_SECONDS

LOG = structlog.get_logger()

_RELEASED_TTL_SECONDS = 1


def _user_control_key(organization_id: str, browser_session_id: str) -> str:
    return f"browser_session_user_control:{organization_id}:{browser_session_id}"


def _control_cache() -> BaseCache | None:
    """None when another server process could hold a mark this one cannot read."""
    if app.CACHE is not None and (app.CACHE.is_shared or server_runs_one_process()):
        return app.CACHE
    return None


async def _record_holding(organization_id: str, browser_session_id: str, holder: str, holding: bool) -> None:
    # Every controlling channel is listed, so one channel ceding or closing cannot clear another's hold.
    cache = _control_cache()
    if cache is None:
        return
    key = _user_control_key(organization_id, browser_session_id)
    try:
        async with cache.get_lock(f"{key}:lock"):
            holders = set((await cache.get(key) or "").split())
            if holding:
                holders.add(holder)
            elif holder in holders:
                holders.remove(holder)
            else:
                return
            ttl = MAX_LIFETIME_SECONDS if holders else _RELEASED_TTL_SECONDS
            await cache.set(key, " ".join(sorted(holders)), ex=ttl)
    except Exception:
        LOG.warning(
            "Failed to record take-control" if holding else "Failed to record cede-control",
            browser_session_id=browser_session_id,
            exc_info=True,
        )


async def mark_user_control(organization_id: str, browser_session_id: str, holder: str) -> None:
    await _record_holding(organization_id, browser_session_id, holder, holding=True)


async def release_user_control(organization_id: str, browser_session_id: str, holder: str) -> None:
    await _record_holding(organization_id, browser_session_id, holder, holding=False)


async def user_controls_browser_session(organization_id: str, browser_session_id: str) -> bool:
    cache = _control_cache()
    if cache is None:
        return True
    return bool(await cache.get(_user_control_key(organization_id, browser_session_id)))
