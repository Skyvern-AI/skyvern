"""
Provides WS endpoints for streaming screenshots.

Screenshot streaming is created on the basis of one of these database entities:
  - task (run)
  - workflow run

Screenshot streaming is used for a run that is invoked without a browser session.
Otherwise, VNC streaming is used.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.screencast import start_screencast_loop, wait_for_browser_state
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services.org_auth_service import get_current_org
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

LOG = structlog.get_logger()
STREAMING_TIMEOUT = 300


@legacy_base_router.websocket("/stream/tasks/{task_id}")
async def task_stream(
    websocket: WebSocket,
    task_id: str,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    try:
        await websocket.accept()
        if not token and not apikey:
            await websocket.send_text("No valid credential provided")
            return
    except ConnectionClosedOK:
        LOG.info("ConnectionClosedOK error. Streaming won't start")
        return

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        organization_id = organization.organization_id
    except Exception:
        LOG.exception("Error while getting organization", task_id=task_id)
        try:
            await websocket.send_text("Invalid credential provided")
        except ConnectionClosedOK:
            LOG.info("ConnectionClosedOK error while sending invalid credential message")
        return

    LOG.info("Started task streaming", task_id=task_id, organization_id=organization_id)

    if settings.ENV == "local":
        await _local_screencast_for_task(websocket, task_id, organization_id)
        return

    # timestamp last time when streaming activity happens
    last_activity_timestamp = datetime.utcnow()

    try:
        while True:
            # if no activity for 5 minutes, close the connection
            if (datetime.utcnow() - last_activity_timestamp).total_seconds() > STREAMING_TIMEOUT:
                LOG.info(
                    "No activity for 5 minutes. Closing connection", task_id=task_id, organization_id=organization_id
                )
                await websocket.send_json(
                    {
                        "task_id": task_id,
                        "status": "timeout",
                    }
                )
                return

            task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
            if not task:
                LOG.info("Task not found. Closing connection", task_id=task_id, organization_id=organization_id)
                await websocket.send_json(
                    {
                        "task_id": task_id,
                        "status": "not_found",
                    }
                )
                return
            if task.status.is_final():
                LOG.info(
                    "Task is in a final state. Closing connection",
                    task_status=task.status,
                    task_id=task_id,
                    organization_id=organization_id,
                )
                await websocket.send_json(
                    {
                        "task_id": task_id,
                        "status": task.status,
                    }
                )
                return

            if task.status == TaskStatus.running:
                file_name = f"{task_id}.png"
                if task.workflow_run_id:
                    file_name = f"{task.workflow_run_id}.png"
                screenshot = await app.STORAGE.get_streaming_file(organization_id, file_name)
                if screenshot:
                    encoded_screenshot = base64.b64encode(screenshot).decode("utf-8")
                    await websocket.send_json(
                        {
                            "task_id": task_id,
                            "status": task.status,
                            "screenshot": encoded_screenshot,
                        }
                    )
                    last_activity_timestamp = datetime.utcnow()
            await asyncio.sleep(2)

    except ValidationError as e:
        await websocket.send_text(f"Invalid data: {e}")
    except WebSocketDisconnect:
        LOG.info("WebSocket connection closed", task_id=task_id, organization_id=organization_id)
    except ConnectionClosedOK:
        LOG.info("ConnectionClosedOK error while streaming", task_id=task_id, organization_id=organization_id)
        return
    except ConnectionClosedError:
        LOG.warning(
            "ConnectionClosedError while streaming (client likely disconnected)",
            task_id=task_id,
            organization_id=organization_id,
        )
        return
    except Exception:
        LOG.warning("Error while streaming", task_id=task_id, organization_id=organization_id, exc_info=True)
        return
    LOG.info("WebSocket connection closed successfully", task_id=task_id, organization_id=organization_id)
    return


@legacy_base_router.websocket("/stream/workflow_runs/{workflow_run_id}")
async def workflow_run_streaming(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    try:
        await websocket.accept()
        if not token and not apikey:
            await websocket.send_text("No valid credential provided")
            return
    except ConnectionClosedOK:
        LOG.info("WofklowRun Streaming: ConnectionClosedOK error. Streaming won't start")
        return

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        organization_id = organization.organization_id
    except HTTPException:
        LOG.warning(
            "WofklowRun Streaming: Error while getting organization",
            workflow_run_id=workflow_run_id,
            token=token,
        )
        try:
            await websocket.send_text("Invalid credential provided")
        except ConnectionClosedOK:
            LOG.info("WofklowRun Streaming: ConnectionClosedOK error while sending invalid credential message")
        return

    LOG.info(
        "WofklowRun Streaming: Started workflow run streaming",
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if settings.ENV == "local":
        await _local_screencast_for_workflow_run(websocket, workflow_run_id, organization_id)
        return

    # timestamp last time when streaming activity happens
    last_activity_timestamp = datetime.utcnow()

    try:
        while True:
            # if no activity for 5 minutes, close the connection
            if (datetime.utcnow() - last_activity_timestamp).total_seconds() > STREAMING_TIMEOUT:
                LOG.info(
                    "WofklowRun Streaming: No activity for 5 minutes. Closing connection",
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                await websocket.send_json(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "timeout",
                    }
                )
                return

            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            if not workflow_run or workflow_run.organization_id != organization_id:
                LOG.info(
                    "WofklowRun Streaming: Workflow not found",
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                await websocket.send_json(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "not_found",
                    }
                )
                return
            if workflow_run.status in [
                WorkflowRunStatus.completed,
                WorkflowRunStatus.failed,
                WorkflowRunStatus.terminated,
            ]:
                LOG.info(
                    "Workflow run is in a final state. Closing connection",
                    workflow_run_status=workflow_run.status,
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                )
                await websocket.send_json(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": workflow_run.status,
                    }
                )
                return

            if workflow_run.status == WorkflowRunStatus.running:
                file_name = f"{workflow_run_id}.png"
                screenshot = await app.STORAGE.get_streaming_file(organization_id, file_name)
                if screenshot:
                    encoded_screenshot = base64.b64encode(screenshot).decode("utf-8")
                    await websocket.send_json(
                        {
                            "workflow_run_id": workflow_run_id,
                            "status": workflow_run.status,
                            "screenshot": encoded_screenshot,
                        }
                    )
                    last_activity_timestamp = datetime.utcnow()
            await asyncio.sleep(2)

    except ValidationError as e:
        await websocket.send_text(f"Invalid data: {e}")
    except WebSocketDisconnect:
        LOG.info(
            "WofklowRun Streaming: WebSocket connection closed",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    except ConnectionClosedOK:
        LOG.info(
            "WofklowRun Streaming: ConnectionClosedOK error while streaming",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return
    except ConnectionClosedError:
        LOG.warning(
            "WofklowRun Streaming: ConnectionClosedError while streaming (client likely disconnected)",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return
    except Exception:
        LOG.warning(
            "WofklowRun Streaming: Error while streaming",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return
    LOG.info(
        "WofklowRun Streaming: WebSocket connection closed successfully",
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    return


@base_router.websocket("/stream/browser_sessions/{browser_session_id}")
async def browser_session_streaming(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    try:
        await websocket.accept()
        if not token and not apikey:
            await websocket.send_text("No valid credential provided")
            return
    except ConnectionClosedOK:
        LOG.info("BrowserSession Streaming: ConnectionClosedOK error. Streaming won't start")
        return

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        organization_id = organization.organization_id
    except Exception:
        LOG.exception("Error while getting organization", browser_session_id=browser_session_id)
        try:
            await websocket.send_text("Invalid credential provided")
        except ConnectionClosedOK:
            LOG.info("BrowserSession Streaming: ConnectionClosedOK error while sending invalid credential message")
        return

    LOG.info(
        "BrowserSession Streaming: Started",
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if settings.ENV == "local":
        await _local_screencast_for_browser_session(websocket, browser_session_id, organization_id)
        return

    await websocket.close(code=4001, reason="use-vnc-streaming")
    return


async def _send_status(websocket: WebSocket, id_key: str, entity_id: str, status: str) -> None:
    await websocket.send_json({id_key: entity_id, "status": status})


async def _run_local_screencast(
    websocket: WebSocket,
    entity_id: str,
    entity_type: str,
    id_key: str,
    wait_for_running: Callable[[], Awaitable[str | None]],
    check_finalized: Callable[[], Awaitable[bool]],
    get_current_status: Callable[[], Awaitable[str | None]],
    get_workflow_run_id: Callable[[], str | None] | None = None,
) -> None:
    """Shared logic for local CDP screencast streaming."""
    try:
        early_exit_status = await wait_for_running()
        if early_exit_status is not None:
            await _send_status(websocket, id_key, entity_id, early_exit_status)
            return

        workflow_run_id = get_workflow_run_id() if get_workflow_run_id else None
        browser_state = await wait_for_browser_state(
            entity_id,
            entity_type,
            workflow_run_id=workflow_run_id,
        )
        if browser_state is None:
            LOG.warning("Timed out waiting for browser state", **{id_key: entity_id})
            await _send_status(websocket, id_key, entity_id, "timeout")
            return

        await start_screencast_loop(
            websocket=websocket,
            browser_state=browser_state,
            entity_id=entity_id,
            entity_type=entity_type,
            check_finalized=check_finalized,
        )

        final_status = await get_current_status()
        if final_status is not None:
            await _send_status(websocket, id_key, entity_id, final_status)

    except (WebSocketDisconnect, ConnectionClosedOK):
        LOG.info("WebSocket closed during local screencast", **{id_key: entity_id})
    except ConnectionClosedError:
        LOG.warning("WebSocket connection error during local screencast", **{id_key: entity_id})
    except Exception:
        LOG.warning("Error in local screencast", **{id_key: entity_id}, exc_info=True)


async def _local_screencast_for_workflow_run(
    websocket: WebSocket,
    workflow_run_id: str,
    organization_id: str,
) -> None:
    async def wait_for_running() -> str | None:
        while True:
            workflow_run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            if not workflow_run or workflow_run.organization_id != organization_id:
                return "not_found"
            if workflow_run.status.is_final():
                return workflow_run.status
            if workflow_run.status == WorkflowRunStatus.running:
                return None
            await asyncio.sleep(1)

    async def check_finalized() -> bool:
        wr = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return wr is None or wr.status.is_final()

    async def get_current_status() -> str | None:
        wr = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return wr.status if wr else None

    await _run_local_screencast(
        websocket=websocket,
        entity_id=workflow_run_id,
        entity_type="workflow_run",
        id_key="workflow_run_id",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
    )


async def _local_screencast_for_task(
    websocket: WebSocket,
    task_id: str,
    organization_id: str,
) -> None:
    task_workflow_run_id: str | None = None

    async def wait_for_running() -> str | None:
        nonlocal task_workflow_run_id
        while True:
            task = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
            if not task:
                return "not_found"
            if task.status.is_final():
                return task.status
            if task.status == TaskStatus.running:
                task_workflow_run_id = task.workflow_run_id
                return None
            await asyncio.sleep(1)

    async def check_finalized() -> bool:
        t = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
        return t is None or t.status.is_final()

    async def get_current_status() -> str | None:
        t = await app.DATABASE.get_task(task_id=task_id, organization_id=organization_id)
        return t.status if t else None

    await _run_local_screencast(
        websocket=websocket,
        entity_id=task_id,
        entity_type="task",
        id_key="task_id",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
        get_workflow_run_id=lambda: task_workflow_run_id,
    )


async def _local_screencast_for_browser_session(
    websocket: WebSocket,
    browser_session_id: str,
    organization_id: str,
) -> None:
    async def wait_for_running() -> str | None:
        session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=browser_session_id,
            organization_id=organization_id,
        )
        if not session:
            LOG.warning(
                "Browser session not found for organization",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
            )
            return "not_found"
        if is_final_status(session.status):
            return session.status
        return None

    async def check_finalized() -> bool:
        s = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=browser_session_id,
            organization_id=organization_id,
        )
        return s is None or is_final_status(s.status)

    async def get_current_status() -> str | None:
        s = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=browser_session_id,
            organization_id=organization_id,
        )
        return s.status if s else None

    await _run_local_screencast(
        websocket=websocket,
        entity_id=browser_session_id,
        entity_type="browser_session",
        id_key="browser_session_id",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
    )
