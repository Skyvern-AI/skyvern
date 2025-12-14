"""
Provides WS endpoints for streaming messages to/from our frontend application.
"""

import structlog
from fastapi import WebSocket

from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth
from skyvern.forge.sdk.routes.streaming.channels.message import (
    Loops,
    MessageChannel,
    get_message_channel_for_browser_session,
    get_message_channel_for_workflow_run,
)
from skyvern.forge.sdk.utils.aio import collect

LOG = structlog.get_logger()


@base_router.websocket("/stream/messages/browser_session/{browser_session_id}")
async def browser_session_messages(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    return await messages(
        websocket=websocket,
        browser_session_id=browser_session_id,
        apikey=apikey,
        client_id=client_id,
        token=token,
    )


@legacy_base_router.websocket("/stream/messages/workflow_run/{workflow_run_id}")
async def workflow_run_messages(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    return await messages(
        websocket=websocket,
        workflow_run_id=workflow_run_id,
        apikey=apikey,
        client_id=client_id,
        token=token,
    )


async def messages(
    websocket: WebSocket,
    browser_session_id: str | None = None,
    workflow_run_id: str | None = None,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.warning(
            "Authentication failed.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
        )
        return

    if not client_id:
        LOG.error(
            "No client ID provided.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
        )
        await websocket.close(code=1002)
        return

    message_channel: MessageChannel
    loops: Loops = []

    if browser_session_id:
        result = await get_message_channel_for_browser_session(
            client_id=client_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            websocket=websocket,
        )
    elif workflow_run_id:
        result = await get_message_channel_for_workflow_run(
            client_id=client_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            websocket=websocket,
        )
    else:
        LOG.error(
            "Message channel: no browser_session_id or workflow_run_id provided.",
            client_id=client_id,
            organization_id=organization_id,
        )
        await websocket.close(code=1002)
        return

    if not result:
        LOG.warning(
            "No message channel found.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await websocket.close(code=1013)
        return

    message_channel, loops = result

    try:
        LOG.info(
            "Starting message channel loops.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the message loop function(s).",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the message channel.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        await message_channel.close(reason="message-stream-closed")
