from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.forge import app
from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    API_BROWSER_SESSION_CREATED_BY,
    Extensions,
    PersistentBrowserSession,
    PersistentBrowserType,
    is_external_cdp_session,
)
from skyvern.schemas.browser_settings import BrowserSettings, BrowserSettingsReceipt

LOG = structlog.get_logger()


class BrowserSessionResponse(BaseModel):
    """Response model for browser session information."""

    browser_session_id: str = Field(
        description="Unique identifier for the browser session. browser_session_id starts with `pbs_`.",
        examples=["pbs_123456"],
    )
    organization_id: str = Field(description="ID of the organization that owns this session")
    status: str | None = Field(
        None,
        description="Current status of the browser session",
        examples=["created", "running", "completed", "failed", "timeout"],
    )
    runnable_type: str | None = Field(
        None,
        description="Type of the current runnable associated with this session (workflow, task etc)",
        examples=["task", "workflow_run"],
    )
    runnable_id: str | None = Field(
        None, description="ID of the current runnable", examples=["tsk_123456", "wr_123456"]
    )
    timeout: int | None = Field(
        None,
        description="Timeout in minutes for the session. Timeout is applied after the session is started. Defaults to 60 minutes.",
        examples=[60, 120],
    )
    browser_address: str | None = Field(
        None,
        description="Url for connecting to the browser",
        examples=["http://localhost:9222", "https://3.12.10.11/browser/123456"],
    )
    app_url: str | None = Field(
        None,
        description="Url for the browser session page",
        examples=["https://app.skyvern.com/browser-session/pbs_123456"],
    )
    extensions: list[Extensions] | None = Field(
        None,
        description="A list of extensions installed in the browser session.",
    )
    browser_type: PersistentBrowserType | None = Field(
        default=None,
        description="The type of browser used for the session.",
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of the browser profile loaded into this session, if any. browser_profile_id starts with `bp_`.",
    )
    generate_browser_profile: bool = Field(
        default=False,
        description="Whether this session's browser profile will be saved when it ends so it can become a reusable browser profile.",
    )
    browser_settings: BrowserSettings | None = Field(
        default=None, description="Browser settings requested when the session was created."
    )
    browser_settings_receipt: BrowserSettingsReceipt | None = Field(
        default=None,
        description="What the browser reported after launch for the requested settings. Null until it is measured, "
        "and always null when no settings were requested.",
    )
    vnc_streaming_supported: bool = Field(False, description="Whether the browser session supports VNC streaming")
    stream_transport: str | None = Field(
        None,
        description='Live-view transport for this session: "vnc" or "cdp". Resolved on the single-session fetch only; null elsewhere.',
        examples=["vnc", "cdp"],
    )
    download_path: str | None = Field(None, description="The path where the browser session downloads files")
    downloaded_files: list[FileInfo] | None = Field(
        None, description="The list of files downloaded by the browser session"
    )
    recordings: list[FileInfo] | None = Field(None, description="The list of video recordings from the browser session")
    started_at: datetime | None = Field(None, description="Timestamp when the session was started")
    completed_at: datetime | None = Field(None, description="Timestamp when the session was completed")
    created_by: str | None = Field(None, description="ID of the user who created the session")
    created_at: datetime = Field(
        description="Timestamp when the session was created (the timestamp for the initial request)"
    )
    modified_at: datetime = Field(description="Timestamp when the session was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the session was deleted, if applicable")
    warning: str | None = Field(
        None,
        description="Advisory message about how the request was adjusted, if it was. Set when a requested timeout "
        "above the maximum was capped at creation, when an extension was granted less than it asked for, or when "
        "an extension was accepted but not yet confirmed; null otherwise.",
    )

    @classmethod
    async def from_browser_session(
        cls,
        browser_session: PersistentBrowserSession,
        storage: BaseStorage | None = None,
        *,
        # False deliberately preserves the existing permissive timeout behavior for PATCH,
        # active-list, and history responses; the single-session GET opts into strict lookup.
        fail_download_lookup: bool = False,
        # Resolving the transport costs a per-session infrastructure lookup, and the list
        # endpoints serialize an unpaginated set concurrently. Only the single-session fetch —
        # the one live view actually reads — pays for it.
        include_stream_transport: bool = False,
        # The fan-out endpoints already gather across sessions, so overlapping one session's two
        # listings there only doubles the request's peak pool checkouts; the polled GET opts in.
        concurrent_listings: bool = False,
    ) -> BrowserSessionResponse:
        """
        Creates a BrowserSessionResponse from a PersistentBrowserSession object.

        Args:
            browser_session: The persistent browser session to convert
            storage: The storage backend used to resolve downloaded files and recordings.
                When omitted, download and recording listings are skipped.
            fail_download_lookup: Raise a structured 503 when downloads cannot be listed.
            include_stream_transport: Whether to resolve the session's stream transport.
                Resolving it costs a per-session infrastructure lookup, so list endpoints leave it off.

        Returns:
            BrowserSessionResponse: The converted response object
        """
        app_url = (
            f"{settings.SKYVERN_APP_URL.rstrip('/')}/browser-session/{browser_session.persistent_browser_session_id}"
        )
        download_path = (
            f"/app/downloads/{browser_session.organization_id}/{browser_session.persistent_browser_session_id}"
        )
        downloaded_files: list[FileInfo] = []
        recordings: list[FileInfo] = []
        if storage:

            async def list_downloads() -> list[FileInfo]:
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        return await storage.get_shared_downloaded_files_in_browser_session(
                            organization_id=browser_session.organization_id,
                            browser_session_id=browser_session.persistent_browser_session_id,
                        )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timeout getting downloaded files",
                        browser_session_id=browser_session.persistent_browser_session_id,
                    )
                    if fail_download_lookup:
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "downloaded_files_unavailable", "retryable": True},
                        ) from None
                    return []

            async def list_recordings() -> list[FileInfo]:
                try:
                    async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                        listed = await storage.get_shared_recordings_in_browser_session(
                            organization_id=browser_session.organization_id,
                            browser_session_id=browser_session.persistent_browser_session_id,
                        )
                        if listed:
                            listed = await app.AGENT_FUNCTION.select_browser_session_recordings(
                                organization_id=browser_session.organization_id,
                                browser_session_id=browser_session.persistent_browser_session_id,
                                recordings=listed,
                                browser_vendor=browser_session.browser_vendor,
                            )
                        return listed
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timeout getting recordings", browser_session_id=browser_session.persistent_browser_session_id
                    )
                    return []

            if concurrent_listings:
                # Downloads is awaited first so its outcome still decides the response before recordings can.
                recordings_task = asyncio.create_task(list_recordings())
                try:
                    downloaded_files = await list_downloads()
                    recordings = await recordings_task
                finally:
                    # A downloads failure or a cancelled request must not leave the recordings listing running.
                    recordings_task.cancel()
                    await asyncio.gather(recordings_task, return_exceptions=True)
            else:
                downloaded_files = await list_downloads()
                recordings = await list_recordings()

            # Sort downloaded files by modified_at in descending order (newest first)
            # Treat None as "oldest".
            downloaded_files.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)
            # Sort recordings by modified_at in descending order (newest first)
            # Treat None as "oldest".
            recordings.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)

        browser_address = await app.AGENT_FUNCTION.resolve_browser_session_connect_url(
            organization_id=browser_session.organization_id,
            browser_session_id=browser_session.persistent_browser_session_id,
            browser_address=browser_session.browser_address,
            # A registered external browser is dialed only in-process, never minted a router URL.
            upstream_cdp_url=None if is_external_cdp_session(browser_session) else browser_session.upstream_cdp_url,
        )

        stream_transport: str | None = None
        if include_stream_transport:
            # The response contract admits exactly two transport words; anything else a resolver
            # produces is withheld rather than serialized to clients.
            stream_transport = await app.AGENT_FUNCTION.resolve_stream_transport(
                browser_session_id=browser_session.persistent_browser_session_id,
                organization_id=browser_session.organization_id,
                ip_address=browser_session.ip_address,
            )
            if stream_transport not in ("vnc", "cdp"):
                stream_transport = None

        return cls(
            browser_session_id=browser_session.persistent_browser_session_id,
            organization_id=browser_session.organization_id,
            status=browser_session.status,
            runnable_type=browser_session.runnable_type,
            runnable_id=browser_session.runnable_id,
            timeout=browser_session.timeout_minutes,
            browser_address=browser_address,
            vnc_streaming_supported=bool(browser_session.ip_address or browser_session.browser_address)
            and await app.AGENT_FUNCTION.supports_live_view(
                browser_session.persistent_browser_session_id,
                ip_address=browser_session.ip_address,
            ),
            stream_transport=stream_transport,
            app_url=app_url,
            started_at=browser_session.started_at,
            completed_at=browser_session.completed_at,
            created_at=browser_session.created_at,
            modified_at=browser_session.modified_at,
            deleted_at=browser_session.deleted_at,
            download_path=download_path,
            downloaded_files=downloaded_files,
            recordings=recordings,
            extensions=browser_session.extensions,
            browser_type=browser_session.browser_type,
            browser_profile_id=browser_session.browser_profile_id,
            generate_browser_profile=browser_session.generate_browser_profile,
            browser_settings=browser_session.browser_settings,
            browser_settings_receipt=browser_session.browser_settings_receipt,
            created_by=(
                None if browser_session.created_by == API_BROWSER_SESSION_CREATED_BY else browser_session.created_by
            ),
        )
