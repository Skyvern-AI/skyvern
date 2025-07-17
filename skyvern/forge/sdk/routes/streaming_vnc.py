"""
Streaming VNC WebSocket connections.
"""

import asyncio
from urllib.parse import urlparse

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets import Data
from websockets.exceptions import ConnectionClosedError

import skyvern.forge.sdk.routes.streaming_clients as sc
from skyvern.config import settings
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming_auth import auth
from skyvern.forge.sdk.routes.streaming_verify import (
    loop_verify_browser_session,
    loop_verify_task,
    loop_verify_workflow_run,
    verify_browser_session,
    verify_task,
    verify_workflow_run,
)
from skyvern.forge.sdk.utils.aio import collect

LOG = structlog.get_logger()


async def get_streaming_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.Streaming, sc.Loops] | None:
    """
    Return a streaming context for a browser session, with a list of loops to run concurrently.
    """

    LOG.info("Getting streaming context for browser session.", browser_session_id=browser_session_id)

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        LOG.info(
            "No initial browser session found.", browser_session_id=browser_session_id, organization_id=organization_id
        )
        return None

    streaming = sc.Streaming(
        client_id=client_id,
        interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        browser_session=browser_session,
        websocket=websocket,
    )

    LOG.info("Got streaming context for browser session.", streaming=streaming)

    loops = [
        asyncio.create_task(loop_verify_browser_session(streaming)),
        asyncio.create_task(loop_stream_vnc(streaming)),
    ]

    return streaming, loops


async def get_streaming_for_task(
    client_id: str,
    task_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.Streaming, sc.Loops] | None:
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

    streaming = sc.Streaming(
        client_id=client_id,
        interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        websocket=websocket,
        browser_session=browser_session,
        task=task,
    )

    loops = [
        asyncio.create_task(loop_verify_task(streaming)),
        asyncio.create_task(loop_stream_vnc(streaming)),
    ]

    return streaming, loops


async def get_streaming_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.Streaming, sc.Loops] | None:
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

    streaming = sc.Streaming(
        client_id=client_id,
        interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        browser_session=browser_session,
        workflow_run=workflow_run,
        websocket=websocket,
    )

    LOG.info("Got streaming context for workflow run.", streaming=streaming)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(streaming)),
        asyncio.create_task(loop_stream_vnc(streaming)),
    ]

    return streaming, loops


async def loop_stream_vnc(streaming: sc.Streaming) -> None:
    """
    Actually stream the VNC session data between a frontend and a browser
    session.

    Loops until the task is cleared or the websocket is closed.
    """

    if not streaming.browser_session:
        LOG.info("No browser session found for task.", task=streaming.task, organization_id=streaming.organization_id)
        return

    vnc_url: str = ""
    if streaming.browser_session.ip_address:
        if ":" in streaming.browser_session.ip_address:
            ip, _ = streaming.browser_session.ip_address.split(":")
            vnc_url = f"ws://{ip}:{streaming.vnc_port}"
        else:
            vnc_url = f"ws://{streaming.browser_session.ip_address}:{streaming.vnc_port}"
    else:
        browser_address = streaming.browser_session.browser_address

        parsed_browser_address = urlparse(browser_address)
        host = parsed_browser_address.hostname
        vnc_url = f"ws://{host}:{streaming.vnc_port}"

    LOG.info(
        "Connecting to VNC URL.",
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

                        if message_type == sc.MessageType.Keyboard.value:
                            streaming.update_key_state(data)

                            if streaming.key_state.is_forbidden(data):
                                continue

                        if message_type == sc.MessageType.Mouse.value:
                            if sc.Mouse.Up.Right(data):
                                continue

                        if not streaming.interactor == "user" and message_type in (
                            sc.MessageType.Keyboard.value,
                            sc.MessageType.Mouse.value,
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


@base_router.websocket("/stream/vnc/browser_session/{browser_session_id}")
async def browser_session_stream(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, browser_session_id=browser_session_id, token=token)


@legacy_base_router.websocket("/stream/vnc/task/{task_id}")
async def task_stream(
    websocket: WebSocket,
    task_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, task_id=task_id, token=token)


@legacy_base_router.websocket("/stream/vnc/workflow_run/{workflow_run_id}")
async def workflow_run_stream(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    await stream(websocket, apikey=apikey, client_id=client_id, workflow_run_id=workflow_run_id, token=token)


async def stream(
    websocket: WebSocket,
    *,
    apikey: str | None = None,
    browser_session_id: str | None = None,
    client_id: str | None = None,
    task_id: str | None = None,
    token: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    if not client_id:
        LOG.error(
            "Client ID not provided for VNC stream.",
            browser_session_id=browser_session_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )
        return

    LOG.info(
        "Starting VNC stream.",
        browser_session_id=browser_session_id,
        client_id=client_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.error("Authentication failed.", task_id=task_id, workflow_run_id=workflow_run_id)
        return

    streaming: sc.Streaming
    loops: list[asyncio.Task] = []

    if browser_session_id:
        result = await get_streaming_for_browser_session(
            client_id=client_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.error(
                "No streaming context found for the browser session.",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        streaming, loops = result

        LOG.info(
            "Starting streaming for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
    elif task_id:
        result = await get_streaming_for_task(
            client_id=client_id,
            task_id=task_id,
            organization_id=organization_id,
            websocket=websocket,
        )

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
            client_id=client_id,
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
