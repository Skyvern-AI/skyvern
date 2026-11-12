import time

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.debug_sessions import DebugSession
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.schemas.browser_session_kind import BrowserSessionKind
from skyvern.schemas.proxy_location import runtime_proxy_location
from skyvern.webeye.persistent_session_errors import BrowserSessionCreditAdmissionRefusal

LOG = structlog.get_logger()


async def create_debug_browser_session(
    *, organization_id: str, user_id: str, workflow_permanent_id: str
) -> PersistentBrowserSession:
    # Look up the workflow's proxy_location so the debug session browser
    # uses the same proxy region the user configured on the workflow.
    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    proxy_location = runtime_proxy_location(workflow.proxy_location)

    browser_start_started_at = time.monotonic()
    try:
        new_browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
            organization_id=organization_id,
            timeout_minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES,
            proxy_location=proxy_location,
            wait_for_startup=settings.ENV != "local",
            needs_live_view=True,
            created_by=user_id,
            session_kind=BrowserSessionKind.editor,
            # Folder access can hide a browser with no debug row only through this workflow id.
            created_for_workflow_permanent_id=workflow_permanent_id,
        )
    except BrowserSessionCreditAdmissionRefusal:
        # Expected: the caller gets the 402 and the org's credits decide the outcome, so there is no defect to page on.
        LOG.warning(
            "Debug session browser startup refused: credits exhausted",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        raise
    except Exception:
        LOG.exception(
            "Debug session browser startup failed",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        raise
    LOG.info(
        "Debug session browser ready",
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        browser_session_id=new_browser_session.persistent_browser_session_id,
        duration_seconds=time.monotonic() - browser_start_started_at,
        proxy_location=str(proxy_location),
    )
    return new_browser_session


async def debug_browser_supports_vnc(browser_session: PersistentBrowserSession) -> bool:
    return (settings.ENV == "local" or bool(browser_session.ip_address or browser_session.browser_address)) and (
        await app.AGENT_FUNCTION.supports_live_view(
            browser_session.persistent_browser_session_id,
            ip_address=browser_session.ip_address,
        )
    )


async def record_debug_session(
    browser_session: PersistentBrowserSession,
    *,
    organization_id: str,
    user_id: str,
    workflow_permanent_id: str,
    vnc_streaming_supported: bool,
) -> DebugSession:
    debug_session = await app.DATABASE.debug.create_debug_session(
        browser_session_id=browser_session.persistent_browser_session_id,
        organization_id=organization_id,
        user_id=user_id,
        workflow_permanent_id=workflow_permanent_id,
        vnc_streaming_supported=vnc_streaming_supported,
    )

    LOG.info(
        "Created new debug session",
        debug_session_id=debug_session.debug_session_id,
        browser_session_id=browser_session.persistent_browser_session_id,
        organization_id=organization_id,
        user_id=user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    # The freshly created session row carries the browser_profile_id already.
    debug_session.pbs_browser_profile_id = browser_session.browser_profile_id
    return debug_session
