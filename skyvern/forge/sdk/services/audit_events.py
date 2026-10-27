from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import structlog

from skyvern.forge.sdk.services.request_principal import RequestPrincipal

LOG = structlog.get_logger()


@dataclass(frozen=True)
class AuditEvent:
    """Resource identifiers and field names for an audited write; never secrets or request bodies."""

    organization_id: str
    action: str
    resource_type: str
    resource_id: str | None
    changed_fields: tuple[str, ...] = ()
    related_resource_ids: tuple[str, ...] = ()
    auth_kind: str | None = None
    oauth_client_id: str | None = None
    app_role: str | None = None


async def record_run_cancel_audit(
    principal: RequestPrincipal | None,
    organization_id: str,
    resource_type: Literal["task", "workflow_run"],
    resource_id: str,
) -> None:
    # Import lazily because the repositories that use this helper import the forge app.
    from skyvern.forge import app

    event = AuditEvent(organization_id, f"{resource_type}.cancel", resource_type, resource_id)
    write = asyncio.create_task(app.AGENT_FUNCTION.record_audit_event(principal, event))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(write)
        except Exception:
            LOG.warning(
                "Failed to record cancellation audit event",
                organization_id=organization_id,
                resource_id=resource_id,
                exc_info=True,
            )
        raise
    except Exception:
        LOG.warning(
            "Failed to record cancellation audit event",
            organization_id=organization_id,
            resource_id=resource_id,
            exc_info=True,
        )
