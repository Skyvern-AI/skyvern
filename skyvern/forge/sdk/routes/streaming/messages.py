"""
Streaming messages for WebSocket connections.
"""

import asyncio

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError

import skyvern.forge.sdk.routes.streaming.clients as sc
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.agent import connected_agent
from skyvern.forge.sdk.routes.streaming.auth import auth
from skyvern.forge.sdk.routes.streaming.verify import (
    loop_verify_browser_session,
    loop_verify_workflow_run,
    verify_browser_session,
    verify_workflow_run,
)
from skyvern.forge.sdk.utils.aio import collect

LOG = structlog.get_logger()


async def get_messages_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.MessageChannel, sc.Loops] | None:
    """
    Return a message channel for a browser session, with a list of loops to run concurrently.
    """

    LOG.info("Getting message channel for browser session.", browser_session_id=browser_session_id)

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        LOG.info(
            "Message channel: no initial browser session found.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        return None

    message_channel = sc.MessageChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        websocket=websocket,
    )

    LOG.info("Got message channel for browser session.", message_channel=message_channel)

    loops = [
        asyncio.create_task(loop_verify_browser_session(message_channel)),
        asyncio.create_task(loop_channel(message_channel)),
    ]

    return message_channel, loops


async def get_messages_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.MessageChannel, sc.Loops] | None:
    """
    Return a message channel for a workflow run, with a list of loops to run concurrently.
    """

    LOG.info("Getting message channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info(
            "Message channel: no initial workflow run found.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    if not browser_session:
        LOG.info(
            "Message channel: no initial browser session found for workflow run.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    message_channel = sc.MessageChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        workflow_run=workflow_run,
        websocket=websocket,
    )

    LOG.info("Got message channel for workflow run.", message_channel=message_channel)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(message_channel)),
        asyncio.create_task(loop_channel(message_channel)),
    ]

    return message_channel, loops


async def loop_channel(message_channel: sc.MessageChannel) -> None:
    """
    Stream messages and their results back and forth.

    Loops until the workflow run is cleared or the websocket is closed.
    """

    if not message_channel.browser_session:
        LOG.info(
            "No browser session found for workflow run.",
            workflow_run=message_channel.workflow_run,
            organization_id=message_channel.organization_id,
        )
        return

    async def frontend_to_backend() -> None:
        LOG.info("Starting frontend-to-backend channel loop.", message_channel=message_channel)

        while message_channel.is_open:
            try:
                data = await message_channel.websocket.receive_json()

                if not isinstance(data, dict):
                    LOG.error(f"Cannot create channel message: expected dict, got {type(data)}")
                    continue

                try:
                    message = sc.reify_channel_message(data)
                except ValueError:
                    continue

                message_kind = message.kind

                match message_kind:
                    case "take-control":
                        streaming = sc.get_streaming_client(message_channel.client_id)
                        if not streaming:
                            LOG.error(
                                "No streaming client found for message.",
                                message_channel=message_channel,
                                message=message,
                            )
                            continue
                        streaming.interactor = "user"
                    case "cede-control":
                        streaming = sc.get_streaming_client(message_channel.client_id)
                        if not streaming:
                            LOG.error(
                                "No streaming client found for message.",
                                message_channel=message_channel,
                                message=message,
                            )
                            continue
                        streaming.interactor = "agent"
                    case "ask-for-clipboard-response":
                        if not isinstance(message, sc.MessageInAskForClipboardResponse):
                            LOG.error(
                                "Invalid message type for ask-for-clipboard-response.",
                                message_channel=message_channel,
                                message=message,
                            )
                            continue

                        streaming = sc.get_streaming_client(message_channel.client_id)
                        text = message.text

                        async with connected_agent(streaming) as agent:
                            await agent.paste_text(text)
                    case _:
                        LOG.error(f"Unknown message kind: '{message_kind}'")
                        continue

            except WebSocketDisconnect:
                LOG.info(
                    "Frontend disconnected.",
                    workflow_run=message_channel.workflow_run,
                    organization_id=message_channel.organization_id,
                )
                raise
            except ConnectionClosedError:
                LOG.info(
                    "Frontend closed the streaming session.",
                    workflow_run=message_channel.workflow_run,
                    organization_id=message_channel.organization_id,
                )
                raise
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.exception(
                    "An unexpected exception occurred.",
                    workflow_run=message_channel.workflow_run,
                    organization_id=message_channel.organization_id,
                )
                raise

    loops = [
        asyncio.create_task(frontend_to_backend()),
    ]

    try:
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in loop channel stream.",
            workflow_run=message_channel.workflow_run,
            organization_id=message_channel.organization_id,
        )
    finally:
        LOG.info(
            "Closing the loop channel stream.",
            workflow_run=message_channel.workflow_run,
            organization_id=message_channel.organization_id,
        )
        await message_channel.close(reason="loop-channel-closed")


@base_router.websocket("/stream/messages/browser_session/{browser_session_id}")
@base_router.websocket("/stream/commands/browser_session/{browser_session_id}")
async def browser_session_messages(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    LOG.info("Starting message stream for browser session.", browser_session_id=browser_session_id)

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.error("Authentication failed.", browser_session_id=browser_session_id)
        return

    if not client_id:
        LOG.error("No client ID provided.", browser_session_id=browser_session_id)
        return

    message_channel: sc.MessageChannel
    loops: list[asyncio.Task] = []

    result = await get_messages_for_browser_session(
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

    message_channel, loops = result

    try:
        LOG.info(
            "Starting message stream loops for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the message stream function for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the message stream session for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )

        await message_channel.close(reason="stream-closed")


@legacy_base_router.websocket("/stream/messages/workflow_run/{workflow_run_id}")
@legacy_base_router.websocket("/stream/commands/workflow_run/{workflow_run_id}")
async def workflow_run_messages(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    LOG.info("Starting message stream.", workflow_run_id=workflow_run_id)

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.error("Authentication failed.", workflow_run_id=workflow_run_id)
        return

    if not client_id:
        LOG.error("No client ID provided.", workflow_run_id=workflow_run_id)
        return

    message_channel: sc.MessageChannel
    loops: list[asyncio.Task] = []

    result = await get_messages_for_workflow_run(
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

    message_channel, loops = result

    try:
        LOG.info(
            "Starting message stream loops.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the message stream function.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the message stream session.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        await message_channel.close(reason="stream-closed")
