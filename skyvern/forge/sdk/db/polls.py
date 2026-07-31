import asyncio

from structlog import get_logger

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession

LOG = get_logger(__name__)


async def wait_on_persistent_browser_address(
    db: AgentDB,
    session_id: str,
    organization_id: str,
    timeout: int = 600,
    poll_interval: float = 2,
) -> str | None:
    persistent_browser_session = await await_browser_session(db, session_id, organization_id, timeout, poll_interval)
    return persistent_browser_session.browser_address if persistent_browser_session else None


async def await_browser_session(
    db: AgentDB,
    session_id: str,
    organization_id: str,
    timeout: int = 600,
    poll_interval: float = 2,
) -> PersistentBrowserSession | None:
    try:
        async with asyncio.timeout(timeout):
            while True:
                persistent_browser_session = await db.browser_sessions.get_persistent_browser_session(
                    session_id, organization_id
                )
                if persistent_browser_session is None:
                    raise Exception(f"Persistent browser session not found for {session_id}")

                LOG.debug(
                    "Checking browser readiness",
                    session_id=session_id,
                    is_browser_ready=persistent_browser_session.is_browser_ready,
                )

                if persistent_browser_session.is_browser_ready:
                    return persistent_browser_session

                await asyncio.sleep(poll_interval)
    except asyncio.TimeoutError:
        LOG.warning(f"Browser address not found for persistent browser session {session_id}")

    return None
