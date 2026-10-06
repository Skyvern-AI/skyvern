import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.webeye import schemas as browser_session_schemas
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
        "stream_transport",
        "download_path",
        "downloaded_files",
        "recordings",
        "started_at",
        "completed_at",
        "created_at",
        "modified_at",
        "deleted_at",
        "created_by",
        "warning",
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
        "created_by",
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


@pytest.mark.asyncio
async def test_browser_session_response_uses_infrastructure_aware_recording_selection() -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="completed",
        created_at=now,
        modified_at=now,
    )
    pod_recording = FileInfo(url="https://recordings.example/pod", filename="playwright-video.webm")
    vendor_recording = FileInfo(url="https://recordings.example/vendor", filename="pbs_123.mp4")
    storage = MagicMock()
    storage.get_shared_downloaded_files_in_browser_session = AsyncMock(return_value=[])
    storage.get_shared_recordings_in_browser_session = AsyncMock(return_value=[pod_recording, vendor_recording])
    selector = AsyncMock(return_value=[vendor_recording])

    with (
        patch.object(app.AGENT_FUNCTION, "select_browser_session_recordings", selector),
        patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", AsyncMock(return_value=None)),
    ):
        response = await asyncio.wait_for(BrowserSessionResponse.from_browser_session(session, storage), timeout=0.1)

    assert response.recordings == [vendor_recording]
    selector.assert_awaited_once_with(
        organization_id="org_123",
        browser_session_id="pbs_123",
        recordings=[pod_recording, vendor_recording],
        browser_vendor=None,
    )


@pytest.mark.asyncio
async def test_browser_session_response_bounds_infrastructure_recording_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="completed",
        created_at=now,
        modified_at=now,
    )
    pod_recording = FileInfo(url="https://recordings.example/pod", filename="playwright-video.webm")
    storage = MagicMock()
    storage.get_shared_downloaded_files_in_browser_session = AsyncMock(return_value=[])
    storage.get_shared_recordings_in_browser_session = AsyncMock(return_value=[pod_recording])

    async def stalled_selection(**_kwargs: object) -> list[FileInfo]:
        await asyncio.sleep(1)
        return []

    selector = AsyncMock(side_effect=stalled_selection)
    monkeypatch.setattr(browser_session_schemas, "GET_DOWNLOADED_FILES_TIMEOUT", 0.01)
    with (
        patch.object(app.AGENT_FUNCTION, "select_browser_session_recordings", selector),
        patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", AsyncMock(return_value=None)),
    ):
        response = await asyncio.wait_for(BrowserSessionResponse.from_browser_session(session, storage), timeout=0.1)

    selector.assert_awaited_once()
    assert response.recordings == []


class FakeListing:
    """A storage listing that returns or raises its outcome once released, or never finishes when held."""

    def __init__(self, outcome: list[FileInfo] | Exception, *, held: bool = False) -> None:
        self.outcome = outcome
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not held:
            self.release.set()
        self.in_flight = False

    async def __call__(self, **_kwargs: object) -> list[FileInfo]:
        self.in_flight = True
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.in_flight = False
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def storage_listing(downloads: FakeListing, recordings: FakeListing) -> MagicMock:
    storage = MagicMock()
    storage.get_shared_downloaded_files_in_browser_session = downloads
    storage.get_shared_recordings_in_browser_session = recordings
    return storage


def completed_session() -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="completed",
        created_at=now,
        modified_at=now,
    )


DOWNLOAD = FileInfo(url="https://files.example/report.pdf", filename="report.pdf")
RECORDING = FileInfo(url="https://recordings.example/session.webm", filename="session.webm")


@pytest.fixture
def base_agent_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub app mocks the agent function; these tests need the hooks to pass values through."""
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.asyncio
async def test_browser_session_response_runs_the_downloads_and_recordings_listings_together() -> None:
    downloads = FakeListing([DOWNLOAD])
    recordings = FakeListing([RECORDING])
    # Each listing finishes only once the other has started, so neither can finish unless they overlap.
    downloads.release, recordings.release = recordings.started, downloads.started

    response = await asyncio.wait_for(
        BrowserSessionResponse.from_browser_session(
            completed_session(), storage_listing(downloads, recordings), concurrent_listings=True
        ),
        timeout=1,
    )

    assert response.downloaded_files == [DOWNLOAD]
    assert response.recordings == [RECORDING]


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.asyncio
async def test_browser_session_response_lists_downloads_then_recordings_by_default() -> None:
    """The fan-out endpoints gather across sessions already, so a session's two listings must not
    overlap there: that would double the request's peak pool checkouts for no gain."""
    downloads = FakeListing([DOWNLOAD], held=True)
    recordings = FakeListing([RECORDING])
    building = asyncio.create_task(
        BrowserSessionResponse.from_browser_session(completed_session(), storage_listing(downloads, recordings))
    )
    await asyncio.wait_for(downloads.started.wait(), timeout=1)
    for _ in range(3):
        await asyncio.sleep(0)

    assert not recordings.started.is_set()

    downloads.release.set()
    response = await asyncio.wait_for(building, timeout=1)
    assert response.downloaded_files == [DOWNLOAD]
    assert response.recordings == [RECORDING]


