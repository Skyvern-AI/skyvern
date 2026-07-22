"""Tests for BrowserSessionsRepository.set_persistent_browser_session_browser_address()."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session

UPSTREAM = "ws://10.0.0.7:9222/devtools/browser/b1"
PROXIED = "wss://proxy.example/pbs_123/token/devtools/browser/b1"


async def _write_address(mock_session: MagicMock) -> None:
    repo = BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))
    await repo.set_persistent_browser_session_browser_address(
        browser_session_id="pbs_123",
        browser_address=PROXIED,
        ip_address="10.0.0.7",
        ecs_task_arn=None,
        organization_id="org_123",
        upstream_cdp_url=UPSTREAM,
        browser_vendor="websocket",
    )


@pytest.mark.asyncio
async def test_address_write_persists_the_routing_fields() -> None:
    mock_pbs = MagicMock()
    await _write_address(make_mock_session(mock_pbs))

    assert mock_pbs.browser_address == PROXIED
    assert mock_pbs.upstream_cdp_url == UPSTREAM
    assert mock_pbs.browser_vendor == "websocket"


@pytest.mark.asyncio
async def test_failed_address_write_never_renders_the_upstream_in_the_error() -> None:
    """A failed commit renders its bound parameters, and callers log the error text."""
    mock_session = make_mock_session(MagicMock())
    mock_session.commit.side_effect = IntegrityError(
        "UPDATE persistent_browser_sessions SET upstream_cdp_url=%(upstream_cdp_url)s",
        {"upstream_cdp_url": UPSTREAM},
        Exception("duplicate key value violates unique constraint"),
    )

    with pytest.raises(IntegrityError) as excinfo:
        await _write_address(mock_session)

    assert UPSTREAM not in str(excinfo.value)
