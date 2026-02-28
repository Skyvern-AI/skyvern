"""WebSocket endpoint for streaming global 2FA verification code notifications."""

import asyncio

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from skyvern.forge import app
from skyvern.forge.sdk.notification.factory import NotificationRegistryFactory
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.routes.streaming.auth import auth

LOG = structlog.get_logger()
HEARTBEAT_INTERVAL = 60


@base_router.websocket("/stream/notifications")
async def notification_stream(
    websocket: WebSocket,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    return await _notification_stream_handler(websocket=websocket, apikey=apikey, token=token)


@legacy_base_router.websocket("/stream/notifications")
async def notification_stream_legacy(
    websocket: WebSocket,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    return await _notification_stream_handler(websocket=websocket, apikey=apikey, token=token)


async def _notification_stream_handler(
    websocket: WebSocket,
    apikey: str | None = None,
    token: str | None = None,
) -> None:
    organization_id = await auth(apikey=apikey, token=token, websocket=websocket)
    if not organization_id:
        LOG.info("Notifications: Authentication failed")
        return

    LOG.info("Notifications: Started streaming", organization_id=organization_id)
    registry = NotificationRegistryFactory.get_registry()
    queue = registry.subscribe(organization_id)

    try:
        # Send initial state: all currently active verification requests
        active_requests = await app.DATABASE.get_active_verification_requests(organization_id)
        for req in active_requests:
            try:
                await websocket.send_json(
                    {
                        "type": "verification_code_required",
                        "task_id": req.get("task_id"),
                        "workflow_run_id": req.get("workflow_run_id"),
                        "identifier": req.get("verification_code_identifier"),
                        "polling_started_at": req.get("verification_code_polling_started_at"),
                    }
                )
            except (WebSocketDisconnect, ConnectionClosedOK, ConnectionClosedError, RuntimeError):
                LOG.info(
                    "Notifications: Client disconnected during initial state send",
                    organization_id=organization_id,
                )
                return

        # Watch for client disconnect while streaming events
        disconnect_event = asyncio.Event()

        async def _watch_disconnect() -> None:
            try:
                while True:
                    await websocket.receive()
            except (WebSocketDisconnect, ConnectionClosedOK, ConnectionClosedError):
                disconnect_event.set()

        watcher = asyncio.create_task(_watch_disconnect())
        try:
            while not disconnect_event.is_set():
                queue_task = asyncio.ensure_future(asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL))
                disconnect_wait = asyncio.ensure_future(disconnect_event.wait())
                done, pending = await asyncio.wait({queue_task, disconnect_wait}, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()

                if disconnect_event.is_set():
                    return

                try:
                    message = queue_task.result()
                    await websocket.send_json(message)
                except TimeoutError:
                    try:
                        await websocket.send_json({"type": "heartbeat"})
                    except Exception:
                        LOG.info(
                            "Notifications: Client unreachable during heartbeat. Closing.",
                            organization_id=organization_id,
                        )
                        return
                except asyncio.CancelledError:
                    return
                except (WebSocketDisconnect, ConnectionClosedOK, ConnectionClosedError, RuntimeError):
                    LOG.info(
                        "Notifications: Client disconnected during send",
                        organization_id=organization_id,
                    )
                    return
        finally:
            watcher.cancel()

    except WebSocketDisconnect:
        LOG.info("Notifications: WebSocket disconnected", organization_id=organization_id)
    except ConnectionClosedOK:
        LOG.info("Notifications: ConnectionClosedOK", organization_id=organization_id)
    except ConnectionClosedError:
        LOG.warning(
            "Notifications: ConnectionClosedError (client likely disconnected)", organization_id=organization_id
        )
    except Exception:
        LOG.warning("Notifications: Error while streaming", organization_id=organization_id, exc_info=True)
    finally:
        registry.unsubscribe(organization_id, queue)
    LOG.info("Notifications: Connection closed", organization_id=organization_id)
