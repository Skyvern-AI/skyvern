"""Raw workflow HTTP helpers for MCP tools.

Keep workflow definitions as plain dicts here. Constructing Fern-generated
workflow/block models in MCP can reject backend-valid block types when the
vendored SDK is behind the backend enum.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.client.errors import BadRequestError, NotFoundError

from ._session import get_skyvern

LOG = structlog.get_logger()

_ERROR_DETAIL_LIMIT = 500

PUBLIC_WORKFLOW_ROUTE = "v1/workflows"
INTERNAL_WORKFLOW_ROUTE = "api/v1/workflows"


def coerce_timestamp(value: Any) -> str | None:
    """Normalize a timestamp coming from either a raw dict or a Fern model."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    LOG.debug("Unexpected timestamp type in workflow response", value_type=type(value).__name__)
    return str(value)


def extract_error_detail(response: Any) -> str:
    """Pull a readable detail out of an httpx Response, bounded by _ERROR_DETAIL_LIMIT.

    Truncation is applied uniformly to both JSON-path and non-JSON-path outputs,
    because a verbose ``detail`` field (e.g., a backend stack trace) can be just
    as unbounded as an HTML error page.
    """
    try:
        body = response.json()
    except Exception:
        detail = str(getattr(response, "text", ""))
    else:
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("error") or body)
        else:
            detail = str(body)
    return detail[:_ERROR_DETAIL_LIMIT] if len(detail) > _ERROR_DETAIL_LIMIT else detail


def _decode_success_payload(response: Any, *, operation: str) -> Any:
    """Decode a successful JSON response with operation context for clearer MCP errors."""
    try:
        return response.json()
    except Exception as exc:
        detail = extract_error_detail(response)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Unexpected {operation} response: failed to decode JSON body{suffix}") from exc


async def list_workflows_raw(
    *,
    search: str | None,
    page: int,
    page_size: int,
    only_workflows: bool,
) -> list[dict[str, Any]]:
    """GET /v1/workflows — returns a list of raw workflow dicts."""
    skyvern = get_skyvern()
    params: dict[str, Any] = {
        "page": page,
        "page_size": page_size,
        "only_workflows": only_workflows,
    }
    if search is not None:
        params["search_key"] = search
    response = await skyvern._client_wrapper.httpx_client.request(
        PUBLIC_WORKFLOW_ROUTE,
        method="GET",
        params=params,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="workflows list")
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected workflows list payload: {type(payload).__name__}")
    return payload


async def create_workflow_raw(
    *,
    json_definition: dict[str, Any] | None,
    yaml_definition: str | None,
    folder_id: str | None,
) -> dict[str, Any]:
    """POST /v1/workflows — returns the created workflow as a raw dict."""
    skyvern = get_skyvern()
    body: dict[str, Any] = {}
    if json_definition is not None:
        body["json_definition"] = json_definition
    if yaml_definition is not None:
        body["yaml_definition"] = yaml_definition
    params: dict[str, Any] = {}
    if folder_id is not None:
        params["folder_id"] = folder_id
    response = await skyvern._client_wrapper.httpx_client.request(
        PUBLIC_WORKFLOW_ROUTE,
        method="POST",
        params=params,
        json=body,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="create_workflow")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected create_workflow payload: {type(payload).__name__}")
    return payload


async def update_workflow_raw(
    workflow_id: str,
    *,
    json_definition: dict[str, Any] | None,
    yaml_definition: str | None,
) -> dict[str, Any]:
    """POST /v1/workflows/{id} — backend update endpoint uses POST, not PUT/PATCH."""
    skyvern = get_skyvern()
    body: dict[str, Any] = {}
    if json_definition is not None:
        body["json_definition"] = json_definition
    if yaml_definition is not None:
        body["yaml_definition"] = yaml_definition
    response = await skyvern._client_wrapper.httpx_client.request(
        f"{PUBLIC_WORKFLOW_ROUTE}/{workflow_id}",
        method="POST",
        json=body,
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow {workflow_id!r} not found"})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="update_workflow")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected update_workflow payload: {type(payload).__name__}")
    return payload


async def update_workflow_folder_raw(workflow_id: str, *, folder_id: str | None) -> dict[str, Any]:
    """PUT /v1/workflows/{id}/folder — returns the updated workflow as a raw dict."""
    skyvern = get_skyvern()
    response = await skyvern._client_wrapper.httpx_client.request(
        f"{PUBLIC_WORKFLOW_ROUTE}/{workflow_id}/folder",
        method="PUT",
        json={"folder_id": folder_id},
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow {workflow_id!r} not found"})
    if response.status_code == 400:
        raise BadRequestError(body={"detail": extract_error_detail(response)})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="update_workflow_folder")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected update_workflow_folder payload: {type(payload).__name__}")
    return payload


async def get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, Any]:
    """GET /api/v1/workflows/{id} — returns a single workflow as a raw dict.

    Uses ``INTERNAL_WORKFLOW_ROUTE`` because the Fern-generated client's public
    ``get_workflow(id)`` does not expose the ``version`` parameter we rely on
    for historic-version lookups. Revisit on next Fern regen (see SKY-7807).
    """
    skyvern = get_skyvern()
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    response = await skyvern._client_wrapper.httpx_client.request(
        f"{INTERNAL_WORKFLOW_ROUTE}/{workflow_id}",
        method="GET",
        params=params,
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow {workflow_id!r} not found"})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="get_workflow")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected get_workflow payload: {type(payload).__name__}")
    return payload


async def get_workflow_run_status(
    workflow_run_id: str,
    *,
    include_output_details: bool,
) -> dict[str, Any]:
    """GET /api/v1/workflows/runs/{id} — returns workflow run detail as a raw dict.

    Uses ``INTERNAL_WORKFLOW_ROUTE`` because the Fern SDK's ``get_run()`` only
    covers the unified ``/v1/runs/{id}`` surface and doesn't accept
    ``include_output_details`` for ``wr_`` ids.
    """
    skyvern = get_skyvern()
    response = await skyvern._client_wrapper.httpx_client.request(
        f"{INTERNAL_WORKFLOW_ROUTE}/runs/{workflow_run_id}",
        method="GET",
        params={"include_output_details": include_output_details},
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow run {workflow_run_id!r} not found"})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {extract_error_detail(response)}")
    payload = _decode_success_payload(response, operation="get_workflow_run_status")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected get_workflow_run_status payload: {type(payload).__name__}")
    return payload
