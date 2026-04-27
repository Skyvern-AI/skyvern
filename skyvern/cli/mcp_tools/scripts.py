"""Skyvern MCP script tools — visibility into cached scripts and fallback episodes.

Tools for listing scripts, viewing generated code, checking version history,
inspecting AI fallback episodes, and deploying updated script versions.
These tools do not require a browser session.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
from pydantic import Field, ValidationError

from skyvern.client.errors import NotFoundError
from skyvern.client.types import ScriptFileCreate

from ._common import ErrorCode, Timer, make_error, make_result, raw_http_get
from ._session import get_skyvern
from ._validation import validate_run_id, validate_script_id, validate_workflow_id

LOG = structlog.get_logger()


# ---------------------------------------------------------------------------
# Script tools
# ---------------------------------------------------------------------------


async def skyvern_script_list_for_workflow(
    workflow_id: Annotated[str, Field(description="Workflow permanent ID (starts with wpid_)")],
) -> dict[str, Any]:
    """List all cached scripts for a workflow. Use this as the entry point to discover
    script IDs for a given workflow. Returns script metadata including version count,
    success rate, and cache key information."""
    if err := validate_workflow_id(workflow_id, "skyvern_script_list_for_workflow"):
        return err

    with Timer() as timer:
        try:
            data = await raw_http_get(f"v1/scripts/workflows/{workflow_id}")
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_script_list_for_workflow",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list",
                ),
            )
        except Exception as e:
            LOG.error("script_list_for_workflow_failed", workflow_id=workflow_id, error=str(e))
            return make_result(
                "skyvern_script_list_for_workflow",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the workflow ID and your API key"),
            )

    raw_scripts = data.get("scripts", []) if isinstance(data, dict) else data
    scripts: Any = []
    if isinstance(raw_scripts, list):
        for script in raw_scripts:
            if not isinstance(script, dict):
                scripts.append(script)
                continue
            script_data = dict(script)
            if "version" not in script_data and "latest_version" in script_data:
                script_data["version"] = script_data["latest_version"]
            scripts.append(script_data)
    else:
        scripts = raw_scripts
    count = len(scripts) if isinstance(scripts, list) else 0
    return make_result(
        "skyvern_script_list_for_workflow",
        data={"workflow_id": workflow_id, "scripts": scripts, "count": count},
        timing_ms=timer.timing_ms,
    )


async def skyvern_script_get_code(
    script_id: Annotated[str, Field(description="Script ID (starts with s_)")],
    version: Annotated[int | None, Field(description="Version number. Omit to get the latest version.")] = None,
) -> dict[str, Any]:
    """Get the generated Python code for a cached script. Returns the main orchestrator
    script and per-block code. Use skyvern_script_list_for_workflow to find script IDs first."""
    if err := validate_script_id(script_id, "skyvern_script_get_code"):
        return err

    with Timer() as timer:
        try:
            if version is None:
                script_meta = await raw_http_get(f"v1/scripts/{script_id}")
                timer.mark("resolve_version")
                version = script_meta.get("version", 1) if isinstance(script_meta, dict) else 1

            data = await raw_http_get(f"v1/scripts/{script_id}/versions/{version}")
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_script_get_code",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Script {script_id!r} version {version} not found",
                    "Use skyvern_script_versions to see available versions",
                ),
            )
        except Exception as e:
            LOG.error("script_get_code_failed", script_id=script_id, version=version, error=str(e))
            return make_result(
                "skyvern_script_get_code",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the script ID and your API key"),
            )

    result: dict[str, Any] = {
        "script_id": script_id,
        "version": version,
    }
    if isinstance(data, dict):
        result["blocks"] = data.get("blocks", {})
        result["main_script"] = data.get("main_script")
    return make_result("skyvern_script_get_code", data=result, timing_ms=timer.timing_ms)


async def skyvern_script_versions(
    script_id: Annotated[str, Field(description="Script ID (starts with s_)")],
) -> dict[str, Any]:
    """List all versions of a cached script. Shows version history including
    creation timestamps and which run triggered each version."""
    if err := validate_script_id(script_id, "skyvern_script_versions"):
        return err

    with Timer() as timer:
        try:
            data = await raw_http_get(f"v1/scripts/{script_id}/versions")
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_script_versions",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Script {script_id!r} not found",
                    "Use skyvern_script_list_for_workflow to find valid script IDs",
                ),
            )
        except Exception as e:
            LOG.error("script_versions_failed", script_id=script_id, error=str(e))
            return make_result(
                "skyvern_script_versions",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the script ID and your API key"),
            )

    versions = data.get("versions", []) if isinstance(data, dict) else data
    return make_result(
        "skyvern_script_versions",
        data={"script_id": script_id, "versions": versions, "count": len(versions)},
        timing_ms=timer.timing_ms,
    )


async def skyvern_script_fallback_episodes(
    workflow_id: Annotated[str, Field(description="Workflow permanent ID (starts with wpid_)")],
    workflow_run_id: Annotated[str | None, Field(description="Filter to a specific run (starts with wr_)")] = None,
    block_label: Annotated[str | None, Field(description="Filter to a specific block label")] = None,
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """List AI fallback episodes for a workflow's cached scripts. Each episode records
    when a cached script's selector failed and the AI agent took over. Shows error details,
    block label, and whether the fallback succeeded. Useful for understanding why a script
    fell back to AI and how the script evolved."""
    if err := validate_workflow_id(workflow_id, "skyvern_script_fallback_episodes"):
        return err
    if workflow_run_id is not None:
        if err := validate_run_id(workflow_run_id, "skyvern_script_fallback_episodes"):
            return err

    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if workflow_run_id is not None:
        params["workflow_run_id"] = workflow_run_id
    if block_label is not None:
        params["block_label"] = block_label

    with Timer() as timer:
        try:
            data = await raw_http_get(f"v1/workflows/{workflow_id}/fallback-episodes", params=params)
            timer.mark("api")
        except NotFoundError:
            return make_result(
                "skyvern_script_fallback_episodes",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list",
                ),
            )
        except Exception as e:
            LOG.error("script_fallback_episodes_failed", workflow_id=workflow_id, error=str(e))
            return make_result(
                "skyvern_script_fallback_episodes",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the workflow ID and your API key"),
            )

    result: dict[str, Any] = {"workflow_id": workflow_id}
    if isinstance(data, dict):
        result["episodes"] = data.get("episodes", [])
        result["total_count"] = data.get("total_count", 0)
        result["page"] = data.get("page", page)
        result["page_size"] = data.get("page_size", page_size)
    else:
        result["episodes"] = data
        result["total_count"] = len(data) if isinstance(data, list) else 0
    return make_result("skyvern_script_fallback_episodes", data=result, timing_ms=timer.timing_ms)


async def skyvern_script_deploy(
    script_id: Annotated[str, Field(description="Script ID to deploy a new version for (starts with s_)")],
    files: Annotated[
        str,
        Field(
            description='JSON array of file objects: [{"path": "main.py", "content": "<base64-encoded>", "encoding": "base64"}]'
        ),
    ],
) -> dict[str, Any]:
    """Deploy a new version of a cached script with updated files. Creates a new version
    that will be used on the next workflow run. File content must be base64-encoded."""
    if err := validate_script_id(script_id, "skyvern_script_deploy"):
        return err

    try:
        parsed_files = json.loads(files)
        if not isinstance(parsed_files, list):
            raise ValueError("files must be a JSON array")
        typed_files = [ScriptFileCreate(**file_data) for file_data in parsed_files]
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as e:
        return make_result(
            "skyvern_script_deploy",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid files JSON: {e}",
                'Provide a JSON array: [{"path": "main.py", "content": "<base64>", "encoding": "base64"}]',
            ),
        )

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            result = await skyvern.deploy_script(script_id, files=typed_files)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_script_deploy",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Script {script_id!r} not found",
                    "Use skyvern_script_list_for_workflow to find valid script IDs",
                ),
            )
        except Exception as e:
            LOG.error("script_deploy_failed", script_id=script_id, error=str(e))
            return make_result(
                "skyvern_script_deploy",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the script ID and your API key"),
            )

    data: dict[str, Any] = {"script_id": script_id}
    if hasattr(result, "model_dump"):
        data.update(result.model_dump(mode="json"))
    elif isinstance(result, dict):
        data.update(result)
    return make_result("skyvern_script_deploy", data=data, timing_ms=timer.timing_ms)
