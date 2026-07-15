"""
Channels (see ./channels/README.md) variously rely on the state of one of
these database entities:
  - browser session
  - task
  - workflow run

That is, channels are created on the basis of one of those entities, and that
entity must be in a valid state for the channel to continue.

So, in order to continue operating a channel, we need to periodically verify
that the underlying entity is still valid. This module provides logic to
perform those verifications.
"""

from __future__ import annotations

import asyncio
import typing as t
from datetime import datetime, timedelta

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    AddressablePersistentBrowserSession,
    PersistentBrowserSession,
    is_final_status,
)
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

if t.TYPE_CHECKING:
    from skyvern.forge.sdk.routes.streaming.channels.message import MessageChannel
    from skyvern.forge.sdk.routes.streaming.channels.vnc import VncChannel

LOG = structlog.get_logger()


class Constants:
    POLL_INTERVAL_FOR_VERIFICATION_SECONDS = 5


def _has_addressless_local_vnc_metadata(browser_session: PersistentBrowserSession) -> bool:
    return (
        settings.BROWSER_STREAMING_MODE == "vnc"
        and manager_has_local_vnc_ownership_capability()
        and not browser_session.browser_address
        and (browser_session.display_number is not None or browser_session.vnc_port is not None)
    )


def manager_has_local_vnc_ownership_capability() -> bool:
    """Return whether the configured manager can authorize process-local VNC routing."""

    return callable(getattr(app.PERSISTENT_SESSIONS_MANAGER, "owns_local_vnc_stack", None))


def manager_owns_local_vnc_session(
    browser_session: PersistentBrowserSession,
    organization_id: str,
) -> bool:
    """Validate addressless local routing metadata against process ownership.

    This capability is intentionally discovered at runtime: only the OSS default
    manager owns process-local VNC stacks.  Managers without the capability cannot
    authorize localhost routing, while their ordinary address-based paths remain
    unchanged.
    """

    if (
        browser_session.organization_id != organization_id
        or browser_session.display_number is None
        or browser_session.vnc_port is None
    ):
        return False
    capability = getattr(app.PERSISTENT_SESSIONS_MANAGER, "owns_local_vnc_stack", None)
    if not callable(capability):
        return False
    return (
        capability(
            session_id=browser_session.persistent_browser_session_id,
            organization_id=organization_id,
            display_number=browser_session.display_number,
            vnc_port=browser_session.vnc_port,
        )
        is True
    )


def _log_unowned_local_vnc_session(
    browser_session: PersistentBrowserSession,
    organization_id: str,
    **identity: str,
) -> None:
    LOG.warning(
        "Rejecting stale or unowned local VNC routing metadata.",
        browser_session_id=browser_session.persistent_browser_session_id,
        organization_id=organization_id,
        display_number=browser_session.display_number,
        vnc_port=browser_session.vnc_port,
        **identity,
    )


