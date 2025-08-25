from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession


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
    started_at: datetime | None = Field(None, description="Timestamp when the session was started")
    completed_at: datetime | None = Field(None, description="Timestamp when the session was completed")
    created_at: datetime = Field(
        description="Timestamp when the session was created (the timestamp for the initial request)"
    )
    modified_at: datetime = Field(description="Timestamp when the session was last modified")
    deleted_at: datetime | None = Field(None, description="Timestamp when the session was deleted, if applicable")

    @classmethod
    def from_browser_session(cls, browser_session: PersistentBrowserSession) -> BrowserSessionResponse:
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
        return cls(
            browser_session_id=browser_session.persistent_browser_session_id,
            organization_id=browser_session.organization_id,
            runnable_type=browser_session.runnable_type,
            runnable_id=browser_session.runnable_id,
            timeout=browser_session.timeout_minutes,
            browser_address=browser_session.browser_address,
            app_url=app_url,
            started_at=browser_session.started_at,
            completed_at=browser_session.completed_at,
            created_at=browser_session.created_at,
            modified_at=browser_session.modified_at,
            deleted_at=browser_session.deleted_at,
        )
