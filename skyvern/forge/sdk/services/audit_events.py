from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import structlog

from skyvern.forge.sdk.services.request_principal import RequestPrincipal

LOG = structlog.get_logger()


@dataclass(frozen=True)
class AuditEvent:
    """Resource identifiers, field names, and explicitly selected non-secret metadata for an audited write."""

    organization_id: str
    action: str
    resource_type: str
    resource_id: str | None
    changed_fields: tuple[str, ...] = ()
    related_resource_ids: tuple[str, ...] = ()
    auth_kind: str | None = None
    oauth_client_id: str | None = None
    app_role: str | None = None
    metadata: Mapping[str, str | list[str]] = field(default_factory=dict)


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
