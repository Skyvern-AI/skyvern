import asyncio
import typing as t
from datetime import datetime, timedelta, timezone
from functools import partial

import structlog
from fastapi import Depends, HTTPException

from skyvern.config import settings
from skyvern.exceptions import BrowserSessionNotRenewable
from skyvern.forge import app
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.debug_sessions import DebugSession, DebugSessionRuns
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.runs import ProxyLocation

LOG = structlog.get_logger()


@base_router.get(
    "/debug-session/{workflow_permanent_id}",
    include_in_schema=False,
)
async def get_or_create_debug_session_by_user_and_workflow_permanent_id(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> DebugSession:
    """
    `current_user_id` is a unique identifier for a user, but does not map to an
    entity in the database (at time of writing)

    If the debug session does not exist, a new one will be created.

    In addition, the timeout for the debug session's browser session will be
    extended to 4 hours from the time of the request. If the browser session
    cannot be renewed, a new one will be created and assigned to the debug
    session. The browser_session that could not be renewed will be closed.
    """

    debug_session = await app.DATABASE.get_debug_session(
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    if not debug_session:
        LOG.info(
            "Existing debug session not found, created a new one, along with a new browser session",
            organization_id=current_org.organization_id,
            user_id=current_user_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        return await new_debug_session(
            workflow_permanent_id,
            current_org,
            current_user_id,
        )

    LOG.info(
        "Existing debug session found",
        debug_session_id=debug_session.debug_session_id,
        browser_session_id=debug_session.browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    try:
        await app.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session(
            debug_session.browser_session_id,
            current_org.organization_id,
        )
        return debug_session
    except BrowserSessionNotRenewable as ex:
        LOG.info(
            "Browser session was non-renewable; creating a new debug session",
            ex=str(ex),
            debug_session_id=debug_session.debug_session_id,
            browser_session_id=debug_session.browser_session_id,
            organization_id=current_org.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            user_id=current_user_id,
        )

        return await new_debug_session(
            workflow_permanent_id,
            current_org,
            current_user_id,
        )


@base_router.post(
    "/debug-session/{workflow_permanent_id}/new",
    include_in_schema=False,
)
async def new_debug_session(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> DebugSession:
    """
    Create a new debug session, along with a new browser session. If any
    existing debug sessions are found, "complete" them. Then close the browser
    sessions associated with those completed debug sessions.

    Return the new debug session.

    CAVEAT: if an existing debug session for this user is <30s old, then we
    return that instead. This is to curtail damage from browser session
    spamming.
    """

    if current_user_id:
        debug_session = await app.DATABASE.get_latest_debug_session_for_user(
            organization_id=current_org.organization_id,
            user_id=current_user_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        if debug_session:
            now = datetime.now(timezone.utc)
            created_at_utc = (
                debug_session.created_at.replace(tzinfo=timezone.utc)
                if debug_session.created_at.tzinfo is None
                else debug_session.created_at
            )
            if now - created_at_utc < timedelta(seconds=30):
                LOG.info(
                    "Existing debug session is less than 30s old, returning it",
                    debug_session_id=debug_session.debug_session_id,
                    browser_session_id=debug_session.browser_session_id,
                    organization_id=current_org.organization_id,
                    user_id=current_user_id,
                    workflow_permanent_id=workflow_permanent_id,
                )
                return debug_session

    completed_debug_sessions = await app.DATABASE.complete_debug_sessions(
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    LOG.info(
        f"Completed {len(completed_debug_sessions)} pre-existing debug session(s)",
        num_completed_debug_sessions=len(completed_debug_sessions),
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    if completed_debug_sessions:
        closeable_browser_sessions: list[PersistentBrowserSession] = []

        for debug_session in completed_debug_sessions:
            try:
                browser_session = await app.DATABASE.get_persistent_browser_session(
                    debug_session.browser_session_id,
                    current_org.organization_id,
                )
            except NotFoundError:
                browser_session = None

            if browser_session and browser_session.completed_at is None:
                closeable_browser_sessions.append(browser_session)

        LOG.info(
            f"Closing browser {len(closeable_browser_sessions)} browser session(s)",
            organization_id=current_org.organization_id,
            user_id=current_user_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        def handle_close_browser_session_error(
            browser_session_id: str,
            organization_id: str,
            task: asyncio.Task,
        ) -> None:
            if task.exception():
                LOG.error(
                    f"Failed to close session: {task.exception()}",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                )

        for browser_session in closeable_browser_sessions:
            LOG.info(
                "Closing existing browser session for debug session",
                browser_session_id=browser_session.persistent_browser_session_id,
                organization_id=current_org.organization_id,
            )

            # NOTE(jdo): these may fail to actually close on infra, but the user
            # wants (and should get) a new session regardless - so we will just
            # log the error and continue
            task = asyncio.create_task(
                app.PERSISTENT_SESSIONS_MANAGER.close_session(
                    current_org.organization_id,
                    browser_session.persistent_browser_session_id,
                )
            )

            task.add_done_callback(
                partial(
                    handle_close_browser_session_error,
                    browser_session.persistent_browser_session_id,
                    current_org.organization_id,
                )
            )

    new_browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id=current_org.organization_id,
        timeout_minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES,
        proxy_location=ProxyLocation.RESIDENTIAL,
    )

    debug_session = await app.DATABASE.create_debug_session(
        browser_session_id=new_browser_session.persistent_browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
        vnc_streaming_supported=True if new_browser_session.ip_address else False,
        # NOTE(jdo:streaming-local-dev)
        # vnc_streaming_supported=True,
    )

    LOG.info(
        "Created new debug session",
        debug_session_id=debug_session.debug_session_id,
        browser_session_id=new_browser_session.persistent_browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    return debug_session


@base_router.get(
    "/debug-session/{workflow_permanent_id}/block-outputs",
    response_model=dict[str, dict[str, t.Any]],
    include_in_schema=False,
)
async def get_block_outputs_for_debug_session(
    workflow_permanent_id: str,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> dict[str, dict[str, t.Any]]:
    return await app.WORKFLOW_SERVICE.get_block_outputs_for_debug_session(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        version=version,
    )


@base_router.get(
    "/debug-session/{debug_session_id}/runs",
    include_in_schema=False,
)
@base_router.get(
    "/debug-session/{debug_session_id}/runs/",
    include_in_schema=False,
)
async def get_debug_session_runs(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    debug_session_id: str = "",
) -> DebugSessionRuns:
    """Get all debug session runs for the debug_session_id"""

    LOG.info(
        "Fetching runs for debugger",
        debug_session_id=debug_session_id,
        organization_id=current_org.organization_id,
    )

    debug_session = await app.DATABASE.get_debug_session_by_id(
        debug_session_id=debug_session_id,
        organization_id=current_org.organization_id,
    )

    if not debug_session:
        raise HTTPException(status_code=404, detail="Debug session not found")

    runs = await app.DATABASE.get_workflow_runs_by_debug_session_id(
        debug_session_id=debug_session.debug_session_id,
        organization_id=current_org.organization_id,
    )

    return DebugSessionRuns(debug_session=debug_session, runs=runs)
