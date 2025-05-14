from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession


class BrowserSessionResponse(BaseModel):
    """Response model for browser session information."""

    browser_session_id: str = Field(description="Unique identifier for the browser session")
    organization_id: str = Field(description="ID of the organization that owns this session")
    runnable_type: str | None = Field(
        None, description="Type of runnable associated with this session (workflow, task etc)"
    )
    runnable_id: str | None = Field(None, description="ID of the associated runnable")
    timeout: int | None = Field(
        None,
        description="Timeout in minutes for the session. Timeout is applied after the session is started.",
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
        return cls(
            browser_session_id=browser_session.persistent_browser_session_id,
            organization_id=browser_session.organization_id,
            runnable_type=browser_session.runnable_type,
            runnable_id=browser_session.runnable_id,
            timeout=browser_session.timeout_minutes,
            started_at=browser_session.started_at,
            completed_at=browser_session.completed_at,
            created_at=browser_session.created_at,
            modified_at=browser_session.modified_at,
            deleted_at=browser_session.deleted_at,
        )
