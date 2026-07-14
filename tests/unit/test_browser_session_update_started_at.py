"""Tests for BrowserSessionsRepository.update_persistent_browser_session()."""

from datetime import datetime, timezone
from inspect import signature
from unittest.mock import MagicMock, patch

import pytest

from skyvern.forge.sdk.db.datetime_utils import to_naive_utc
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

    assert mock_pbs.started_at == to_naive_utc(now)


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


@pytest.mark.asyncio
async def test_update_persistent_browser_session_assigns_vnc_metadata_when_not_none() -> None:
    """Falsy VNC metadata values are assignments rather than omitted updates."""
    mock_pbs = MagicMock()
    mock_pbs.display_number = 99
    mock_pbs.vnc_port = 6080
    mock_pbs.interactor = "agent"
    repo = _make_browser_repo(mock_pbs)

    with patch(
        "skyvern.forge.sdk.schemas.persistent_browser_sessions.PersistentBrowserSession.model_validate",
        return_value=MagicMock(),
    ):
        await repo.update_persistent_browser_session(
            "pbs_123",
            organization_id="org_123",
            display_number=0,
            vnc_port=0,
            interactor="user",
        )

    assert mock_pbs.display_number == 0
    assert mock_pbs.vnc_port == 0
    assert mock_pbs.interactor == "user"


@pytest.mark.asyncio
async def test_update_persistent_browser_session_preserves_omitted_vnc_metadata() -> None:
    """Omitted VNC metadata leaves persisted values unchanged."""
    parameters = signature(BrowserSessionsRepository.update_persistent_browser_session).parameters
    assert {"display_number", "vnc_port", "interactor"} <= parameters.keys()

    mock_pbs = MagicMock()
    mock_pbs.display_number = 99
    mock_pbs.vnc_port = 6080
    mock_pbs.interactor = "user"
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

    assert mock_pbs.display_number == 99
    assert mock_pbs.vnc_port == 6080
    assert mock_pbs.interactor == "user"
