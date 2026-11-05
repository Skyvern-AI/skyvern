"""
Provides WS endpoints for streaming messages to/from our frontend application.
"""

import asyncio

import structlog
from fastapi import WebSocket

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth
from skyvern.forge.sdk.routes.streaming.cdp_input import _reauthorize_stream_periodically
from skyvern.forge.sdk.routes.streaming.channels.message import (
    Loops,
    MessageChannel,
    get_message_channel_for_browser_session,
    get_message_channel_for_workflow_run,
)
from skyvern.forge.sdk.services.request_principal import get_request_principal
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
    organization_id = await auth(
        apikey=apikey,
        token=token,
        websocket=websocket,
        # The message loop navigates, pastes and clears cookies, so it needs execute, not read.
        folder_action="workflow_run.execute",
        folder_resource={"browser_session_id": browser_session_id, "workflow_run_id": workflow_run_id},
    )

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
            "[WS] messages: no browser_session_id or workflow_run_id provided",
            client_id=client_id,
            organization_id=organization_id,
        )
        await websocket.close(code=1002)
        return

    if not result:
        LOG.debug(
            "No message channel found.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await websocket.close(code=1013)
        return

    message_channel, loops = result
    principal = get_request_principal()
    scoped = principal is not None and principal.api_key_access_mode == "scoped"
    reauthorization_task: asyncio.Task[None] | None = None

    try:
        if scoped and await app.AGENT_FUNCTION.browser_session_stream_reauthorization_required(
            principal, organization_id
        ):

            async def reauthorize() -> bool:
                if workflow_run_id and not await app.AGENT_FUNCTION.reauthorize_workflow_run_stream(
                    principal, organization_id, workflow_run_id, "stream.connect", folder_action="workflow_run.execute"
                ):
                    return False
                session = message_channel.browser_session
                if session is None:
                    return False
                return await app.AGENT_FUNCTION.reauthorize_browser_session_stream(
                    principal,
                    organization_id,
                    session.persistent_browser_session_id,
                    action="stream.connect",
                    folder_action="workflow_run.execute",
                )

            reauthorization_task = asyncio.create_task(
                _reauthorize_stream_periodically(
                    websocket,
                    reauthorize,
                    {"browser_session_id": browser_session_id or "", "workflow_run_id": workflow_run_id or ""},
                )
            )
        LOG.debug(
            "Starting message channel loops.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if reauthorization_task is None:
            await collect(loops)
        else:
            done, _ = await asyncio.wait([*loops, reauthorization_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except Exception:
        LOG.exception(
            "An exception occurred in the message loop function(s).",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        if scoped:
            if reauthorization_task is not None:
                reauthorization_task.cancel()
            for loop in loops:
                loop.cancel()
            await asyncio.gather(
                *loops, *([reauthorization_task] if reauthorization_task else []), return_exceptions=True
            )
        LOG.debug(
            "Closing the message channel.",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await message_channel.close(reason="message-stream-closed")
