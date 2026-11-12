from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from typing_extensions import TypedDict

from skyvern.forge.sdk.copilot.credential_resolution import grounded_references
from skyvern.forge.sdk.integrations.connected_accounts import ConnectionProvider, OrgConnection
from skyvern.forge.sdk.schemas.google_oauth import STATE_ACTIVE
from skyvern.forge.sdk.services.request_principal import get_request_principal, human_user_id


def request_user_id(organization_id: str) -> str | None:
    """The person this request acts for, which narrows the org connections they may see."""
    return human_user_id(get_request_principal(), organization_id)


@dataclass(frozen=True)
class ConnectionResolution:
    status: Literal["resolved", "ambiguous", "not_found"]
    connection: OrgConnection | None
    candidates: tuple[OrgConnection, ...]


def resolve_connection_reference(connections: Sequence[OrgConnection], reference: str) -> ConnectionResolution:
    """Match an id, name or account email; active rows win over same-named inactive ones."""
    reference = reference.strip()
    normalized = reference.casefold()
    matches = [
        connection
        for connection in connections
        if connection.id == reference
        or (
            normalized
            and normalized in {connection.name.strip().casefold(), (connection.email_address or "").strip().casefold()}
        )
    ]
    if not matches:
        return ConnectionResolution("not_found", None, ())
    # Active wins only within one provider; a label shared across providers is the model's choice to make.
    if len({connection.provider for connection in matches}) > 1:
        return ConnectionResolution("ambiguous", None, tuple(matches))
    candidates = tuple([connection for connection in matches if connection.active] or matches)
    if len({connection.id for connection in candidates}) != 1:
        return ConnectionResolution("ambiguous", None, candidates)
    return ConnectionResolution("resolved", candidates[0], candidates)


def connection_labels(connections: Sequence[OrgConnection]) -> list[str]:
    return [
        label.casefold() for connection in connections for label in (connection.name, connection.email_address) if label
    ]


def reference_is_cited(message: str, reference: str, connections: Sequence[OrgConnection]) -> bool:
    """The exact literal stands as its own token in the user's turn. A label that only occurs inside a longer
    sibling label ("Marketing" within "Marketing Archive") is not cited on its own."""
    if not message or not reference:
        return False
    return reference.casefold() in grounded_references(
        message.casefold(), [reference.casefold(), *connection_labels(connections)]
    )


def connection_is_cited(message: str, connection: OrgConnection, connections: Sequence[OrgConnection]) -> bool:
    """The user cited this connection's id, or a name that resolves to it alone among its provider's rows."""
    peers = [peer for peer in connections if peer.provider == connection.provider]
    for label in (connection.id, connection.name, connection.email_address):
        if not label or not reference_is_cited(message, label, connections):
            continue
        resolved = resolve_connection_reference(peers, label).connection
        if resolved is not None and resolved.id == connection.id:
            return True
    return False


def listed_connection_ids(tool_activity: Sequence[dict[str, Any]]) -> set[str]:
    """Active connections in this turn's latest `list_integrations` result, plus every connection
    `read_google_sheet` opened a sheet through."""
    opened = {
        connection_id
        for activity in tool_activity
        if activity.get("tool") == "read_google_sheet" and isinstance(activity.get("opened_connection_ids"), list)
        for connection_id in activity["opened_connection_ids"]
        if isinstance(connection_id, str)
    }
    listing = next(
        (activity for activity in reversed(tool_activity) if activity.get("tool") == "list_integrations"), {}
    )
    integrations = listing.get("integrations")
    listed = {
        connection_id
        for integration in (integrations if isinstance(integrations, list) else [])
        if isinstance(integration, dict)
        and integration.get("state") == STATE_ACTIVE
        and isinstance((connection_id := integration.get("connection_id")), str)
    }
    return listed | opened


class ConnectionCardPayload(TypedDict):
    integrationId: str
    integrationName: str
    provider: ConnectionProvider
    scopeProfile: str | None
    requiredScopes: list[str]
    reconnectConnectionId: str | None