async def verify_browser_session(
    browser_session_id: str,
    organization_id: str,
) -> AddressablePersistentBrowserSession | None:
    """
    Verify the browser session exists, and is usable.
    """
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(browser_session_id, organization_id)

    if not browser_session:
        LOG.info(
            "No browser session found.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        return None

    if is_final_status(browser_session.status):
        return None

    started_at = browser_session.started_at
    timeout_minutes = browser_session.timeout_minutes

    if started_at and timeout_minutes:
        current_time = datetime.utcnow()
        times_out_at = started_at + timedelta(minutes=timeout_minutes)

        if current_time > times_out_at:
            LOG.info(
                "Browser session invalid, as it has timed out, but is still in a non-final status. This is likely a bug!",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
                timeout_minutes=timeout_minutes,
                started_at=started_at.isoformat(),
                now=current_time.isoformat(),
                times_out_at=times_out_at.isoformat(),
            )
            return None

    browser_address = browser_session.browser_address

    if not browser_address:
        if settings.BROWSER_STREAMING_MODE == "cdp":
            browser_address = ""
        elif _has_addressless_local_vnc_metadata(browser_session):
            if not manager_owns_local_vnc_session(browser_session, organization_id):
                _log_unowned_local_vnc_session(browser_session, organization_id)
                return None
            browser_address = ""
        else:
            LOG.info(
                "Checking browser session address readiness.",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
            )

            try:
                if settings.ENV == "local":
                    browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address_if_ready(
                        session_id=browser_session_id,
                        organization_id=organization_id,
                    )
                else:
                    browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address(
                        session_id=browser_session_id,
                        organization_id=organization_id,
                    )
            except Exception as ex:
                LOG.info(
                    "Browser session address not found for browser session.",
                    browser_session_id=browser_session_id,
                    organization_id=organization_id,
                    ex=ex,
                )
                return None
            if not browser_address:
                return None

    try:
        addressable_browser_session = AddressablePersistentBrowserSession(
            **browser_session.model_dump() | {"browser_address": browser_address},
        )
    except Exception:
        return None

    return addressable_browser_session


async def verify_task(
    task_id: str, organization_id: str
) -> tuple[Task | None, AddressablePersistentBrowserSession | None]:
    """
    Verify the task is running, and that it has a browser session associated
    with it.
    """

    task = await app.DATABASE.tasks.get_task(task_id=task_id, organization_id=organization_id)

    if not task:
        LOG.info("Task not found.", task_id=task_id, organization_id=organization_id)
        return None, None

    if task.status.is_final():
        LOG.info("Task is in a final state.", task_status=task.status, task_id=task_id, organization_id=organization_id)

        return None, None

    if task.status not in [TaskStatus.created, TaskStatus.queued, TaskStatus.running]:
        LOG.info(
            "Task is not created, queued, or running.",
            task_status=task.status,
            task_id=task_id,
            organization_id=organization_id,
        )

        return None, None

    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(
        organization_id=organization_id,
        runnable_id=task_id,
    )

    if not browser_session:
        LOG.info("No browser session found for task.", task_id=task_id, organization_id=organization_id)
        return task, None

    browser_address = browser_session.browser_address
    if not browser_address and _has_addressless_local_vnc_metadata(browser_session):
        if not manager_owns_local_vnc_session(browser_session, organization_id):
            _log_unowned_local_vnc_session(browser_session, organization_id, task_id=task_id)
            return task, None
        browser_address = ""
    elif not browser_address:
        LOG.info("Browser session address not found for task.", task_id=task_id, organization_id=organization_id)
        return task, None

    try:
        addressable_browser_session = AddressablePersistentBrowserSession(
            **browser_session.model_dump() | {"browser_address": browser_address},
        )
    except Exception as e:
        LOG.error("vnc.browser-session-reify-error", task_id=task_id, organization_id=organization_id, error=e)
        return task, None

    return task, addressable_browser_session


async def verify_workflow_run(
    workflow_run_id: str,
    organization_id: str,
) -> tuple[WorkflowRun | None, AddressablePersistentBrowserSession | None]:
    """
    Verify the workflow run is running, and that it has a browser session associated
    with it.
    """

    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info("Workflow run not found.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        return None, None

    if workflow_run.status.is_final():
        LOG.info(
            "Workflow run is in a final state. Closing connection.",
            workflow_run_status=workflow_run.status,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        return None, None

    if workflow_run.status not in [
        WorkflowRunStatus.created,
        WorkflowRunStatus.queued,
        WorkflowRunStatus.running,
        WorkflowRunStatus.paused,
    ]:
        LOG.info(
            "Workflow run is not running.",
            workflow_run_status=workflow_run.status,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        return None, None

    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(
        organization_id=organization_id,
        runnable_id=workflow_run_id,
    )

    if not browser_session:
        LOG.info(
            "No browser session found for workflow run.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return workflow_run, None

    browser_address = browser_session.browser_address

    if not browser_address and _has_addressless_local_vnc_metadata(browser_session):
        if not manager_owns_local_vnc_session(browser_session, organization_id):
            _log_unowned_local_vnc_session(browser_session, organization_id, workflow_run_id=workflow_run_id)
            return workflow_run, None
        browser_address = ""
    elif not browser_address:
        LOG.info(
            "Waiting for browser session address.", workflow_run_id=workflow_run_id, organization_id=organization_id
        )

        try:
            browser_address = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address(
                session_id=browser_session.persistent_browser_session_id,
                organization_id=organization_id,
            )
        except Exception as ex:
            LOG.info(
                "Browser session address not found for workflow run.",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                ex=ex,
            )
            return workflow_run, None

    try:
        addressable_browser_session = AddressablePersistentBrowserSession(
            **browser_session.model_dump() | {"browser_address": browser_address},
        )
    except Exception:
        return workflow_run, None

    return workflow_run, addressable_browser_session


async def loop_verify_browser_session(verifiable: MessageChannel | VncChannel) -> None:
    """
    Loop until the browser session is cleared or the websocket is closed.
    """

    while verifiable.browser_session and verifiable.is_open:
        browser_session = await verify_browser_session(
            browser_session_id=verifiable.browser_session.persistent_browser_session_id,
            organization_id=verifiable.organization_id,
        )

        verifiable.browser_session = browser_session
        if hasattr(verifiable, "refresh_interactor_from_browser_session"):
            verifiable.refresh_interactor_from_browser_session()

        await asyncio.sleep(Constants.POLL_INTERVAL_FOR_VERIFICATION_SECONDS)


async def loop_verify_task(vnc_channel: VncChannel) -> None:
    """
    Loop until the task is cleared or the websocket is closed.
    """

    while vnc_channel.task and vnc_channel.is_open:
        task, browser_session = await verify_task(
            task_id=vnc_channel.task.task_id,
            organization_id=vnc_channel.organization_id,
        )

        vnc_channel.task = task
        vnc_channel.browser_session = browser_session
        vnc_channel.refresh_interactor_from_browser_session()

        await asyncio.sleep(Constants.POLL_INTERVAL_FOR_VERIFICATION_SECONDS)


async def loop_verify_workflow_run(verifiable: MessageChannel | VncChannel) -> None:
    """
    Loop until the workflow run is cleared or the websocket is closed.
    """

    while verifiable.workflow_run and verifiable.is_open:
        workflow_run, browser_session = await verify_workflow_run(
            workflow_run_id=verifiable.workflow_run.workflow_run_id,
            organization_id=verifiable.organization_id,
        )

        verifiable.workflow_run = workflow_run
        verifiable.browser_session = browser_session
        if hasattr(verifiable, "refresh_interactor_from_browser_session"):
            verifiable.refresh_interactor_from_browser_session()

        await asyncio.sleep(Constants.POLL_INTERVAL_FOR_VERIFICATION_SECONDS)
