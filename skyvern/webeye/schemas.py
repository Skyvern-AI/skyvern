from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT
from skyvern.forge.sdk.artifact.storage.base import BaseStorage
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession

LOG = structlog.get_logger()


class BrowserSessionResponse(BaseModel):
    """Response model for browser session information."""

    browser_session_id: str = Field(
        description="Unique identifier for the browser session. browser_session_id starts with `pbs_`.",
        examples=["pbs_123456"],
    )
    organization_id: str = Field(description="ID of the organization that owns this session")
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
    vnc_streaming_supported: bool = Field(False, description="Whether the browser session supports VNC streaming")
    download_path: str | None = Field(None, description="The path where the browser session downloads files")
    downloaded_files: list[FileInfo] | None = Field(
        None, description="The list of files downloaded by the browser session"
    )
    recordings: list[FileInfo] | None = Field(None, description="The list of video recordings from the browser session")
    started_at: datetime | None = Field(None, description="Timestamp when the session was started")
    completed_at: datetime | None = Field(None, description="Timestamp when the session was completed")
    created_at: datetime = Field(
        description="Timestamp when the session was created (the timestamp for the initial request)"
    )
    modified_at: datetime = Field(description="Timestamp when the session was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the session was deleted, if applicable")

    @classmethod
    async def from_browser_session(
        cls, browser_session: PersistentBrowserSession, storage: BaseStorage | None = None
    ) -> BrowserSessionResponse:
        """
        Creates a BrowserSessionResponse from a PersistentBrowserSession object.

        Args:
            browser_session: The persistent browser session to convert

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
            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    downloaded_files = await storage.get_shared_downloaded_files_in_browser_session(
                        organization_id=browser_session.organization_id,
                        browser_session_id=browser_session.persistent_browser_session_id,
                    )
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout getting downloaded files", browser_session_id=browser_session.persistent_browser_session_id
                )

            try:
                async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                    recordings = await storage.get_shared_recordings_in_browser_session(
                        organization_id=browser_session.organization_id,
                        browser_session_id=browser_session.persistent_browser_session_id,
                    )
            except asyncio.TimeoutError:
                LOG.warning(
                    "Timeout getting recordings", browser_session_id=browser_session.persistent_browser_session_id
                )

            # Sort downloaded files by modified_at in descending order (newest first)
            downloaded_files.sort(key=lambda x: x.modified_at or datetime.min, reverse=True)
            # Sort recordings by modified_at in descending order (newest first)
            recordings.sort(key=lambda x: x.modified_at or datetime.min, reverse=True)

        return cls(
            browser_session_id=browser_session.persistent_browser_session_id,
            organization_id=browser_session.organization_id,
            runnable_type=browser_session.runnable_type,
            runnable_id=browser_session.runnable_id,
            timeout=browser_session.timeout_minutes,
            browser_address=browser_session.browser_address,
            vnc_streaming_supported=True if browser_session.ip_address else False,
            app_url=app_url,
            started_at=browser_session.started_at,
            completed_at=browser_session.completed_at,
            created_at=browser_session.created_at,
            modified_at=browser_session.modified_at,
            deleted_at=browser_session.deleted_at,
            download_path=download_path,
            downloaded_files=downloaded_files,
            recordings=recordings,
        )
