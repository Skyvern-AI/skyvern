from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
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

    with patch.object(
        app.AGENT_FUNCTION,
        "resolve_browser_session_connect_url",
        AsyncMock(return_value=session.browser_address),
    ):
        response = await BrowserSessionResponse.from_browser_session(session)

    assert response.vnc_streaming_supported is True


@pytest.mark.asyncio
async def test_browser_session_response_never_exposes_upstream_routing_fields() -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address="wss://proxy.example/pbs_123/token/devtools/browser/test",
        upstream_cdp_url="ws://10.0.0.7:9222/devtools/browser/test",
        browser_vendor="websocket",
        created_at=now,
        modified_at=now,
    )

    with patch.object(
        app.AGENT_FUNCTION,
        "resolve_browser_session_connect_url",
        AsyncMock(return_value=session.browser_address),
    ):
        response = await BrowserSessionResponse.from_browser_session(session)

    serialized = response.model_dump_json()
    for leaked in ("10.0.0.7", "upstream_cdp_url", "browser_vendor"):
        assert leaked not in serialized
    assert response.browser_address == "wss://proxy.example/pbs_123/token/devtools/browser/test"


@pytest.mark.asyncio
async def test_browser_session_response_resolves_the_client_connect_url_without_mutating_the_session() -> None:
    now = datetime.now(timezone.utc)
    direct_address = "wss://cluster.example/pbs_123/token/devtools/browser/test"
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address=direct_address,
        upstream_cdp_url="ws://10.0.0.7:9223/devtools/browser/test",
        created_at=now,
        modified_at=now,
    )
    resolved_address = "wss://session-router.example/pbs_123"
    resolver = AsyncMock(return_value=resolved_address)

    with patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", resolver):
        response = await BrowserSessionResponse.from_browser_session(session)

    resolver.assert_awaited_once_with(
        organization_id="org_123",
        browser_session_id="pbs_123",
        browser_address=direct_address,
        upstream_cdp_url="ws://10.0.0.7:9223/devtools/browser/test",
    )
    assert response.browser_address == resolved_address
    assert session.browser_address == direct_address


@pytest.mark.asyncio
async def test_base_agent_function_preserves_the_existing_browser_session_address() -> None:
    direct_address = "ws://127.0.0.1:9222/devtools/browser/test"

    resolved_address = await AgentFunction().resolve_browser_session_connect_url(
        organization_id="org_123",
        browser_session_id="pbs_123",
        browser_address=direct_address,
        upstream_cdp_url="ws://10.0.0.7:9223/devtools/browser/test",
    )

    assert resolved_address == direct_address
