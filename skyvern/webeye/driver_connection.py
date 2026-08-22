"""Make a Playwright driver whose transport died report itself closed.

Playwright sets ``Connection._closed_error`` only from an explicit ``stop()``. When the driver
subprocess dies on its own, its read loop completes ``transport.on_error_future`` and nothing else
happens, so every retained page/context keeps passing the send guard in ``_send_message_to_server``:
each call writes into the dead pipe (asyncio warns once per write past its fifth) and then awaits a
reply that can never arrive. Completing the same latch ``stop()`` uses turns those calls into an
immediate ``TargetClosedError``, which the callers already handle.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

LOG = structlog.get_logger()

_ARMED_FLAG = "_skyvern_transport_loss_armed"


def close_driver_connection_on_transport_loss(driver: Any) -> None:
    """Arm ``driver`` so a dead transport marks its connection closed. Idempotent, and a no-op on an
    engine that is a Playwright driver by duck type only, with no connection/transport underneath."""
    connection = getattr(driver, "_connection", None)
    transport = getattr(connection, "_transport", None)
    on_error_future = getattr(transport, "on_error_future", None)
    cleanup = getattr(connection, "cleanup", None)
    if not isinstance(on_error_future, asyncio.Future) or not callable(cleanup):
        return
    if getattr(connection, _ARMED_FLAG, False):
        return
    setattr(connection, _ARMED_FLAG, True)

    def _mark_closed(finished: asyncio.Future) -> None:
        if finished.cancelled():
            return
        # Retrieving the exception here is also what keeps a lost driver from surfacing as an
        # orphaned "Future exception was never retrieved" record.
        error = finished.exception()
        if getattr(connection, "_closed_error", None) is not None:
            return
        cleanup(f"Playwright driver transport closed: {error}")
        LOG.warning(
            "Playwright driver transport closed; failing further calls on this driver",
            error_type=type(error).__name__ if error is not None else None,
            error_message=str(error)[:200] if error is not None else None,
        )

    on_error_future.add_done_callback(_mark_closed)
