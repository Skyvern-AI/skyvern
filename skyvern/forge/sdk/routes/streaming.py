import asyncio
import base64
from datetime import datetime

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services.org_auth_service import get_current_org
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

LOG = structlog.get_logger()
websocket_router = APIRouter()
STREAMING_TIMEOUT = 300


@websocket_router.websocket("/tasks/{task_id}")
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
    except Exception:
        LOG.warning("Error while streaming", task_id=task_id, organization_id=organization_id, exc_info=True)
        return
    LOG.info("WebSocket connection closed successfully", task_id=task_id, organization_id=organization_id)
    return


@websocket_router.websocket("/workflow_runs/{workflow_run_id}")
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
    except Exception:
        LOG.exception("WofklowRun Streaming: Error while getting organization", workflow_run_id=workflow_run_id)
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
