from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession


class BrowserSessionResponse(BaseModel):
    session_id: str
    organization_id: str
    runnable_type: str | None = None
    runnable_id: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    @classmethod
    def from_browser_session(cls, browser_session: PersistentBrowserSession) -> BrowserSessionResponse:
        return cls(
            session_id=browser_session.persistent_browser_session_id,
            organization_id=browser_session.organization_id,
            runnable_type=browser_session.runnable_type,
            runnable_id=browser_session.runnable_id,
            created_at=browser_session.created_at,
            modified_at=browser_session.modified_at,
            deleted_at=browser_session.deleted_at,
        )
