import asyncio

from structlog import get_logger

from skyvern.forge.sdk.db.client import AgentDB

LOG = get_logger(__name__)


async def wait_on_persistent_browser_address(db: AgentDB, session_id: str, organization_id: str) -> str | None:
    try:
        async with asyncio.timeout(10 * 60):
            while True:
                persistent_browser_session = await db.get_persistent_browser_session(session_id, organization_id)
                if persistent_browser_session is None:
                    raise Exception(f"Persistent browser session not found for {session_id}")

                LOG.info(
                    "Checking browser address",
                    session_id=session_id,
                    address=persistent_browser_session.browser_address,
                )

                if persistent_browser_session.browser_address:
                    return persistent_browser_session.browser_address

                await asyncio.sleep(2)
    except asyncio.TimeoutError:
        LOG.warning(f"Browser address not found for persistent browser session {session_id}")

    return None