@pytest.mark.parametrize(
    ("downloads_error", "raised"),
    [(RuntimeError("storage unavailable"), RuntimeError), (TimeoutError(), HTTPException)],
    ids=["error", "strict-timeout"],
)
@pytest.mark.asyncio
async def test_browser_session_response_stops_the_recordings_listing_when_the_downloads_listing_fails(
    downloads_error: Exception, raised: type[Exception]
) -> None:
    downloads = FakeListing(downloads_error)
    recordings = FakeListing([RECORDING], held=True)
    downloads.release = recordings.started

    with pytest.raises(raised):
        await asyncio.wait_for(
            BrowserSessionResponse.from_browser_session(
                completed_session(),
                storage_listing(downloads, recordings),
                fail_download_lookup=True,
                concurrent_listings=True,
            ),
            timeout=1,
        )

    assert recordings.started.is_set()
    assert not recordings.in_flight


@pytest.mark.asyncio
async def test_browser_session_response_answers_for_the_downloads_listing_when_both_listings_fail() -> None:
    """A storage outage fails both listings; the strict lookup's retryable 503 must not lose to the recordings error."""
    downloads = FakeListing(TimeoutError())
    recordings = FakeListing(RuntimeError("storage unavailable"))
    downloads.release = recordings.started

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            BrowserSessionResponse.from_browser_session(
                completed_session(),
                storage_listing(downloads, recordings),
                fail_download_lookup=True,
                concurrent_listings=True,
            ),
            timeout=1,
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_browser_session_response_stops_both_listings_when_the_request_is_cancelled() -> None:
    downloads = FakeListing([DOWNLOAD], held=True)
    recordings = FakeListing([RECORDING], held=True)
    building = asyncio.create_task(
        BrowserSessionResponse.from_browser_session(
            completed_session(), storage_listing(downloads, recordings), concurrent_listings=True
        )
    )
    await asyncio.wait_for(asyncio.gather(downloads.started.wait(), recordings.started.wait()), timeout=1)

    building.cancel()
    with pytest.raises(asyncio.CancelledError):
        await building

    assert not downloads.in_flight
    assert not recordings.in_flight


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.asyncio
async def test_browser_session_response_keeps_downloads_when_the_recordings_listing_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict lookup answers 503 only for its own listing; a stalled recordings listing is not its failure."""
    storage = storage_listing(downloads=FakeListing([DOWNLOAD]), recordings=FakeListing([RECORDING], held=True))
    monkeypatch.setattr(browser_session_schemas, "GET_DOWNLOADED_FILES_TIMEOUT", 0.01)

    response = await asyncio.wait_for(
        BrowserSessionResponse.from_browser_session(completed_session(), storage, fail_download_lookup=True),
        timeout=1,
    )

    assert response.downloaded_files == [DOWNLOAD]
    assert response.recordings == []


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.asyncio
async def test_browser_session_response_keeps_recordings_when_the_downloads_listing_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = storage_listing(downloads=FakeListing([DOWNLOAD], held=True), recordings=FakeListing([RECORDING]))
    monkeypatch.setattr(browser_session_schemas, "GET_DOWNLOADED_FILES_TIMEOUT", 0.01)

    response = await asyncio.wait_for(
        BrowserSessionResponse.from_browser_session(completed_session(), storage), timeout=1
    )

    assert response.downloaded_files == []
    assert response.recordings == [RECORDING]


@pytest.mark.parametrize("failing", ["downloads", "recordings"])
@pytest.mark.asyncio
async def test_browser_session_response_surfaces_a_listing_error_that_is_not_a_timeout(failing: str) -> None:
    error = RuntimeError("storage unavailable")
    storage = storage_listing(
        downloads=FakeListing(error if failing == "downloads" else [DOWNLOAD]),
        recordings=FakeListing(error if failing == "recordings" else [RECORDING]),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await BrowserSessionResponse.from_browser_session(completed_session(), storage)

    assert exc_info.value is error


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.asyncio
async def test_browser_session_response_lists_files_newest_first_with_undated_files_last() -> None:
    older = FileInfo(url="https://files.example/older", modified_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = FileInfo(url="https://files.example/newer", modified_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    undated = FileInfo(url="https://files.example/undated")
    storage = storage_listing(
        downloads=FakeListing([older, undated, newer]), recordings=FakeListing([undated, newer, older])
    )

    response = await BrowserSessionResponse.from_browser_session(completed_session(), storage)

    assert response.downloaded_files == [newer, older, undated]
    assert response.recordings == [newer, older, undated]


_KEYRING = '{"current_kid": "k1", "keys": {"k1": {"secret": "00"}}}'


def s3_holding_one_file_per_prefix() -> MagicMock:
    async def list_files(uri: str) -> list[str]:
        name = "s3_session.webm" if uri.endswith("/videos") else "s3_report.pdf"
        return [f"{uri.split('/', 3)[3]}/{name}"]

    client = MagicMock()
    client.list_files = AsyncMock(side_effect=list_files)
    client.get_object_info = AsyncMock(return_value={"Metadata": {}, "LastModified": None, "ContentLength": 10})
    client.create_presigned_urls = AsyncMock(side_effect=lambda keys: [f"https://b.s3.amazonaws.com/{k}" for k in keys])
    return client


@pytest.mark.usefixtures("base_agent_function")
@pytest.mark.parametrize(
    ("keyring", "rows", "expected"),
    [
        pytest.param(_KEYRING, "none", ([], []), id="keyring-no-rows-skips-s3"),
        pytest.param(_KEYRING, "present", (["row_report.pdf"], ["row_session.webm"]), id="keyring-serves-rows"),
        pytest.param(_KEYRING, "lookup-raises", (["s3_report.pdf"], ["s3_session.webm"]), id="failed-lookup-lists"),
        pytest.param(None, "none", (["s3_report.pdf"], ["s3_session.webm"]), id="no-keyring-lists"),
    ],
)
@pytest.mark.asyncio
async def test_browser_session_files_come_from_artifact_rows_and_list_s3_only_as_a_fallback(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    keyring: str | None,
    rows: str,
    expected: tuple[list[str], list[str]],
) -> None:
    db = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    monkeypatch.setattr(app, "DATABASE", db)
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", keyring)
    session = completed_session()
    prefix = (
        f"s3://{settings.AWS_S3_BUCKET_ARTIFACTS}/v1/{settings.ENV}/{session.organization_id}"
        f"/browser_sessions/{session.persistent_browser_session_id}"
    )
    if rows == "present":
        for artifact_type, path in (
            (ArtifactType.DOWNLOAD, "downloads/row_report.pdf"),
            (ArtifactType.RECORDING, "videos/row_session.webm"),
        ):
            await db.artifacts.create_artifact(
                artifact_id=f"a_{artifact_type}",
                artifact_type=artifact_type,
                uri=f"{prefix}/{path}",
                organization_id=session.organization_id,
                browser_session_id=session.persistent_browser_session_id,
            )
    elif rows == "lookup-raises":
        monkeypatch.setattr(
            db.artifacts, "list_artifacts_for_browser_session_by_type", AsyncMock(side_effect=RuntimeError("db down"))
        )
    storage = S3Storage()
    storage.async_client = s3_holding_one_file_per_prefix()

    response = await BrowserSessionResponse.from_browser_session(session, storage, concurrent_listings=True)

    downloads = [f.filename for f in response.downloaded_files or []]
    recordings = [f.filename for f in response.recordings or []]
    assert (downloads, recordings) == expected
    assert storage.async_client.list_files.await_count == (2 if expected[0] == ["s3_report.pdf"] else 0)


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


@pytest.mark.asyncio
async def test_browser_session_response_carries_per_session_stream_transport() -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="active",
        created_at=now,
        modified_at=now,
    )

    with (
        patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", AsyncMock(return_value=None)),
        patch.object(
            app.AGENT_FUNCTION, "resolve_stream_transport", AsyncMock(return_value="cdp")
        ) as transport_resolver,
    ):
        response = await BrowserSessionResponse.from_browser_session(session, include_stream_transport=True)

    assert response.stream_transport == "cdp"
    transport_resolver.assert_awaited_once_with(
        browser_session_id="pbs_123", organization_id="org_123", ip_address=None
    )


@pytest.mark.asyncio
async def test_browser_session_response_leaves_the_transport_unresolved_by_default() -> None:
    """The list endpoints serialize an unpaginated set concurrently, so they must not each pay a
    per-session infrastructure lookup — nor publish which sessions are hosted elsewhere."""
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="active",
        created_at=now,
        modified_at=now,
    )

    with (
        patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", AsyncMock(return_value=None)),
        patch.object(
            app.AGENT_FUNCTION, "resolve_stream_transport", AsyncMock(return_value="cdp")
        ) as transport_resolver,
    ):
        response = await BrowserSessionResponse.from_browser_session(session)

    assert response.stream_transport is None
    transport_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_response_withholds_a_transport_outside_the_contract() -> None:
    now = datetime.now(timezone.utc)
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status="active",
        created_at=now,
        modified_at=now,
    )

    with (
        patch.object(app.AGENT_FUNCTION, "resolve_browser_session_connect_url", AsyncMock(return_value=None)),
        patch.object(app.AGENT_FUNCTION, "resolve_stream_transport", AsyncMock(return_value="webrtc")),
    ):
        response = await BrowserSessionResponse.from_browser_session(session, include_stream_transport=True)

    assert response.stream_transport is None
