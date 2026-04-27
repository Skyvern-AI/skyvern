"""Test that BrowserSessionsRepository.update_persistent_browser_session() accepts started_at."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session


def _make_browser_repo(mock_pbs: MagicMock) -> BrowserSessionsRepository:
    mock_session = make_mock_session(mock_pbs)
    return BrowserSessionsRepository(session_factory=lambda: MockAsyncSessionCtx(mock_session))


@pytest.mark.asyncio
async def test_update_persistent_browser_session_accepts_started_at() -> None:
    """update_persistent_browser_session() must accept started_at without raising TypeError."""
    mock_pbs = MagicMock()
    mock_pbs.status = "running"
    mock_pbs.started_at = None
    repo = _make_browser_repo(mock_pbs)

    now = datetime.now(timezone.utc)
    with patch(
        "skyvern.forge.sdk.schemas.persistent_browser_sessions.PersistentBrowserSession.model_validate",
        return_value=MagicMock(),
    ):
        await repo.update_persistent_browser_session(
            "pbs_123",
            organization_id="org_123",
            started_at=now,
        )

    assert mock_pbs.started_at == now


@pytest.mark.asyncio
async def test_update_persistent_browser_session_without_started_at() -> None:
    """When started_at is not passed, the field should not be touched."""
    mock_pbs = MagicMock()
    mock_pbs.status = "created"
    original_started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mock_pbs.started_at = original_started_at
    repo = _make_browser_repo(mock_pbs)

    with patch(
        "skyvern.forge.sdk.schemas.persistent_browser_sessions.PersistentBrowserSession.model_validate",
        return_value=MagicMock(),
    ):
        await repo.update_persistent_browser_session(
            "pbs_123",
            organization_id="org_123",
            status="running",
        )

    assert mock_pbs.started_at == original_started_at
