import asyncio
import dataclasses
import typing as t
from enum import IntEnum

import structlog
import websockets
from cloud.config import settings
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets import Data
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import legacy_base_router
from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.services.org_auth_service import get_current_org
from skyvern.forge.sdk.utils.aio import collect
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

Interactor = t.Literal["agent", "user"]
Loops = list[asyncio.Task]  # aka "queue-less actors"; or "programs"


class MessageType(IntEnum):
    keyboard = 4
    mouse = 5


LOG = structlog.get_logger()


@dataclasses.dataclass
class Streaming:
    """
    Streaming state.
    """

    interactor: Interactor
    """
    Whether the user or the agent are the interactor.
    """

    organization_id: str
    vnc_port: int
    websocket: WebSocket

    # --

    browser_session: AddressablePersistentBrowserSession | None = None
    task: Task | None = None
    workflow_run: WorkflowRun | None = None

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state not in (WebSocketState.CONNECTED, WebSocketState.CONNECTING):
            return False

        if not self.task and not self.workflow_run:
            return False

        return True

    async def close(self, code: int = 1000, reason: str | None = None) -> "Streaming":
        LOG.info("Closing Streaming.", reason=reason, code=code)

        self.browser_session = None
        self.task = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        return self


async def auth(apikey: str | None, token: str | None, websocket: WebSocket) -> str | None:
    """
    Accepts the websocket connection.

    Authenticates the user; cannot proceed with WS connection if an organization_id cannot be
    determined.
    """

    try:
        await websocket.accept()
        if not token and not apikey:
            await websocket.close(code=1002)
            return None
    except ConnectionClosedOK:
        LOG.info("WebSocket connection closed cleanly.")
        return None

    try:
        organization = await get_current_org(x_api_key=apikey, authorization=token)
        organization_id = organization.organization_id

        if not organization_id:
            await websocket.close(code=1002)
            return None
    except Exception:
        LOG.exception("Error occurred while retrieving organization information.")
        try:
            await websocket.close(code=1002)
        except ConnectionClosedOK:
            LOG.info("WebSocket connection closed due to invalid credentials.")
        return None

    return organization_id


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

    if not task.status == TaskStatus.running:
        LOG.info("Task is not running.", task_status=task.status, task_id=task_id, organization_id=organization_id)

        return None, None

    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(
        organization_id=organization_id,
        runnable_id=task_id,  # is this correct; is there a task_run_id?
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


async def get_streaming_for_task(
    task_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[Streaming, Loops] | None:
    """
    Return a streaming context for a task, with a list of loops to run concurrently.
    """

    task, browser_session = await verify_task(task_id=task_id, organization_id=organization_id)

    if not task:
        LOG.info("No initial task found.", task_id=task_id, organization_id=organization_id)
        return None

    if not browser_session:
        LOG.info("No initial browser session found for task.", task_id=task_id, organization_id=organization_id)
        return None

    streaming = Streaming(
        interactor="user",
        organization_id=organization_id,
        vnc_port=settings.PERSISTENT_BROWSER_VNC_PORT,
        websocket=websocket,
        # --
        browser_session=browser_session,
        task=task,
    )

    loops = [
        asyncio.create_task(loop_verify_task(streaming)),
        asyncio.create_task(loop_stream_vnc(streaming)),
    ]

    return streaming, loops


async def get_streaming_for_workflow_run(
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[Streaming, Loops] | None:
    """
    Return a streaming context for a workflow run, with a list of loops to run concurrently.
    """

    LOG.info("Getting streaming context for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info("No initial workflow run found.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        return None

    if not browser_session:
        LOG.info(
            "No initial browser session found for workflow run.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    streaming = Streaming(
        interactor="user",
        organization_id=organization_id,
        vnc_port=settings.PERSISTENT_BROWSER_VNC_PORT,
        # --
        browser_session=browser_session,
        workflow_run=workflow_run,
        websocket=websocket,
    )

    loops = [
        asyncio.create_task(loop_verify_workflow_run(streaming)),
        asyncio.create_task(loop_stream_vnc(streaming)),
    ]

    return streaming, loops


async def verify_workflow_run(
    workflow_run_id: str,
    organization_id: str,
) -> tuple[WorkflowRun | None, AddressablePersistentBrowserSession | None]:
    """
    Verify the workflow run is running, and that it has a browser session associated
    with it.
    """

    workflow_run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info("Workflow run not found.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        return None, None

    if workflow_run.status in [
        WorkflowRunStatus.completed,
        WorkflowRunStatus.failed,
        WorkflowRunStatus.terminated,
    ]:
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
            _, host, cdp_port = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_address(
                session_id=browser_session.persistent_browser_session_id,
                organization_id=organization_id,
            )
            browser_address = f"{host}:{cdp_port}"
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


async def loop_verify_task(streaming: Streaming) -> None:
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


async def loop_verify_workflow_run(streaming: Streaming) -> None:
    """
    Loop until the workflow run is cleared or the websocket is closed.
    """

    while streaming.workflow_run and streaming.is_open:
        workflow_run, browser_session = await verify_workflow_run(
            workflow_run_id=streaming.workflow_run.workflow_run_id,
            organization_id=streaming.organization_id,
        )

        streaming.workflow_run = workflow_run
        streaming.browser_session = browser_session

        await asyncio.sleep(2)


async def loop_stream_vnc(streaming: Streaming) -> None:
    """
    Actually stream the VNC session data between a frontend and a browser
    session.

    Loops until the task is cleared or the websocket is closed.
    """

    if not streaming.browser_session:
        LOG.info("No browser session found for task.", task=streaming.task, organization_id=streaming.organization_id)
        return

    browser_address = streaming.browser_session.browser_address
    host, _ = browser_address.rsplit(":")
    vnc_url = f"ws://{host}:{streaming.vnc_port}"

    LOG.info(
        "Connecting to VNC URL.",
        browser_address=browser_address,
        vnc_url=vnc_url,
        task=streaming.task,
        workflow_run=streaming.workflow_run,
        organization_id=streaming.organization_id,
    )

    async with websockets.connect(vnc_url) as novnc_ws:

        async def frontend_to_browser() -> None:
            LOG.info("Starting frontend-to-browser data transfer.", streaming=streaming)
            data: Data | None = None

            while streaming.is_open:
                try:
                    data = await streaming.websocket.receive_bytes()

                    if data:
                        message_type = data[0]

                        # TODO: verify 4,5 are keyboard/mouse; they seem to be
                        if not streaming.interactor == "user" and message_type in (
                            MessageType.keyboard.value,
                            MessageType.mouse.value,
                        ):
                            LOG.info(
                                "Blocking user message.", task=streaming.task, organization_id=streaming.organization_id
                            )
                            continue

                except WebSocketDisconnect:
                    LOG.info("Frontend disconnected.", task=streaming.task, organization_id=streaming.organization_id)
                    raise
                except ConnectionClosedError:
                    LOG.info(
                        "Frontend closed the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        "An unexpected exception occurred.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise

                if not data:
                    continue

                try:
                    await novnc_ws.send(data)
                except WebSocketDisconnect:
                    LOG.info(
                        "Browser disconnected from the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise
                except ConnectionClosedError:
                    LOG.info(
                        "Browser closed the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        "An unexpected exception occurred in frontend-to-browser loop.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise

        async def browser_to_frontend() -> None:
            LOG.info("Starting browser-to-frontend data transfer.", streaming=streaming)
            data: Data | None = None

            while streaming.is_open:
                try:
                    data = await novnc_ws.recv()

                except WebSocketDisconnect:
                    LOG.info(
                        "Browser disconnected from the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    await streaming.close(reason="browser-disconnected")
                except ConnectionClosedError:
                    LOG.info(
                        "Browser closed the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    await streaming.close(reason="browser-closed")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        "An unexpected exception occurred in browser-to-frontend loop.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise

                if not data:
                    continue

                try:
                    await streaming.websocket.send_bytes(data)
                except WebSocketDisconnect:
                    LOG.info(
                        "Frontend disconnected from the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    await streaming.close(reason="frontend-disconnected")
                except ConnectionClosedError:
                    LOG.info(
                        "Frontend closed the streaming session.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    await streaming.close(reason="frontend-closed")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        "An unexpected exception occurred.",
                        task=streaming.task,
                        organization_id=streaming.organization_id,
                    )
                    raise

        loops = [
            asyncio.create_task(frontend_to_browser()),
            asyncio.create_task(browser_to_frontend()),
        ]

        try:
            await collect(loops)
        except Exception:
            LOG.exception(
                "An exception occurred in loop stream.", task=streaming.task, organization_id=streaming.organization_id
            )
        finally:
            LOG.info("Closing the loop stream.", task=streaming.task, organization_id=streaming.organization_id)
            await streaming.close(reason="loop-stream-vnc-closed")


@legacy_base_router.websocket("/stream/vnc/task/{task_id}")
async def task_stream(
    websocket: WebSocket,
    task_id: str,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, task_id=task_id, token=token)


@legacy_base_router.websocket("/stream/vnc/workflow_run/{workflow_run_id}")
async def workflow_run_stream(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, workflow_run_id=workflow_run_id, token=token)


async def stream(
    websocket: WebSocket,
    *,
    apikey: str | None = None,
    task_id: str | None = None,
    token: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    LOG.info("Starting VNC stream.", task_id=task_id, workflow_run_id=workflow_run_id)

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.info("Authentication failed.", task_id=task_id, workflow_run_id=workflow_run_id)
        return

    streaming: Streaming
    loops: list[asyncio.Task] = []

    if task_id:
        result = await get_streaming_for_task(task_id=task_id, organization_id=organization_id, websocket=websocket)

        if not result:
            LOG.error("No streaming context found for the task.", task_id=task_id, organization_id=organization_id)
            await websocket.close(code=1013)
            return

        streaming, loops = result

        LOG.info("Starting streaming for task.", task_id=task_id, organization_id=organization_id)

    elif workflow_run_id:
        LOG.info(
            "Starting streaming for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id
        )
        result = await get_streaming_for_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.error(
                "No streaming context found for the workflow run.",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        streaming, loops = result

        LOG.info(
            "Starting streaming for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id
        )
    else:
        LOG.error("Neither task ID nor workflow run ID was provided.")
        return

    try:
        LOG.info(
            "Starting streaming loops.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the stream function.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the streaming session.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await streaming.close(reason="stream-closed")
