from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye.schemas import BrowserSessionResponse

# Every field a client is allowed to read off a browser session. Adding a field to
# BrowserSessionResponse fails the pin below until it is listed here, which is the point:
# the row carries upstream routing and provider identity, and the response is the allowlist.
PINNED_CLIENT_FIELDS = frozenset(
    {
        "browser_session_id",
        "organization_id",
        "status",
        "runnable_type",
        "runnable_id",
        "timeout",
        "browser_address",
        "app_url",
        "extensions",
        "browser_type",
        "browser_profile_id",
        "generate_browser_profile",
        "vnc_streaming_supported",
        "download_path",
        "downloaded_files",
        "recordings",
        "started_at",
        "completed_at",
        "created_at",
        "modified_at",
        "deleted_at",
    }
)

# Row fields the response legitimately reflects, under the response's own names.
CLIENT_VISIBLE_ROW_FIELDS = frozenset(
    {
        "persistent_browser_session_id",  # -> browser_session_id
        "timeout_minutes",  # -> timeout
        "organization_id",
        "runnable_type",
        "runnable_id",
        "browser_address",
        "status",
        "extensions",
        "browser_type",
        "browser_profile_id",
        "generate_browser_profile",
        "started_at",
        "completed_at",
        "created_at",
        "modified_at",
        "deleted_at",
    }
)

# Server-side row fields that take a free-form string, so a sentinel round-trips unvalidated.
SERVER_SIDE_STRING_ROW_FIELDS = (
    "ip_address",
    "upstream_cdp_url",
    "browser_vendor",
    "browser_id",
    "instance_type",
)


def server_side_row_fields() -> set[str]:
    """Row fields no client may read. Derived, so a newly added row field is server-side
    by default and has to be named in CLIENT_VISIBLE_ROW_FIELDS to become readable."""
    return set(PersistentBrowserSession.model_fields) - CLIENT_VISIBLE_ROW_FIELDS


def test_browser_session_response_exposes_exactly_the_pinned_client_field_set() -> None:
    assert set(BrowserSessionResponse.model_fields) == PINNED_CLIENT_FIELDS


def test_no_server_side_row_field_becomes_a_response_field() -> None:
    leaked = server_side_row_fields() & set(BrowserSessionResponse.model_fields)
    assert leaked == set()


@pytest.mark.asyncio
async def test_no_server_side_row_value_reaches_the_serialized_response() -> None:
    """from_browser_session must stay a constructed allowlist. Dumping the row instead
    would carry every sentinel below into the payload."""
    now = datetime.now(timezone.utc)
    sentinels = {field: f"server-side-{field}-sentinel" for field in SERVER_SIDE_STRING_ROW_FIELDS}
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address="wss://proxy.example/pbs_123?token=t",
        created_at=now,
        modified_at=now,
        **sentinels,
    )

    with patch.object(
        app.AGENT_FUNCTION,
        "resolve_browser_session_connect_url",
        AsyncMock(return_value=session.browser_address),
    ):
        response = await BrowserSessionResponse.from_browser_session(session)

    serialized = response.model_dump_json()
    for field, sentinel in sentinels.items():
        assert sentinel not in serialized, f"{field} leaked into the response"
        assert field not in serialized


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
async def test_browser_session_response_reports_no_vnc_when_the_infrastructure_cannot_serve_it() -> None:
    """An address the client can dial does not imply a live view stream behind it. Reporting
    supported anyway is what makes the UI offer a stream that then fails on click."""
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address="wss://session-router.example/pbs_123",
        ip_address=None,
        created_at=now,
        modified_at=now,
    )

    with (
        patch.object(
            app.AGENT_FUNCTION,
            "resolve_browser_session_connect_url",
            AsyncMock(return_value=session.browser_address),
        ),
        patch.object(app.AGENT_FUNCTION, "supports_live_view", AsyncMock(return_value=False)),
    ):
        response = await BrowserSessionResponse.from_browser_session(session)

    assert response.vnc_streaming_supported is False


@pytest.mark.asyncio
async def test_browser_session_response_tells_the_capability_which_address_the_session_holds() -> None:
    """The capability short-circuits on a pod address, so a caller that never forwards one turns
    that short-circuit into dead code and puts every response behind the lookup."""
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="running",
        browser_address="wss://session-router.example/pbs_123",
        ip_address="10.0.0.7",
        created_at=now,
        modified_at=now,
    )
    capability = AsyncMock(return_value=True)

    with (
        patch.object(
            app.AGENT_FUNCTION,
            "resolve_browser_session_connect_url",
            AsyncMock(return_value=session.browser_address),
        ),
        patch.object(app.AGENT_FUNCTION, "supports_live_view", capability),
    ):
        await BrowserSessionResponse.from_browser_session(session)

    capability.assert_awaited_once_with("pbs_123", ip_address="10.0.0.7")


@pytest.mark.asyncio
async def test_base_agent_function_serves_live_view_for_every_session() -> None:
    """A self-hosted deployment runs every browser itself, so the capability is unconditional."""
    assert await AgentFunction().supports_live_view("pbs_123", ip_address=None) is True


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
        browser_id="upstream-session-cafebabe",
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
    for leaked in ("10.0.0.7", "upstream_cdp_url", "browser_vendor", "browser_id", "upstream-session-cafebabe"):
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
