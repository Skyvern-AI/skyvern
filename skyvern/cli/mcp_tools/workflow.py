"""Skyvern MCP workflow tools — CRUD and execution for Skyvern workflows.

Tools for listing, creating, updating, deleting, running, and monitoring
Skyvern workflows via the Skyvern HTTP API. These tools do not require a
browser session.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import structlog
from pydantic import Field

from skyvern.client.errors import NotFoundError
from skyvern.client.types import WorkflowCreateYamlRequest
from skyvern.schemas.runs import ProxyLocation

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern

LOG = structlog.get_logger()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_workflow(wf: Any) -> dict[str, Any]:
    """Pick the fields we expose from a Workflow Pydantic model.

    Uses Any to avoid tight coupling with Fern-generated client types.
    """
    return {
        "workflow_permanent_id": wf.workflow_permanent_id,
        "workflow_id": wf.workflow_id,
        "title": wf.title,
        "version": wf.version,
        "status": str(wf.status) if wf.status else None,
        "description": wf.description,
        "is_saved_task": wf.is_saved_task,
        "folder_id": wf.folder_id,
        "created_at": wf.created_at.isoformat() if wf.created_at else None,
        "modified_at": wf.modified_at.isoformat() if wf.modified_at else None,
    }


def _serialize_workflow_full(wf: Any) -> dict[str, Any]:
    """Like _serialize_workflow but includes the full definition."""
    data = _serialize_workflow(wf)
    if hasattr(wf, "workflow_definition") and wf.workflow_definition is not None:
        try:
            data["workflow_definition"] = wf.workflow_definition.model_dump(mode="json")
        except Exception:
            data["workflow_definition"] = str(wf.workflow_definition)
    return data


def _serialize_run(run: Any) -> dict[str, Any]:
    """Pick fields from a run response (GetRunResponse variant or WorkflowRunResponse).

    Uses Any to avoid tight coupling with Fern-generated client types.
    """
    data: dict[str, Any] = {
        "run_id": run.run_id,
        "status": str(run.status) if run.status else None,
    }
    for field in (
        "run_type",
        "step_count",
        "failure_reason",
        "recording_url",
        "app_url",
        "browser_session_id",
        "run_with",
    ):
        val = getattr(run, field, None)
        if val is not None:
            data[field] = str(val) if not isinstance(val, (str, int, bool)) else val

    if hasattr(run, "output") and run.output is not None:
        try:
            data["output"] = run.output.model_dump(mode="json") if hasattr(run.output, "model_dump") else run.output
        except Exception:
            data["output"] = str(run.output)

    for ts_field in ("created_at", "modified_at", "started_at", "finished_at", "queued_at"):
        val = getattr(run, ts_field, None)
        if val is not None:
            data[ts_field] = val.isoformat()

    return data


def _validate_workflow_id(workflow_id: str, action: str) -> dict[str, Any] | None:
    """Validate workflow_id format. Returns a make_result error dict or None if valid."""
    if "/" in workflow_id or "\\" in workflow_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "workflow_id must not contain path separators",
                "Provide a valid workflow permanent ID (starts with wpid_)",
            ),
        )
    if not workflow_id.startswith("wpid_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid workflow_id format: {workflow_id!r}",
                "Workflow IDs start with wpid_. Use skyvern_workflow_list to find valid IDs.",
            ),
        )
    return None


def _validate_run_id(run_id: str, action: str) -> dict[str, Any] | None:
    """Validate run_id format. Returns a make_result error dict or None if valid."""
    if "/" in run_id or "\\" in run_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "run_id must not contain path separators",
                "Provide a valid run ID (starts with wr_ or tsk_v2_)",
            ),
        )
    if not run_id.startswith("wr_") and not run_id.startswith("tsk_v2_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid run_id format: {run_id!r}",
                "Run IDs start with wr_ (workflow runs) or tsk_v2_ (task runs). Check skyvern_workflow_run output.",
            ),
        )
    return None


async def _get_workflow_by_id(workflow_id: str, version: int | None = None) -> dict[str, Any]:
    """Fetch a single workflow by ID via the Skyvern API.

    The Fern-generated client has get_workflows() (list) but no get_workflow(id).
    This helper isolates the private client access so the workaround is contained
    in one place. Replace with ``skyvern.get_workflow(id)`` when the SDK adds it.

    Raises NotFoundError on 404, or RuntimeError on other HTTP errors, so callers
    can use the same ``except NotFoundError`` pattern as all other workflow tools.
    """
    skyvern = get_skyvern()
    params: dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    # TODO(SKY-7807): Replace with skyvern.get_workflow() when the Fern client adds it.
    response = await skyvern._client_wrapper.httpx_client.request(
        f"api/v1/workflows/{workflow_id}",
        method="GET",
        params=params,
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow {workflow_id!r} not found"})
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    return response.json()


def _validate_definition_structure(json_def: WorkflowCreateYamlRequest | None, action: str) -> dict[str, Any] | None:
    """Validate required fields in a JSON workflow definition.

    Returns a make_result error dict if validation fails, or None if valid.
    Only validates JSON definitions — YAML is validated server-side.
    Note: WorkflowCreateYamlRequest already enforces ``title`` and
    ``workflow_definition`` as required fields via Pydantic, so this is
    a belt-and-suspenders check that produces user-friendly error messages.
    """
    if json_def is None:
        return None
    if not json_def.title:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Workflow definition missing 'title' field",
                "Add a 'title' field to your workflow definition",
            ),
        )
    return None


def _parse_definition(
    definition: str, fmt: str
) -> tuple[WorkflowCreateYamlRequest | None, str | None, dict[str, Any] | None]:
    """Parse a workflow definition string.

    Returns (json_definition, yaml_definition, error).
    Exactly one of the first two will be set on success, or error on failure.
    JSON input is parsed into a WorkflowCreateYamlRequest (the type the SDK expects).
    """
    if fmt == "json":
        try:
            raw = json.loads(definition)
            return WorkflowCreateYamlRequest(**raw), None, None
        except (json.JSONDecodeError, TypeError) as e:
            return (
                None,
                None,
                make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid JSON definition: {e}",
                    "Provide a valid JSON object for the workflow definition",
                ),
            )
        except Exception as e:
            return (
                None,
                None,
                make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid workflow definition: {e}",
                    "Check the workflow definition fields (title, workflow_definition with blocks)",
                ),
            )
    elif fmt == "yaml":
        return None, definition, None
    else:
        # auto: try JSON first, fall back to YAML
        try:
            raw = json.loads(definition)
            return WorkflowCreateYamlRequest(**raw), None, None
        except (json.JSONDecodeError, TypeError):
            return None, definition, None
        except Exception:
            # JSON parsed but failed model validation — treat as YAML
            return None, definition, None


# ---------------------------------------------------------------------------
# SKY-7807: Workflow CRUD
# ---------------------------------------------------------------------------


async def skyvern_workflow_list(
    search: Annotated[str | None, "Search across workflow titles, folder names, and parameter metadata"] = None,
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 10,
    only_workflows: Annotated[bool, "Only return multi-step workflows (exclude saved tasks)"] = False,
) -> dict[str, Any]:
    """Find and browse available Skyvern workflows. Use when you need to discover what workflows exist,
    search for a workflow by name, or list all workflows for an organization."""
    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            workflows = await skyvern.get_workflows(
                search_key=search,
                page=page,
                page_size=page_size,
                only_workflows=only_workflows,
            )
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_workflow_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    return make_result(
        "skyvern_workflow_list",
        data={
            "workflows": [_serialize_workflow(wf) for wf in workflows],
            "page": page,
            "page_size": page_size,
            "count": len(workflows),
            "has_more": len(workflows) == page_size,
            "sdk_equivalent": f"await skyvern.get_workflows(search_key={search!r}, page={page}, page_size={page_size})",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_workflow_get(
    workflow_id: Annotated[str, "Workflow permanent ID (starts with wpid_)"],
    version: Annotated[int | None, "Specific version to retrieve (latest if omitted)"] = None,
) -> dict[str, Any]:
    """Get the full definition of a specific workflow. Use when you need to inspect a workflow's
    blocks, parameters, and configuration before running or updating it."""
    if err := _validate_workflow_id(workflow_id, "skyvern_workflow_get"):
        return err

    with Timer() as timer:
        try:
            wf_data = await _get_workflow_by_id(workflow_id, version)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_workflow_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and workflow ID"),
            )

    version_str = f", version={version}" if version is not None else ""
    return make_result(
        "skyvern_workflow_get",
        data={
            **wf_data,
            "sdk_equivalent": f"# No SDK method yet — GET /api/v1/workflows/{workflow_id}{version_str}",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_workflow_create(
    definition: Annotated[str, "Workflow definition as a YAML or JSON string"],
    format: Annotated[  # noqa: A002
        str, Field(description="Definition format: 'json', 'yaml', or 'auto' (tries JSON first, falls back to YAML)")
    ] = "auto",
    folder_id: Annotated[str | None, "Folder ID (fld_...) to organize the workflow in"] = None,
) -> dict[str, Any]:
    """Create a new Skyvern workflow from a YAML or JSON definition. Use when you need to save
    a new automation workflow that can be run repeatedly with different parameters.

    Best practice: use one block per logical step with a short focused prompt (2-3 sentences).
    Use "navigation" blocks for actions (filling forms, clicking) and "extraction" blocks for pulling data.
    Do NOT use the deprecated "task" block type.
    Common block types: navigation, extraction, for_loop, conditional, code, text_prompt, action, wait, login.
    Call skyvern_block_schema() for the full list with schemas and examples.

    Example JSON definition (multi-block EIN application):

        {
          "title": "Apply for EIN",
          "workflow_definition": {
            "parameters": [
              {"parameter_type": "workflow", "key": "business_name", "workflow_parameter_type": "string"},
              {"parameter_type": "workflow", "key": "owner_name", "workflow_parameter_type": "string"},
              {"parameter_type": "workflow", "key": "owner_ssn", "workflow_parameter_type": "string"}
            ],
            "blocks": [
              {"block_type": "navigation", "label": "select_entity_type",
               "url": "https://sa.www4.irs.gov/modiein/individual/index.jsp",
               "title": "Select Entity Type",
               "navigation_goal": "Select 'Sole Proprietor' as the entity type and click Continue."},
              {"block_type": "navigation", "label": "enter_business_info",
               "title": "Enter Business Info",
               "navigation_goal": "Fill in the business name as '{{business_name}}' and click Continue.",
               "parameter_keys": ["business_name"]},
              {"block_type": "navigation", "label": "enter_owner_info",
               "title": "Enter Owner Info",
               "navigation_goal": "Enter the responsible party name '{{owner_name}}' and SSN '{{owner_ssn}}'. Click Continue.",
               "parameter_keys": ["owner_name", "owner_ssn"]},
              {"block_type": "extraction", "label": "extract_ein",
               "title": "Extract EIN",
               "data_extraction_goal": "Extract the assigned EIN number from the confirmation page",
               "data_schema": {"type": "object", "properties": {"ein": {"type": "string"}}}}
            ]
          }
        }

    Use {{parameter_key}} to reference workflow input parameters in any block field.
    Blocks in the same run share the same browser session automatically.
    """
    if format not in ("json", "yaml", "auto"):
        return make_result(
            "skyvern_workflow_create",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid format: {format!r}",
                "Use 'json', 'yaml', or 'auto'",
            ),
        )

    json_def, yaml_def, parse_err = _parse_definition(definition, format)
    if parse_err is not None:
        return make_result("skyvern_workflow_create", ok=False, error=parse_err)

    if err := _validate_definition_structure(json_def, "skyvern_workflow_create"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            workflow = await skyvern.create_workflow(
                json_definition=json_def,
                yaml_definition=yaml_def,
                folder_id=folder_id,
            )
            timer.mark("sdk")
        except Exception as e:
            LOG.error("workflow_create_failed", error=str(e))
            return make_result(
                "skyvern_workflow_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.API_ERROR,
                    str(e),
                    "Check the workflow definition syntax and required fields (title, workflow_definition.blocks)",
                ),
            )

    LOG.info("workflow_created", workflow_id=workflow.workflow_permanent_id)
    data = _serialize_workflow(workflow)
    fmt_label = "json_definition" if json_def is not None else "yaml_definition"
    folder_str = f", folder_id={folder_id!r}" if folder_id is not None else ""
    data["sdk_equivalent"] = f"await skyvern.create_workflow({fmt_label}=<definition>{folder_str})"
    return make_result("skyvern_workflow_create", data=data, timing_ms=timer.timing_ms)


async def skyvern_workflow_update(
    workflow_id: Annotated[str, "Workflow permanent ID (wpid_...) to update"],
    definition: Annotated[str, "Updated workflow definition as a YAML or JSON string"],
    format: Annotated[  # noqa: A002
        str, Field(description="Definition format: 'json', 'yaml', or 'auto'")
    ] = "auto",
) -> dict[str, Any]:
    """Update an existing workflow's definition. Use when you need to modify a workflow's blocks,
    parameters, or configuration. Creates a new version of the workflow."""
    if err := _validate_workflow_id(workflow_id, "skyvern_workflow_update"):
        return err

    if format not in ("json", "yaml", "auto"):
        return make_result(
            "skyvern_workflow_update",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid format: {format!r}",
                "Use 'json', 'yaml', or 'auto'",
            ),
        )

    json_def, yaml_def, parse_err = _parse_definition(definition, format)
    if parse_err is not None:
        return make_result("skyvern_workflow_update", ok=False, error=parse_err)

    if err := _validate_definition_structure(json_def, "skyvern_workflow_update"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            workflow = await skyvern.update_workflow(
                workflow_id,
                json_definition=json_def,
                yaml_definition=yaml_def,
            )
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_workflow_update",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.API_ERROR,
                    str(e),
                    "Check the workflow ID and definition syntax",
                ),
            )

    data = _serialize_workflow(workflow)
    fmt_label = "json_definition" if json_def is not None else "yaml_definition"
    data["sdk_equivalent"] = f"await skyvern.update_workflow({workflow_id!r}, {fmt_label}=<definition>)"
    return make_result("skyvern_workflow_update", data=data, timing_ms=timer.timing_ms)


async def skyvern_workflow_delete(
    workflow_id: Annotated[str, "Workflow permanent ID (wpid_...) to delete"],
    force: Annotated[bool, "Must be true to confirm deletion — prevents accidental deletes"] = False,
) -> dict[str, Any]:
    """Delete a workflow permanently. Use when you need to remove a workflow that is no longer needed.
    Requires force=true to prevent accidental deletion."""
    if err := _validate_workflow_id(workflow_id, "skyvern_workflow_delete"):
        return err

    if not force:
        return make_result(
            "skyvern_workflow_delete",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Deletion of workflow {workflow_id!r} requires confirmation",
                "Set force=true to confirm deletion. This action is irreversible.",
            ),
        )

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            await skyvern.delete_workflow(workflow_id)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list",
                ),
            )
        except Exception as e:
            LOG.error("workflow_delete_failed", workflow_id=workflow_id, error=str(e))
            return make_result(
                "skyvern_workflow_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the workflow ID and your permissions"),
            )

    LOG.info("workflow_deleted", workflow_id=workflow_id)
    return make_result(
        "skyvern_workflow_delete",
        data={
            "workflow_permanent_id": workflow_id,
            "deleted": True,
            "sdk_equivalent": f"await skyvern.delete_workflow({workflow_id!r})",
        },
        timing_ms=timer.timing_ms,
    )


# ---------------------------------------------------------------------------
# SKY-7808: Workflow Execution
# ---------------------------------------------------------------------------


async def skyvern_workflow_run(
    workflow_id: Annotated[str, "Workflow permanent ID (wpid_...) to run"],
    parameters: Annotated[str | None, Field(description="JSON string of workflow parameters")] = None,
    browser_session_id: Annotated[
        str | None, Field(description="Reuse an existing browser session (pbs_...) to preserve login state")
    ] = None,
    webhook_url: Annotated[str | None, Field(description="URL for status webhook callbacks after completion")] = None,
    proxy_location: Annotated[
        str | None, Field(description="Geographic proxy: RESIDENTIAL, RESIDENTIAL_GB, NONE, etc.")
    ] = None,
    wait: Annotated[bool, "Wait for the workflow to complete before returning (default: return immediately)"] = False,
    timeout_seconds: Annotated[
        int, Field(description="Max wait time in seconds when wait=true (default 300)", ge=10, le=3600)
    ] = 300,
) -> dict[str, Any]:
    """Run a Skyvern workflow with parameters. Use when you need to execute an automation workflow.
    Returns immediately by default (async) — set wait=true to block until completion.
    Default timeout is 300s (5 minutes). For longer workflows, increase timeout_seconds
    or use wait=false and poll with skyvern_workflow_status."""
    if err := _validate_workflow_id(workflow_id, "skyvern_workflow_run"):
        return err

    parsed_params: dict[str, Any] | None = None
    if parameters is not None:
        try:
            parsed_params = json.loads(parameters)
        except (json.JSONDecodeError, TypeError) as e:
            return make_result(
                "skyvern_workflow_run",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid parameters JSON: {e}",
                    "Provide parameters as a valid JSON object string",
                ),
            )

    proxy: ProxyLocation | None = None
    if proxy_location is not None:
        try:
            proxy = ProxyLocation(proxy_location)
        except ValueError:
            return make_result(
                "skyvern_workflow_run",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Invalid proxy_location: {proxy_location!r}",
                    "Use RESIDENTIAL, RESIDENTIAL_GB, NONE, etc.",
                ),
            )

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            run = await skyvern.run_workflow(
                workflow_id=workflow_id,
                parameters=parsed_params,
                browser_session_id=browser_session_id,
                webhook_url=webhook_url,
                proxy_location=proxy,
                wait_for_completion=wait,
                timeout=timeout_seconds,
            )
            timer.mark("sdk")
        except asyncio.TimeoutError:
            return make_result(
                "skyvern_workflow_run",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.TIMEOUT,
                    f"Workflow did not complete within {timeout_seconds}s",
                    "Increase timeout_seconds or set wait=false and poll with skyvern_workflow_status",
                ),
            )
        except NotFoundError:
            return make_result(
                "skyvern_workflow_run",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list",
                ),
            )
        except Exception as e:
            LOG.error("workflow_run_failed", workflow_id=workflow_id, error=str(e))
            return make_result(
                "skyvern_workflow_run",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the workflow ID, parameters, and API key"),
            )

    LOG.info("workflow_run_started", workflow_id=workflow_id, run_id=run.run_id, wait=wait)
    data = _serialize_run(run)
    params_str = f", parameters={parsed_params}" if parsed_params else ""
    wait_str = f", wait_for_completion=True, timeout={timeout_seconds}" if wait else ""
    data["sdk_equivalent"] = f"await skyvern.run_workflow(workflow_id={workflow_id!r}{params_str}{wait_str})"
    return make_result("skyvern_workflow_run", data=data, timing_ms=timer.timing_ms)


async def skyvern_workflow_status(
    run_id: Annotated[str, "Run ID to check (wr_... for workflow runs, tsk_v2_... for task runs)"],
) -> dict[str, Any]:
    """Check the status and progress of a workflow or task run. Use when you need to monitor
    a running workflow, check if it completed, or retrieve its output."""
    if err := _validate_run_id(run_id, "skyvern_workflow_status"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            run = await skyvern.get_run(run_id)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_status",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.RUN_NOT_FOUND,
                    f"Run {run_id!r} not found",
                    "Verify the run ID — it should start with wr_ or tsk_v2_",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_workflow_status",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the run ID and your API key"),
            )

    data = _serialize_run(run)
    data["sdk_equivalent"] = f"await skyvern.get_run({run_id!r})"
    return make_result("skyvern_workflow_status", data=data, timing_ms=timer.timing_ms)


async def skyvern_workflow_cancel(
    run_id: Annotated[str, "Run ID to cancel (wr_... for workflow runs, tsk_v2_... for task runs)"],
) -> dict[str, Any]:
    """Cancel a running workflow or task. Use when you need to stop a workflow that is taking
    too long, is stuck, or is no longer needed."""
    if err := _validate_run_id(run_id, "skyvern_workflow_cancel"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            await skyvern.cancel_run(run_id)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_cancel",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.RUN_NOT_FOUND,
                    f"Run {run_id!r} not found",
                    "Verify the run ID — it should start with wr_ or tsk_v2_",
                ),
            )
        except Exception as e:
            LOG.error("workflow_cancel_failed", run_id=run_id, error=str(e))
            return make_result(
                "skyvern_workflow_cancel",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the run ID and your API key"),
            )

    LOG.info("workflow_cancelled", run_id=run_id)
    return make_result(
        "skyvern_workflow_cancel",
        data={
            "run_id": run_id,
            "cancelled": True,
            "sdk_equivalent": f"await skyvern.cancel_run({run_id!r})",
        },
        timing_ms=timer.timing_ms,
    )
