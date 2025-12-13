"""
Provides WS endpoints for streaming a remote browser via VNC.

NOTE(jdo:streaming-local-dev)
-----------------------------
  - grep the above for local development seams
  - augment those seams as indicated, then
  - stand up https://github.com/jomido/whyvern

"""

import structlog
from fastapi import WebSocket

from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    Loops,
    VncChannel,
    get_vnc_channel_for_browser_session,
    get_vnc_channel_for_task,
    get_vnc_channel_for_workflow_run,
)
from skyvern.forge.sdk.utils.aio import collect

LOG = structlog.get_logger()


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
            "Client ID not provided for vnc stream.",
            browser_session_id=browser_session_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )
        return

    LOG.info(
        "Starting vnc stream.",
        browser_session_id=browser_session_id,
        client_id=client_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.warning("Authentication failed.", task_id=task_id, workflow_run_id=workflow_run_id)
        return

    vnc_channel: VncChannel
    loops: Loops

    if browser_session_id:
        result = await get_vnc_channel_for_browser_session(
            client_id=client_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.warning(
                "No vnc context found for the browser session.",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result
    elif task_id:
        result = await get_vnc_channel_for_task(
            client_id=client_id,
            task_id=task_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.warning("No vnc context found for the task.", task_id=task_id, organization_id=organization_id)
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result

        LOG.info("Starting vnc for task.", task_id=task_id, organization_id=organization_id)

    elif workflow_run_id:
        LOG.info("Starting vnc for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        result = await get_vnc_channel_for_workflow_run(
            client_id=client_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            websocket=websocket,
        )

        if not result:
            LOG.warning(
                "No vnc context found for the workflow run.",
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            await websocket.close(code=1013)
            return

        vnc_channel, loops = result

        LOG.info("Starting vnc for workflow run.", workflow_run_id=workflow_run_id, organization_id=organization_id)
    else:
        LOG.error("Neither task ID nor workflow run ID was provided.")
        return

    try:
        LOG.info(
            "Starting vnc loops.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the vnc loop.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the vnc session.",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await vnc_channel.close(reason="vnc-closed")
