"""
Streaming commands WebSocket connections.
"""

import asyncio

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError

import skyvern.forge.sdk.routes.streaming_clients as sc
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming_auth import auth
from skyvern.forge.sdk.routes.streaming_verify import (
    loop_verify_browser_session,
    loop_verify_workflow_run,
    verify_browser_session,
    verify_workflow_run,
)
from skyvern.forge.sdk.utils.aio import collect

LOG = structlog.get_logger()


async def get_commands_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.CommandChannel, sc.Loops] | None:
    """
    Return a commands channel for a browser session, with a list of loops to run concurrently.
    """

    LOG.info("Getting commands channel for browser session.", browser_session_id=browser_session_id)

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        LOG.info(
            "Command channel: no initial browser session found.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        return None

    commands = sc.CommandChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        websocket=websocket,
    )

    LOG.info("Got command channel for browser session.", commands=commands)

    loops = [
        asyncio.create_task(loop_verify_browser_session(commands)),
        asyncio.create_task(loop_channel(commands)),
    ]

    return commands, loops


async def get_commands_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[sc.CommandChannel, sc.Loops] | None:
    """
    Return a commands channel for a workflow run, with a list of loops to run concurrently.
    """

    LOG.info("Getting commands channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info(
            "Command channel: no initial workflow run found.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    if not browser_session:
        LOG.info(
            "Command channel: no initial browser session found for workflow run.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    commands = sc.CommandChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        workflow_run=workflow_run,
        websocket=websocket,
    )

    LOG.info("Got command channel for workflow run.", commands=commands)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(commands)),
        asyncio.create_task(loop_channel(commands)),
    ]

    return commands, loops


async def loop_channel(commands: sc.CommandChannel) -> None:
    """
    Stream commands and their results back and forth.

    Loops until the workflow run is cleared or the websocket is closed.
    """

    if not commands.browser_session:
        LOG.info(
            "No browser session found for workflow run.",
            workflow_run=commands.workflow_run,
            organization_id=commands.organization_id,
        )
        return

    async def frontend_to_backend() -> None:
        LOG.info("Starting frontend-to-backend channel loop.", commands=commands)

        while commands.is_open:
            try:
                data = await commands.websocket.receive_json()

                if not isinstance(data, dict):
                    LOG.error(f"Cannot create channel command: expected dict, got {type(data)}")
                    continue

                try:
                    command = sc.reify_channel_command(data)
                except ValueError:
                    continue

                command_kind = command.kind

                match command_kind:
                    case "take-control":
                        streaming = sc.get_streaming_client(commands.client_id)
                        if not streaming:
                            LOG.error("No streaming client found for command.", commands=commands, command=command)
                            continue
                        streaming.interactor = "user"
                    case "cede-control":
                        streaming = sc.get_streaming_client(commands.client_id)
                        if not streaming:
                            LOG.error("No streaming client found for command.", commands=commands, command=command)
                            continue
                        streaming.interactor = "agent"
                    case _:
                        LOG.error(f"Unknown command kind: '{command_kind}'")
                        continue

            except WebSocketDisconnect:
                LOG.info(
                    "Frontend disconnected.",
                    workflow_run=commands.workflow_run,
                    organization_id=commands.organization_id,
                )
                raise
            except ConnectionClosedError:
                LOG.info(
                    "Frontend closed the streaming session.",
                    workflow_run=commands.workflow_run,
                    organization_id=commands.organization_id,
                )
                raise
            except asyncio.CancelledError:
                pass
            except Exception:
                LOG.exception(
                    "An unexpected exception occurred.",
                    workflow_run=commands.workflow_run,
                    organization_id=commands.organization_id,
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
            workflow_run=commands.workflow_run,
            organization_id=commands.organization_id,
        )
    finally:
        LOG.info(
            "Closing the loop channel stream.",
            workflow_run=commands.workflow_run,
            organization_id=commands.organization_id,
        )
        await commands.close(reason="loop-channel-closed")


@base_router.websocket("/stream/commands/browser_session/{browser_session_id}")
async def browser_session_commands(
    websocket: WebSocket,
    browser_session_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    LOG.info("Starting stream commands for browser session.", browser_session_id=browser_session_id)

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.error("Authentication failed.", browser_session_id=browser_session_id)
        return

    if not client_id:
        LOG.error("No client ID provided.", browser_session_id=browser_session_id)
        return

    commands: sc.CommandChannel
    loops: list[asyncio.Task] = []

    result = await get_commands_for_browser_session(
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

    commands, loops = result

    try:
        LOG.info(
            "Starting command stream loops for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the command stream function for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the command stream session for browser session.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )

        await commands.close(reason="stream-closed")


@legacy_base_router.websocket("/stream/commands/workflow_run/{workflow_run_id}")
async def workflow_run_commands(
    websocket: WebSocket,
    workflow_run_id: str,
    apikey: str | None = None,
    client_id: str | None = None,
    token: str | None = None,
) -> None:
    LOG.info("Starting stream commands.", workflow_run_id=workflow_run_id)

    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)

    if not organization_id:
        LOG.error("Authentication failed.", workflow_run_id=workflow_run_id)
        return

    if not client_id:
        LOG.error("No client ID provided.", workflow_run_id=workflow_run_id)
        return

    commands: sc.CommandChannel
    loops: list[asyncio.Task] = []

    result = await get_commands_for_workflow_run(
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

    commands, loops = result

    try:
        LOG.info(
            "Starting command stream loops.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        await collect(loops)
    except Exception:
        LOG.exception(
            "An exception occurred in the command stream function.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    finally:
        LOG.info(
            "Closing the command stream session.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        await commands.close(reason="stream-closed")
