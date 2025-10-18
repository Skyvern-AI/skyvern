import ipaddress
from enum import Enum
from typing import Any, NamedTuple

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.services.local_org_auth_token_service import fingerprint_token, regenerate_local_api_key
from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

router = APIRouter(prefix="/internal/auth", tags=["internal"])
LOG = structlog.get_logger()


class AuthStatus(str, Enum):
    missing_env = "missing_env"
    invalid_format = "invalid_format"
    invalid = "invalid"
    expired = "expired"
    not_found = "not_found"
    ok = "ok"


class DiagnosticsResult(NamedTuple):
    status: AuthStatus
    detail: str | None
    validation: Any | None
    token: str | None


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _require_local_access(request: Request) -> None:
    if settings.ENV != "local":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Endpoint only available in local env")
    if not _is_local_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Endpoint requires localhost access")


async def _evaluate_local_api_key(token: str) -> DiagnosticsResult:
    token_candidate = token.strip()
    if not token_candidate or token_candidate == "YOUR_API_KEY":
        return DiagnosticsResult(status=AuthStatus.missing_env, detail=None, validation=None, token=None)

    try:
        validation = await resolve_org_from_api_key(token_candidate, app.DATABASE)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return DiagnosticsResult(status=AuthStatus.not_found, detail=None, token=None, validation=None)

        detail_text = exc.detail if isinstance(exc.detail, str) else None
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            status_value = AuthStatus.invalid
            if detail_text and "expired" in detail_text.lower():
                status_value = AuthStatus.expired
            elif detail_text and "validate" in detail_text.lower():
                status_value = AuthStatus.invalid_format
            return DiagnosticsResult(status=status_value, detail=detail_text, token=None, validation=None)

        LOG.error("Unexpected error while diagnosing API key", status_code=exc.status_code, detail=detail_text)
        raise
    except Exception:
        LOG.error("Unexpected exception while diagnosing API key", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Unable to diagnose API key")

    return DiagnosticsResult(status=AuthStatus.ok, detail=None, validation=validation, token=token_candidate)


def _emit_diagnostics(result: DiagnosticsResult) -> dict[str, object]:
    status_value = result.status.value

    if result.status is AuthStatus.ok and result.validation and result.token:
        fingerprint = fingerprint_token(result.token)
        LOG.info(
            "Local auth diagnostics",
            status=status_value,
            organization_id=result.validation.organization.organization_id,
            fingerprint=fingerprint,
            expires_at=result.validation.payload.exp,
        )
        return {
            "status": status_value,
            "organization_id": result.validation.organization.organization_id,
            "fingerprint": fingerprint,
            "expires_at": result.validation.payload.exp,
        }

    log_kwargs: dict[str, object] = {"status": status_value}
    if result.detail:
        log_kwargs["detail"] = result.detail

    LOG.warning("Local auth diagnostics", **log_kwargs)

    return {"status": status_value}


@router.post("/repair", include_in_schema=False)
async def repair_api_key(request: Request) -> dict[str, object]:
    _require_local_access(request)

    token, organization_id, backend_env_path, frontend_env_path = await regenerate_local_api_key()

    response: dict[str, object] = {
        "status": AuthStatus.ok.value,
        "organization_id": organization_id,
        "fingerprint": fingerprint_token(token),
        "api_key": token,
        "backend_env_path": backend_env_path,
    }

    if frontend_env_path:
        response["frontend_env_path"] = frontend_env_path

    return response


@router.get("/status", include_in_schema=False)
async def auth_status(request: Request) -> dict[str, object]:
    _require_local_access(request)
    token_candidate = request.headers.get("x-api-key") or ""
    result = await _evaluate_local_api_key(token_candidate)
    return _emit_diagnostics(result)
