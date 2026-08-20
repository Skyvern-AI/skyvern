import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot import active_run_session as active_run_session_mod
from skyvern.forge.sdk.copilot.active_run_session import (
    ActiveRunSessionAssociation,
    active_run_session_cache_key,
    clear_active_run_session,
    get_active_run_session,
    publish_active_run_session,
)


@pytest.fixture
def cache() -> LocalCache:
    return LocalCache()


@pytest.mark.asyncio
async def test_publish_and_get_preserve_typed_identity_and_expiry(cache: LocalCache) -> None:
    with patch.object(active_run_session_mod, "app", SimpleNamespace(CACHE=cache)):
        published = await publish_active_run_session(
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            debug_browser_session_id="pbs_debug",
            run_browser_session_id="pbs_run",
            workflow_run_id="wr_1",
            turn_id="turn_1",
        )
        loaded = await get_active_run_session(
            organization_id="org_1",
            debug_browser_session_id="pbs_debug",
        )

    assert loaded == published
    assert published.expires_at > datetime.now(timezone.utc)
    assert published.generation


@pytest.mark.asyncio
async def test_get_rejects_malformed_and_expired_records(cache: LocalCache) -> None:
    key = active_run_session_cache_key("org_1", "pbs_debug")
    expired = ActiveRunSessionAssociation(
        organization_id="org_1",
        workflow_permanent_id="wpid_1",
        debug_browser_session_id="pbs_debug",
        run_browser_session_id="pbs_run",
        workflow_run_id="wr_1",
        turn_id="turn_1",
        generation="old",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with patch.object(active_run_session_mod, "app", SimpleNamespace(CACHE=cache)):
        await cache.set(key, expired.model_dump_json())
        assert (
            await get_active_run_session(
                organization_id="org_1",
                debug_browser_session_id="pbs_debug",
            )
            is None
        )
        await cache.set(key, "{not-json")
        assert (
            await get_active_run_session(
                organization_id="org_1",
                debug_browser_session_id="pbs_debug",
            )
            is None
        )


@pytest.mark.asyncio
async def test_older_generation_cannot_clear_successor(cache: LocalCache) -> None:
    with patch.object(active_run_session_mod, "app", SimpleNamespace(CACHE=cache)):
        first = await publish_active_run_session(
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            debug_browser_session_id="pbs_debug",
            run_browser_session_id="pbs_run_1",
            workflow_run_id="wr_1",
            turn_id="turn_1",
        )
        second = await publish_active_run_session(
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            debug_browser_session_id="pbs_debug",
            run_browser_session_id="pbs_run_2",
            workflow_run_id="wr_2",
            turn_id="turn_2",
        )
        cleared = await clear_active_run_session(
            organization_id="org_1",
            debug_browser_session_id="pbs_debug",
            generation=first.generation,
        )
        loaded = await get_active_run_session(
            organization_id="org_1",
            debug_browser_session_id="pbs_debug",
        )

    assert cleared is False
    assert loaded == second


@pytest.mark.asyncio
async def test_matching_generation_clears(cache: LocalCache) -> None:
    with patch.object(active_run_session_mod, "app", SimpleNamespace(CACHE=cache)):
        published = await publish_active_run_session(
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            debug_browser_session_id="pbs_debug",
            run_browser_session_id="pbs_run",
            workflow_run_id="wr_1",
            turn_id="turn_1",
        )
        assert await clear_active_run_session(
            organization_id="org_1",
            debug_browser_session_id="pbs_debug",
            generation=published.generation,
        )
        assert (
            await get_active_run_session(
                organization_id="org_1",
                debug_browser_session_id="pbs_debug",
            )
            is None
        )


@pytest.mark.asyncio
async def test_overlapping_successor_publish_survives_older_clear(cache: LocalCache) -> None:
    with patch.object(active_run_session_mod, "app", SimpleNamespace(CACHE=cache)):
        first = await publish_active_run_session(
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            debug_browser_session_id="pbs_debug",
            run_browser_session_id="pbs_run_1",
            workflow_run_id="wr_1",
            turn_id="turn_1",
        )
        _, second = await asyncio.gather(
            clear_active_run_session(
                organization_id="org_1",
                debug_browser_session_id="pbs_debug",
                generation=first.generation,
            ),
            publish_active_run_session(
                organization_id="org_1",
                workflow_permanent_id="wpid_1",
                debug_browser_session_id="pbs_debug",
                run_browser_session_id="pbs_run_2",
                workflow_run_id="wr_2",
                turn_id="turn_2",
            ),
        )
        loaded = await get_active_run_session(
            organization_id="org_1",
            debug_browser_session_id="pbs_debug",
        )

    assert loaded == second
    assert active_run_session_mod._LOCAL_LOCKS == {}
