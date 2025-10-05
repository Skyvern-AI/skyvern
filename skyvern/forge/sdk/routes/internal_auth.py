from __future__ import annotations

import ipaddress

from fastapi import APIRouter, HTTPException, Request, status

from skyvern.config import settings
from skyvern.forge.sdk.services.local_org_auth_token_service import fingerprint_token, regenerate_local_api_key

router = APIRouter(prefix="/internal/auth", tags=["internal"])


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


@router.post("/repair")
async def repair_api_key(request: Request) -> dict[str, object]:
    if settings.ENV != "local":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Self-heal endpoint only available in local env")

    if not _is_local_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Self-heal endpoint requires localhost access")

    token, organization_id = await regenerate_local_api_key()

    return {
        "status": "ok",
        "organization_id": organization_id,
        "fingerprint": fingerprint_token(token),
    }
