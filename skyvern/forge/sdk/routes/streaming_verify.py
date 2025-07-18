import asyncio
from datetime import datetime

import structlog

import skyvern.forge.sdk.routes.streaming_clients as sc
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

LOG = structlog.get_logger()


async def verify_browser_session(
    browser_session_id: str,
    organization_id: str,
) -> AddressablePersistentBrowserSession | None:
    """
    Verify the browser session exists, and is usable.
    """

    if settings.ENV == "local":
        dummy_browser_session = AddressablePersistentBrowserSession(
            persistent_browser_session_id=browser_session_id,
            organization_id=organization_id,
            browser_address="0.0.0.0:9223",
            created_at=datetime.now(),
            modified_at=datetime.now(),
        )

        return dummy_browser_session

    browser_session = await app.DATABASE.get_persistent_browser_session(browser_session_id, organization_id)

    if not browser_session:
        LOG.info(
            "No browser session found.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        return None

    browser_address = browser_session.browser_address

    if not browser_address:
        LOG.info(
            "Waiting for browser session address.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )

        try:
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

    task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)

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

    if not browser_session.browser_address:
        LOG.info("Browser session address not found for task.", task_id=task_id, organization_id=organization_id)
        return task, None

    try:
        addressable_browser_session = AddressablePersistentBrowserSession(
            **browser_session.model_dump() | {"browser_address": browser_session.browser_address},
        )
    except Exception as e:
        LOG.error(
            "streaming-vnc.browser-session-reify-error", task_id=task_id, organization_id=organization_id, error=e
        )
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

    if settings.ENV == "local":
        dummy_workflow_run = WorkflowRun(
            workflow_id="123",
            workflow_permanent_id="wpid_123",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            status=WorkflowRunStatus.running,
            created_at=datetime.now(),
            modified_at=datetime.now(),
        )

        dummy_browser_session = AddressablePersistentBrowserSession(
            persistent_browser_session_id=workflow_run_id,
            organization_id=organization_id,
            browser_address="0.0.0.0:9223",
            created_at=datetime.now(),
            modified_at=datetime.now(),
        )

        return dummy_workflow_run, dummy_browser_session

    workflow_run = await app.DATABASE.get_workflow_run(
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

    if workflow_run.status not in [WorkflowRunStatus.created, WorkflowRunStatus.queued, WorkflowRunStatus.running]:
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

    if not browser_address:
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


async def loop_verify_browser_session(verifiable: sc.CommandChannel | sc.Streaming) -> None:
    """
    Loop until the browser session is cleared or the websocket is closed.
    """

    while verifiable.browser_session and verifiable.is_open:
        browser_session = await verify_browser_session(
            browser_session_id=verifiable.browser_session.persistent_browser_session_id,
            organization_id=verifiable.organization_id,
        )

        verifiable.browser_session = browser_session

        await asyncio.sleep(2)


async def loop_verify_task(streaming: sc.Streaming) -> None:
    """
    Loop until the task is cleared or the websocket is closed.
    """

    while streaming.task and streaming.is_open:
        task, browser_session = await verify_task(
            task_id=streaming.task.task_id,
            organization_id=streaming.organization_id,
        )

        streaming.task = task
        streaming.browser_session = browser_session

        await asyncio.sleep(2)


async def loop_verify_workflow_run(verifiable: sc.CommandChannel | sc.Streaming) -> None:
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

        await asyncio.sleep(2)
