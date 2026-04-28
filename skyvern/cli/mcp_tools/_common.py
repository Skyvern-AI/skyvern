"""Backward-compatible re-exports from skyvern.cli.core.

MCP tools import from here; the canonical implementations live in core/.
"""

from __future__ import annotations

from typing import Any

from skyvern.cli.core.artifacts import get_artifact_dir, save_artifact
from skyvern.cli.core.result import Artifact, BrowserContext, ErrorCode, Timer, make_error, make_result
from skyvern.client.errors import NotFoundError


async def raw_http_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET request to Skyvern API for endpoints without SDK methods.

    Raises NotFoundError on 404, RuntimeError on other HTTP errors.
    """
    return await _raw_http_request("GET", path, params=params)


async def raw_http_put(path: str, json_body: dict[str, Any] | None = None) -> Any:
    """PUT request to Skyvern API for endpoints without SDK methods.

    Raises NotFoundError on 404, RuntimeError on other HTTP errors.
    """
    return await _raw_http_request("PUT", path, json_body=json_body)


async def _raw_http_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    from ._session import get_skyvern

    skyvern = get_skyvern()
    # Temporary workaround: these MCP routes do not have public Fern SDK methods yet,
    # so we reach through the generated client's private wrapper. Revisit if the SDK
    # is regenerated or adds first-class methods for these endpoints.
    kwargs: dict[str, Any] = {"method": method, "params": params or {}}
    if json_body is not None:
        kwargs["json"] = json_body
    response = await skyvern._client_wrapper.httpx_client.request(path, **kwargs)
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Not found: {path}"})
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


__all__ = [
    "Artifact",
    "BrowserContext",
    "ErrorCode",
    "Timer",
    "get_artifact_dir",
    "make_error",
    "make_result",
    "raw_http_get",
    "raw_http_put",
    "save_artifact",
]
