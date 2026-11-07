"""Clerk token-claim helpers, kept free of the MCP server stack so importing them stays light."""

from fastapi import HTTPException


class MCPOrgRoleRequiredError(HTTPException):
    """Raised when an organization session has no supported MCP sign-in role."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail="MCP sign-in requires an organization Admin or Member role.")


def get_token_org_id(payload: dict[str, object]) -> str | None:
    """Return the Clerk org id from supported JWT claim shapes, if any.

    Session tokens carry the organization either as top-level ``org_id`` (v1
    shape, and our JWT template) or as compact claim ``o.id`` (v2 shape,
    current since Clerk API version 2025-04-10).
    """
    org_id_claim = payload.get("org_id")
    if isinstance(org_id_claim, str) and org_id_claim:
        return org_id_claim

    compact_org_claim = payload.get("o")
    if isinstance(compact_org_claim, dict):
        compact_org_id = compact_org_claim.get("id")
        if isinstance(compact_org_id, str) and compact_org_id:
            return compact_org_id

    return None
