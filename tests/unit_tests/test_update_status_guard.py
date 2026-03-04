"""Tests for the update_status finalization guard in default_persistent_sessions_manager."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub the AWS module to avoid import-time boto session creation.
sys.modules.setdefault("skyvern.forge.sdk.api.aws", MagicMock())

from skyvern.forge.sdk.schemas.persistent_browser_sessions import (  # noqa: E402
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
)
from skyvern.webeye.default_persistent_sessions_manager import update_status  # noqa: E402

SESSION_ID = "sess_1"
ORG_ID = "org_1"
NOW = datetime.now(timezone.utc)


def _make_session(status: str) -> PersistentBrowserSession:
    return PersistentBrowserSession(
        persistent_browser_session_id=SESSION_ID,
        organization_id=ORG_ID,
        status=status,
        created_at=NOW,
        modified_at=NOW,
    )


@pytest.mark.parametrize(
    "desired_status",
    [
        pytest.param(PersistentBrowserSessionStatus.running, id="non-final-to-final"),
        pytest.param(PersistentBrowserSessionStatus.failed, id="final-to-final"),
    ],
)
@pytest.mark.asyncio
async def test_rejects_update_when_already_final(desired_status: str):
    """A finalized session must not accept any status update."""
    db = AsyncMock()
    db.get_persistent_browser_session.return_value = _make_session(
        PersistentBrowserSessionStatus.completed,
    )

    result = await update_status(db, SESSION_ID, ORG_ID, desired_status)

    assert result is None
    db.update_persistent_browser_session.assert_not_called()
