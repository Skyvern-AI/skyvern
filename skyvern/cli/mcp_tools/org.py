"""Skyvern MCP organization-settings tools."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, ValidationError

from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate

from ._common import ErrorCode, Timer, make_error, make_result, raw_http_get, raw_http_put

_UPDATE_FIELDS: frozenset[str] = frozenset(OrganizationUpdate.model_fields)


async def skyvern_org_get() -> dict[str, Any]:
    """Get the caller's organization settings.

    Use this to discover valid keys before calling skyvern_org_update.
    """
    with Timer() as timer:
        try:
            data = await raw_http_get("api/v1/organizations/me")
            timer.mark("http")
        except Exception as e:
            return make_result(
                "skyvern_org_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    if not isinstance(data, dict) or not data.get("organization_id"):
        return make_result(
            "skyvern_org_get",
            ok=False,
            timing_ms=timer.timing_ms,
            error=make_error(ErrorCode.API_ERROR, "Unexpected response from /organizations/me", str(data)[:200]),
        )

    return make_result("skyvern_org_get", data=data, timing_ms=timer.timing_ms)


async def skyvern_org_update(
    updates: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Partial settings dict. Allowed keys: "
                "max_steps_per_run (int >= 1), "
                "max_retries_per_step (int >= 0), "
                "webhook_callback_url (string), "
                "artifact_url_expiry_seconds (int 3600-604800), "
                "clear_artifact_url_expiry_seconds (bool — resets the expiry override to the global default)."
            )
        ),
    ],
) -> dict[str, Any]:
    """Update organization settings. Pass only the fields you want to change."""
    if not updates:
        return make_result(
            "skyvern_org_update",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, "updates dict is empty", "Pass at least one settable key"),
        )

    none_keys = sorted(k for k, v in updates.items() if v is None)
    if none_keys:
        return make_result(
            "skyvern_org_update",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"None is not a valid value for: {', '.join(none_keys)}",
                "Omit keys you don't want to change instead of passing None",
            ),
        )

    unknown = sorted(set(updates) - set(_UPDATE_FIELDS))
    if unknown:
        return make_result(
            "skyvern_org_update",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Unknown settings keys: {', '.join(unknown)}",
                f"Allowed keys: {', '.join(_UPDATE_FIELDS)}",
            ),
        )

    try:
        validated = OrganizationUpdate.model_validate(updates).model_dump(exclude_unset=True)
    except ValidationError as e:
        return make_result(
            "skyvern_org_update",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), "Check field types and ranges"),
        )

    with Timer() as timer:
        try:
            data = await raw_http_put("api/v1/organizations", json_body=validated)
            timer.mark("http")
        except Exception as e:
            return make_result(
                "skyvern_org_update",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Verify field types and ranges"),
            )

    return make_result("skyvern_org_update", data=data, timing_ms=timer.timing_ms)
