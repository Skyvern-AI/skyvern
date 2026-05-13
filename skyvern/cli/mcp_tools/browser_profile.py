"""Skyvern MCP browser profile tools — cloud-durable auth state persistence.

Browser profiles store cookies, localStorage, and the full user-data-dir from a
browser session or workflow run, so future sessions can resume a logged-in state
without re-authenticating. These tools expose the cloud `/v1/browser_profiles`
surface as MCP calls so agents can manage durable auth state without dropping
out to the REST API.

This complements `skyvern_state_save` / `skyvern_state_load` (local-file state)
with a cloud-durable option: pass the returned `browser_profile_id` as
`browser_profile_id` when creating a new session or running a workflow.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from pydantic import Field

from skyvern.client.core.api_error import ApiError
from skyvern.client.errors import BadRequestError, ConflictError, NotFoundError

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern
from ._validation import validate_browser_profile_id, validate_workflow_run_id

LOG = structlog.get_logger()


def _extract_detail(exc: Exception) -> str:
    """Pull a human-readable detail from a Fern client error body."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    message = str(exc)
    return message if message else "request rejected"


def _profile_to_dict(profile: Any) -> dict[str, Any]:
    """Normalize a BrowserProfile response into a JSON-serializable dict.

    BrowserProfile may be a pydantic model (attributes) or a dict-like response
    depending on client version. Handle both.
    """
    if isinstance(profile, dict):
        source: dict[str, Any] = profile
    else:
        dumper = getattr(profile, "model_dump", None)
        if callable(dumper):
            source = dumper(mode="json")
        else:
            source = {
                "browser_profile_id": getattr(profile, "browser_profile_id", None),
                "organization_id": getattr(profile, "organization_id", None),
                "name": getattr(profile, "name", None),
                "description": getattr(profile, "description", None),
                "created_at": getattr(profile, "created_at", None),
                "modified_at": getattr(profile, "modified_at", None),
                "deleted_at": getattr(profile, "deleted_at", None),
            }

    result: dict[str, Any] = {}
    for key in (
        "browser_profile_id",
        "organization_id",
        "name",
        "description",
        "created_at",
        "modified_at",
        "deleted_at",
    ):
        value = source.get(key)
        if value is None:
            result[key] = None
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


