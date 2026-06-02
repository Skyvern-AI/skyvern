from datetime import datetime, timezone

import pytest

from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.schemas import BrowserSessionResponse


@pytest.mark.asyncio
async def test_browser_session_response_supports_vnc_when_browser_address_is_set() -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address="ws://127.0.0.1:9222/devtools/browser/test",
        ip_address=None,
        created_at=now,
        modified_at=now,
    )

    response = await BrowserSessionResponse.from_browser_session(session)

    assert response.vnc_streaming_supported is True
