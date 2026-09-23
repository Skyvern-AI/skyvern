import asyncio
import hashlib
import typing as t
from datetime import datetime, timedelta, timezone
from functools import partial

import structlog
from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError

from skyvern.config import settings
from skyvern.exceptions import BrowserSessionNotRenewable
from skyvern.forge import app
from skyvern.forge.sdk.copilot.active_run_session import get_active_run_session
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.debug_sessions import (
    DebugLoginBlockCompatibility,
    DebugSession,
    DebugSessionPrewarmRequest,
    DebugSessionRuns,
    DebugSessionViewerState,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.block import LoginBlock
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import (
    DEBUG_SESSION_PROFILE_REASON_DIFFERENT,
    DEBUG_SESSION_PROFILE_REASON_NO_PROFILE,
)
from skyvern.schemas.proxy_location import runtime_proxy_location

LOG = structlog.get_logger()
PREWARM_BOUND_WORKFLOW_PERMANENT_ID = "debug-session-prewarm"
PREWARM_PENDING_RUNNABLE_TYPE = "debug_session_prewarm_pending"
PREWARM_DISPATCHED_RUNNABLE_TYPE = "debug_session_prewarm_dispatched"
BROWSER_SESSION_PREWARM_FLAG = "BROWSER_SESSION_PREWARM"


def _prewarm_bound_key(organization_id: str, user_id: str) -> str:
    identity = f"{len(organization_id)}:{organization_id}{len(user_id)}:{user_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


async def _hydrate_pbs_browser_profile_id(
    debug_session: DebugSession,
    organization_id: str,
) -> DebugSession:
    """Populate the visible PBS's saved browser_profile_id on the response.

    Looked up lazily here (rather than carried on the debug_sessions row) so
    profile rotations after session creation are reflected at fetch time.
    The UI keys on this value to detect LoginBlock credential-profile
    incompatibility before kicking off a debug run.
    """
    try:
        pbs = await app.DATABASE.browser_sessions.get_persistent_browser_session(
            debug_session.browser_session_id,
            organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to hydrate pbs_browser_profile_id on debug session",
            debug_session_id=debug_session.debug_session_id,
            browser_session_id=debug_session.browser_session_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return debug_session
    if pbs is not None and debug_session.pbs_browser_profile_id is None:
        debug_session.pbs_browser_profile_id = pbs.browser_profile_id
    return debug_session


async def _claim_compatible_prewarm(
    *,
    workflow_permanent_id: str,
    organization_id: str,
    user_id: str,
) -> DebugSession | None:
    bound_key = _prewarm_bound_key(organization_id, user_id)
    browser_session = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
        organization_id=organization_id,
        workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        bound_key=bound_key,
    )
    if browser_session is None:
        return None
    # create_session inserts its row before infrastructure routing and Temporal
    # dispatch finish. The POST publishes this marker only after dispatch returns.
    if browser_session.runnable_type != PREWARM_DISPATCHED_RUNNABLE_TYPE:
        return None

    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    if browser_session.proxy_location != runtime_proxy_location(workflow.proxy_location):
        await _retire_prewarm(browser_session, organization_id, bound_key)
        return None

    if browser_session.started_at is not None:
        try:
            browser_session = await app.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session(
                browser_session.persistent_browser_session_id,
                organization_id,
            )
        except BrowserSessionNotRenewable:
            LOG.info(
                "Rejected expiring debug-session prewarm",
                organization_id=organization_id,
                browser_session_id=browser_session.persistent_browser_session_id,
            )
            return None

        fixed_deadline_seconds = await app.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline(
            browser_session.persistent_browser_session_id,
            organization_id,
        )
        # A fixed-deadline vendor browser cannot be renewed. Require almost a
        # full fresh debug window so adoption cannot hand the editor a session
        # that expires shortly after it opens.
        minimum_lifetime_seconds = settings.DEBUG_SESSION_TIMEOUT_MINUTES * 60 - 60
        if fixed_deadline_seconds is not None and fixed_deadline_seconds < minimum_lifetime_seconds:
            LOG.info(
                "Rejected short-lived debug-session prewarm",
                organization_id=organization_id,
                browser_session_id=browser_session.persistent_browser_session_id,
                remaining_seconds=fixed_deadline_seconds,
            )
            await _retire_prewarm(browser_session, organization_id, bound_key)
            return None

    released = await app.DATABASE.browser_sessions.clear_prewarm_binding(
        session_id=browser_session.persistent_browser_session_id,
        organization_id=organization_id,
        expected_bound_workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        expected_bound_key=bound_key,
    )
    if not released:
        # A concurrent workflow request for this user may have won the binding CAS
        # and still be inserting its DebugSession. Give that tiny transaction
        # gap a bounded chance to settle before falling back to a second PBS.
        for _ in range(3):
            claimed = await app.DATABASE.debug.get_debug_session(
                organization_id=organization_id,
                user_id=user_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            if claimed is not None:
                return await _hydrate_pbs_browser_profile_id(claimed, organization_id)
            await asyncio.sleep(0.05)
        return None

    try:
        claimed = await app.DATABASE.debug.create_debug_session(
            browser_session_id=browser_session.persistent_browser_session_id,
            organization_id=organization_id,
            user_id=user_id,
            workflow_permanent_id=workflow_permanent_id,
            # Prewarms request live-view-capable infrastructure. Capability
            # metadata may not exist yet if this GET wins a race with the
            # asynchronous POST dispatch, so do not persist a false negative.
            vnc_streaming_supported=True,
        )
    except Exception:
        try:
            await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                organization_id,
                browser_session.persistent_browser_session_id,
            )
        except Exception:
            LOG.warning(
                "Failed to close prewarm after debug-session claim failed",
                organization_id=organization_id,
                browser_session_id=browser_session.persistent_browser_session_id,
                exc_info=True,
            )
        raise

    claimed.pbs_browser_profile_id = browser_session.browser_profile_id
    LOG.info(
        "Claimed prewarmed debug session",
        organization_id=organization_id,
        debug_session_id=claimed.debug_session_id,
        browser_session_id=claimed.browser_session_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    return claimed


async def _retire_prewarm(
    browser_session: PersistentBrowserSession,
    organization_id: str,
    bound_key: str,
) -> None:
    released = await app.DATABASE.browser_sessions.clear_prewarm_binding(
        session_id=browser_session.persistent_browser_session_id,
        organization_id=organization_id,
        expected_bound_workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        expected_bound_key=bound_key,
    )
    if not released:
        return
    try:
        await app.PERSISTENT_SESSIONS_MANAGER.close_session(
            organization_id,
            browser_session.persistent_browser_session_id,
        )
    except Exception:
        LOG.warning(
            "Failed to close retired debug-session prewarm",
            organization_id=organization_id,
            browser_session_id=browser_session.persistent_browser_session_id,
            exc_info=True,
        )


@base_router.post(
    "/debug-session/prewarm",
    include_in_schema=False,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prewarm_debug_session(
    request: DebugSessionPrewarmRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> Response:
    organization_id = current_org.organization_id
    try:
        enabled = await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
            BROWSER_SESSION_PREWARM_FLAG,
            organization_id,
            properties={"organization_id": organization_id},
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate browser-session prewarm flag",
            organization_id=organization_id,
            exc_info=True,
        )
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if enabled is not True:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    proxy_location = runtime_proxy_location(request.proxy_location)
    bound_key = _prewarm_bound_key(organization_id, current_user_id)
    existing = await app.DATABASE.browser_sessions.get_live_bound_persistent_browser_session(
        organization_id=organization_id,
        workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        bound_key=bound_key,
    )
    if existing is not None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    try:
        browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
            organization_id=organization_id,
            timeout_minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES,
            proxy_location=proxy_location,
            bound_workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
            bound_key=bound_key,
            runnable_type=PREWARM_PENDING_RUNNABLE_TYPE,
            wait_for_startup=False,
            needs_live_view=True,
        )
    except IntegrityError:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    except Exception as error:
        LOG.info(
            "Failed to start debug-session prewarm",
            organization_id=organization_id,
            error_type=type(error).__name__,
        )
        return Response(status_code=status.HTTP_202_ACCEPTED)
    try:
        dispatched = await app.DATABASE.browser_sessions.mark_prewarm_dispatched(
            session_id=browser_session.persistent_browser_session_id,
            organization_id=organization_id,
            expected_bound_workflow_permanent_id=PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
            expected_bound_key=bound_key,
            expected_runnable_type=PREWARM_PENDING_RUNNABLE_TYPE,
            dispatched_runnable_type=PREWARM_DISPATCHED_RUNNABLE_TYPE,
        )
    except Exception as error:
        LOG.info(
            "Failed to publish debug-session prewarm",
            organization_id=organization_id,
            browser_session_id=browser_session.persistent_browser_session_id,
            error_type=type(error).__name__,
        )
        dispatched = False
    if not dispatched:
        try:
            await app.PERSISTENT_SESSIONS_MANAGER.close_session(
                organization_id,
                browser_session.persistent_browser_session_id,
            )
        except Exception:
            LOG.warning(
                "Failed to close unpublished debug-session prewarm",
                organization_id=organization_id,
                browser_session_id=browser_session.persistent_browser_session_id,
                exc_info=True,
            )
        return Response(status_code=status.HTTP_202_ACCEPTED)
    LOG.info(
        "Started debug-session prewarm",
        organization_id=organization_id,
        browser_session_id=browser_session.persistent_browser_session_id,
        proxy_location=str(proxy_location),
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@base_router.get(
    "/debug-session/{workflow_permanent_id}/viewer-state",
    include_in_schema=False,
)
async def get_debug_session_viewer_state(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> DebugSessionViewerState:
    """Return the active run viewer target without creating or renewing a debug session."""
    debug_session = await app.DATABASE.debug.get_debug_session(
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if debug_session is None:
        return DebugSessionViewerState()

    try:
        association = await get_active_run_session(
            organization_id=current_org.organization_id,
            debug_browser_session_id=debug_session.browser_session_id,
        )
    except Exception:
        LOG.warning(
            "Failed to read active Copilot run session",
            organization_id=current_org.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            debug_browser_session_id=debug_session.browser_session_id,
            exc_info=True,
        )
        return DebugSessionViewerState()

    if (
        association is None
        or association.organization_id != current_org.organization_id
        or association.workflow_permanent_id != workflow_permanent_id
        or association.debug_browser_session_id != debug_session.browser_session_id
    ):
        return DebugSessionViewerState()

    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=association.workflow_run_id,
        organization_id=current_org.organization_id,
    )
    try:
        run_is_final = workflow_run is not None and WorkflowRunStatus(workflow_run.status).is_final()
    except ValueError:
        return DebugSessionViewerState()
    if (
        workflow_run is None
        or workflow_run.workflow_permanent_id != workflow_permanent_id
        or workflow_run.browser_session_id != association.run_browser_session_id
        or run_is_final
    ):
        return DebugSessionViewerState()

    return DebugSessionViewerState(active_run_session_id=association.run_browser_session_id)


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

    debug_session = await app.DATABASE.debug.get_debug_session(
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    claimed_prewarm = False
    if debug_session is None:
        try:
            debug_session = await _claim_compatible_prewarm(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=current_org.organization_id,
                user_id=current_user_id,
            )
            claimed_prewarm = debug_session is not None
        except Exception:
            LOG.warning(
                "Failed to claim debug-session prewarm; falling back to cold start",
                organization_id=current_org.organization_id,
                workflow_permanent_id=workflow_permanent_id,
                exc_info=True,
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

    if claimed_prewarm:
        return debug_session

    LOG.info(
        "Existing debug session found",
        debug_session_id=debug_session.debug_session_id,
        browser_session_id=debug_session.browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )

    # Skip renewal for sessions that haven't started yet (browser still launching)
    session = await app.DATABASE.browser_sessions.get_persistent_browser_session(
        debug_session.browser_session_id,
        current_org.organization_id,
    )
    # Reuse the already-fetched PBS row to populate pbs_browser_profile_id
    # rather than going through _hydrate_pbs_browser_profile_id, which would
    # round-trip the DB a second time. If the PBS lookup itself raises, that
    # surfaces here intentionally so the renewal block can react.
    if session is not None:
        debug_session.pbs_browser_profile_id = session.browser_profile_id
    if session and session.started_at is None and session.completed_at is None:
        created_at_utc = (
            session.created_at.replace(tzinfo=timezone.utc) if session.created_at.tzinfo is None else session.created_at
        )
        age_seconds = (datetime.now(timezone.utc) - created_at_utc).total_seconds()
        if age_seconds < 120:
            return debug_session

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
        debug_session = await app.DATABASE.debug.get_debug_session(
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
                return await _hydrate_pbs_browser_profile_id(debug_session, current_org.organization_id)

    completed_debug_sessions = await app.DATABASE.debug.complete_debug_sessions(
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

    if completed_debug_sessions and settings.ENV != "local":
        closeable_browser_sessions: list[PersistentBrowserSession] = []

        for debug_session in completed_debug_sessions:
            try:
                browser_session = await app.DATABASE.browser_sessions.get_persistent_browser_session(
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
    elif completed_debug_sessions:
        LOG.info(
            "Local mode: skipping debug-session close tasks before creating replacement browser session",
            num_completed_debug_sessions=len(completed_debug_sessions),
            organization_id=current_org.organization_id,
            user_id=current_user_id,
            workflow_permanent_id=workflow_permanent_id,
        )

    # Look up the workflow's proxy_location so the debug session browser
    # uses the same proxy region the user configured on the workflow.
    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    proxy_location = runtime_proxy_location(workflow.proxy_location)

    new_browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id=current_org.organization_id,
        timeout_minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES,
        proxy_location=proxy_location,
        wait_for_startup=settings.ENV != "local",
        needs_live_view=True,
    )

    debug_session = await app.DATABASE.debug.create_debug_session(
        browser_session_id=new_browser_session.persistent_browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
        vnc_streaming_supported=(
            settings.ENV == "local" or bool(new_browser_session.ip_address or new_browser_session.browser_address)
        )
        and await app.AGENT_FUNCTION.supports_live_view(
            new_browser_session.persistent_browser_session_id,
            ip_address=new_browser_session.ip_address,
        ),
    )

    LOG.info(
        "Created new debug session",
        debug_session_id=debug_session.debug_session_id,
        browser_session_id=new_browser_session.persistent_browser_session_id,
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
        proxy_location=str(proxy_location),
    )

    # The freshly created session row carries the browser_profile_id already.
    debug_session.pbs_browser_profile_id = new_browser_session.browser_profile_id
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
    "/debug-session/{workflow_permanent_id}/login-block-compatibility",
    response_model=DebugLoginBlockCompatibility,
    include_in_schema=False,
)
async def get_login_block_compatibility(
    workflow_permanent_id: str,
    block_label: str = Query(..., min_length=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
    current_user_id: str = Depends(org_auth_service.get_current_user_id),
) -> DebugLoginBlockCompatibility:
    """Backend-authoritative compatibility verdict for one debugger LoginBlock.

    The FE pre-check normally resolves the credential off the bounded
    `useCredentialsQuery` window; if the block references a credential past
    that window (pagination miss / inaccessible), this endpoint runs the same
    org-scoped resolution the run path uses so a Play retry can recover.
    Returns the same shape the FE's compatibility gate consumes.
    """
    debug_session = await app.DATABASE.debug.get_debug_session(
        organization_id=current_org.organization_id,
        user_id=current_user_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if not debug_session:
        return DebugLoginBlockCompatibility(compatible=True, reason=None)

    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
    )
    if workflow is None or workflow.workflow_definition is None:
        return DebugLoginBlockCompatibility(compatible=True, reason=None)

    block = next(
        (b for b in workflow.workflow_definition.blocks if getattr(b, "label", None) == block_label),
        None,
    )
    if block is None or not isinstance(block, LoginBlock):
        return DebugLoginBlockCompatibility(compatible=True, reason=None)

    resolved_profile_id = await app.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run(
        block=block,
        organization_id=current_org.organization_id,
    )
    if resolved_profile_id is None:
        return DebugLoginBlockCompatibility(compatible=True, reason=None)

    pbs = await app.DATABASE.browser_sessions.get_persistent_browser_session(
        debug_session.browser_session_id,
        current_org.organization_id,
    )
    pbs_profile_id = pbs.browser_profile_id if pbs is not None else None

    if pbs_profile_id == resolved_profile_id:
        return DebugLoginBlockCompatibility(compatible=True, reason=None)

    reason = (
        DEBUG_SESSION_PROFILE_REASON_NO_PROFILE if pbs_profile_id is None else DEBUG_SESSION_PROFILE_REASON_DIFFERENT
    )
    return DebugLoginBlockCompatibility(compatible=False, reason=reason)


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

    debug_session = await app.DATABASE.debug.get_debug_session_by_id(
        debug_session_id=debug_session_id,
        organization_id=current_org.organization_id,
    )

    if not debug_session:
        raise HTTPException(status_code=404, detail="Debug session not found")

    runs = await app.DATABASE.debug.get_workflow_runs_by_debug_session_id(
        debug_session_id=debug_session.debug_session_id,
        organization_id=current_org.organization_id,
    )

    return DebugSessionRuns(debug_session=debug_session, runs=runs)