async def skyvern_browser_profile_list(
    include_deleted: Annotated[
        bool,
        Field(description="Include soft-deleted profiles in the response."),
    ] = False,
) -> dict[str, Any]:
    """List browser profiles for the current organization. Returns profile metadata
    including browser_profile_id (starts with bp_), name, description, created_at,
    and modified_at. Use the returned browser_profile_id when creating a new
    browser session or running a workflow to resume an authenticated state."""
    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            profiles = await skyvern.list_browser_profiles(include_deleted=include_deleted)
            timer.mark("api")
        except Exception as e:
            LOG.error("browser_profile_list_failed", error=str(e))
            return make_result(
                "skyvern_browser_profile_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and network connection"),
            )

    items = [_profile_to_dict(p) for p in (profiles or [])]
    return make_result(
        "skyvern_browser_profile_list",
        data={"browser_profiles": items, "count": len(items)},
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_profile_get(
    browser_profile_id: Annotated[
        str,
        Field(description="Browser profile ID (starts with bp_)."),
    ],
) -> dict[str, Any]:
    """Get a single browser profile by ID. Returns full profile metadata. Use
    skyvern_browser_profile_list first to discover available profile IDs."""
    if err := validate_browser_profile_id(browser_profile_id, "skyvern_browser_profile_get"):
        return err

    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            profile = await skyvern.get_browser_profile(browser_profile_id)
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_browser_profile_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Browser profile {browser_profile_id!r} not found",
                    "Use skyvern_browser_profile_list to discover valid browser_profile_ids",
                ),
            )
        except Exception as e:
            LOG.error("browser_profile_get_failed", browser_profile_id=browser_profile_id, error=str(e))
            return make_result(
                "skyvern_browser_profile_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the profile ID and your API key"),
            )

    return make_result(
        "skyvern_browser_profile_get",
        data=_profile_to_dict(profile),
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_profile_create(
    name: Annotated[
        str,
        Field(description="Human-readable name for the browser profile. Must be unique within the organization."),
    ],
    browser_session_id: Annotated[
        str | None,
        Field(
            description="Persistent browser session to snapshot (starts with pbs_). Provide EXACTLY ONE of browser_session_id OR workflow_run_id."
        ),
    ] = None,
    workflow_run_id: Annotated[
        str | None,
        Field(
            description="Workflow run whose persisted session should be captured (starts with wr_; tsk_v2_ task IDs are NOT accepted). Provide EXACTLY ONE of browser_session_id OR workflow_run_id."
        ),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional free-text description of what this profile represents."),
    ] = None,
) -> dict[str, Any]:
    """Create a cloud-durable browser profile by snapshotting the authenticated
    state of a browser session or workflow run. Returns the new browser_profile_id.

    Provide exactly one of browser_session_id OR workflow_run_id. Subsequent
    sessions/runs can pass browser_profile_id to resume the logged-in state
    without re-authenticating.

    IMPORTANT: the browser session's persisted archive uploads asynchronously
    AFTER the session closes. The typical correct sequence for a session_id
    source is: sign in -> skyvern_browser_session_close(...) -> retry-loop
    skyvern_browser_profile_create(browser_session_id=...) until the archive
    is ready (roughly 5-10 seconds after close). Calling this tool against an
    OPEN session returns ARCHIVE_NOT_READY and will never succeed until the
    session is closed.

    For workflow_run_id sources (wr_...), the server polls internally for up
    to 30 seconds so a close-then-call dance is not required. tsk_v2_ IDs are
    not accepted here."""
    if not name or not name.strip():
        return make_result(
            "skyvern_browser_profile_create",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "name is required and must be non-empty",
                "Provide a descriptive profile name, e.g. 'example-app-signed-in'",
            ),
        )

    # Require exactly one of the two sources: XOR via bool-equality.
    # Equality is True when both are None (neither provided) or both are truthy
    # (both provided) — in either case we reject with the same INVALID_INPUT.
    if bool(browser_session_id) == bool(workflow_run_id):
        return make_result(
            "skyvern_browser_profile_create",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Provide exactly ONE of browser_session_id or workflow_run_id",
                "Set browser_session_id (pbs_...) for an active session, or workflow_run_id (wr_...) for a completed workflow run",
            ),
        )

    if browser_session_id is not None:
        if "/" in browser_session_id or "\\" in browser_session_id:
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "browser_session_id must not contain path separators",
                    "Provide a valid browser session ID (starts with pbs_)",
                ),
            )
        if not browser_session_id.startswith("pbs_"):
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid browser_session_id format: {browser_session_id!r}",
                    "Browser session IDs start with pbs_. Use skyvern_browser_session_list to find valid IDs.",
                ),
            )

    if workflow_run_id is not None:
        if err := validate_workflow_run_id(workflow_run_id, "skyvern_browser_profile_create"):
            return err

    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            profile = await skyvern.create_browser_profile(
                name=name,
                description=description,
                browser_session_id=browser_session_id,
                workflow_run_id=workflow_run_id,
            )
            timer.mark("api")
        except ConflictError:
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"A browser profile named {name!r} already exists",
                    "Choose a different name, or delete the existing profile with skyvern_browser_profile_delete",
                ),
            )
        except BadRequestError as e:
            # Most common 400 is "session archive not ready yet" — the server-side
            # upload pipeline is asynchronous after session close. Surface a clear,
            # retryable hint so agents know to wait instead of giving up.
            detail = _extract_detail(e)
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.ACTION_FAILED,
                    f"Profile creation rejected: {detail}",
                    "If the session recently closed, the profile archive may still be uploading. Wait 5-10 seconds and retry.",
                ),
            )
        except ApiError as e:
            # The Fern-generated create_browser_profile client only explicitly
            # maps 400/409/422; a 404 (bad browser_session_id or
            # workflow_run_id source) is raised as a generic ApiError, not
            # NotFoundError. Catch ApiError and key off status_code so the
            # 404-routing fires in production, not only in tests that happen
            # to raise NotFoundError directly.
            if getattr(e, "status_code", None) == 404:
                detail = ""
                if isinstance(e.body, dict):
                    detail = str(e.body.get("detail") or "")
                return make_result(
                    "skyvern_browser_profile_create",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        detail or "Source browser_session_id or workflow_run_id not found",
                        "Verify the source ID with skyvern_browser_session_list or skyvern_workflow_status",
                    ),
                )
            LOG.error(
                "browser_profile_create_failed",
                name=name,
                browser_session_id=browser_session_id,
                workflow_run_id=workflow_run_id,
                error=str(e),
            )
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the source ID and your API key"),
            )
        except Exception as e:
            LOG.error(
                "browser_profile_create_failed",
                name=name,
                browser_session_id=browser_session_id,
                workflow_run_id=workflow_run_id,
                error=str(e),
            )
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the source ID and your API key"),
            )

    return make_result(
        "skyvern_browser_profile_create",
        data=_profile_to_dict(profile),
        timing_ms=timer.timing_ms,
    )


async def skyvern_browser_profile_delete(
    browser_profile_id: Annotated[
        str,
        Field(
            description="Browser profile ID to delete (starts with bp_). Soft delete — the profile is marked deleted but not immediately purged."
        ),
    ],
) -> dict[str, Any]:
    """Soft-delete a browser profile. The profile is marked deleted but not
    immediately purged; it will stop appearing in skyvern_browser_profile_list
    unless include_deleted=true is passed. Existing sessions that loaded this
    profile continue to work until they close."""
    if err := validate_browser_profile_id(browser_profile_id, "skyvern_browser_profile_delete"):
        return err

    with Timer() as timer:
        try:
            skyvern = get_skyvern()
            await skyvern.delete_browser_profile(browser_profile_id)
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_browser_profile_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Browser profile {browser_profile_id!r} not found",
                    "Use skyvern_browser_profile_list to discover valid browser_profile_ids",
                ),
            )
        except Exception as e:
            LOG.error("browser_profile_delete_failed", browser_profile_id=browser_profile_id, error=str(e))
            return make_result(
                "skyvern_browser_profile_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the profile ID and your API key"),
            )

    return make_result(
        "skyvern_browser_profile_delete",
        data={"browser_profile_id": browser_profile_id, "deleted": True},
        timing_ms=timer.timing_ms,
    )
