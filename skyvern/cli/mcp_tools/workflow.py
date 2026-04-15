"""Skyvern MCP workflow tools — CRUD and execution for Skyvern workflows.

Tools for listing, creating, updating, deleting, running, and monitoring
Skyvern workflows via the Skyvern HTTP API. These tools do not require a
browser session.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

import structlog
import yaml
from pydantic import Field

from skyvern.client.errors import BadRequestError, NotFoundError
from skyvern.client.types import WorkflowCreateYamlRequest
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest as WorkflowCreateYAMLRequestSchema
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern
from ._validation import validate_folder_id, validate_run_id, validate_workflow_id

LOG = structlog.get_logger()
_SUMMARY_TOP_LEVEL_KEY_LIMIT = 8
_SUMMARY_SCALAR_PREVIEW_LIMIT = 3
_SUMMARY_ARTIFACT_PREVIEW_LIMIT = 4
_SUMMARY_STRING_PREVIEW_LIMIT = 120
_SUMMARY_RECURSION_LIMIT = 10
_SCREENSHOT_LIST_KEYS = frozenset({"task_screenshots", "workflow_screenshots", "screenshot_urls"})
_SCREENSHOT_ARTIFACT_ID_KEYS = frozenset({"task_screenshot_artifact_ids", "workflow_screenshot_artifact_ids"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_workflow(wf: Any) -> dict[str, Any]:
    """Pick the fields we expose from a Workflow Pydantic model.

    Uses Any to avoid tight coupling with Fern-generated client types.
    """
    data: dict[str, Any] = {
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
    for caching_field in ("run_with", "code_version", "adaptive_caching"):
        val = getattr(wf, caching_field, None)
        if val is not None:
            data[caching_field] = val
    return data


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
        "ai_fallback",
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

    script_run = getattr(run, "script_run", None)
    if script_run is not None:
        data["script_run"] = script_run.model_dump(mode="json") if hasattr(script_run, "model_dump") else script_run

    return data


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_run_id(run: Any) -> str | None:
    return _get_value(run, "run_id") or _get_value(run, "workflow_run_id")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _truncate_preview(value: Any) -> Any:
    value = _jsonable(value)
    if isinstance(value, str) and len(value) > _SUMMARY_STRING_PREVIEW_LIMIT:
        return f"{value[: _SUMMARY_STRING_PREVIEW_LIMIT - 3]}..."
    return value


def _is_scalarish(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _init_output_stats() -> dict[str, Any]:
    return {
        "has_extracted_information": False,
        "nested_screenshot_count": 0,
        "artifact_id_count": 0,
        "artifact_ids_preview": [],
    }


def _note_artifact_ids(stats: dict[str, Any], values: list[Any]) -> None:
    stats["artifact_id_count"] += len(values)
    preview = stats["artifact_ids_preview"]
    for value in values:
        if len(preview) >= _SUMMARY_ARTIFACT_PREVIEW_LIMIT:
            break
        value_str = str(value)
        if value_str not in preview:
            preview.append(value_str)


def _scan_output_value(value: Any, stats: dict[str, Any], depth: int = 0) -> None:
    if depth > _SUMMARY_RECURSION_LIMIT:
        return

    value = _jsonable(value)

    if isinstance(value, dict):
        for key, nested_value in value.items():
            if key == "extracted_information" and nested_value is not None:
                stats["has_extracted_information"] = True
            if key in _SCREENSHOT_LIST_KEYS and isinstance(nested_value, list):
                stats["nested_screenshot_count"] += len(nested_value)
            if key in _SCREENSHOT_ARTIFACT_ID_KEYS and isinstance(nested_value, list):
                _note_artifact_ids(stats, nested_value)
            _scan_output_value(nested_value, stats, depth + 1)
        return

    if isinstance(value, list):
        for item in value:
            _scan_output_value(item, stats, depth + 1)


def _summarize_output_value(output_value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if output_value is None:
        return {"present": False}, _init_output_stats()

    output_value = _jsonable(output_value)
    stats = _init_output_stats()
    _scan_output_value(output_value, stats)

    summary: dict[str, Any] = {"present": True}

    if isinstance(output_value, dict):
        top_level_keys = list(output_value.keys())
        summary["top_level_keys"] = top_level_keys[:_SUMMARY_TOP_LEVEL_KEY_LIMIT]
        if len(top_level_keys) > _SUMMARY_TOP_LEVEL_KEY_LIMIT:
            summary["top_level_key_count"] = len(top_level_keys)
        summary["block_output_count"] = len([key for key in top_level_keys if key != "extracted_information"])

        scalar_preview: dict[str, Any] = {}
        for key, value in output_value.items():
            if key == "extracted_information":
                continue
            if _is_scalarish(value):
                scalar_preview[key] = _truncate_preview(value)
            elif (
                isinstance(value, list)
                and value
                and len(value) <= _SUMMARY_SCALAR_PREVIEW_LIMIT
                and all(_is_scalarish(item) for item in value)
            ):
                scalar_preview[key] = [_truncate_preview(item) for item in value]
            if len(scalar_preview) >= _SUMMARY_SCALAR_PREVIEW_LIMIT:
                break
        if scalar_preview:
            summary["scalar_preview"] = scalar_preview
    elif isinstance(output_value, list):
        summary["item_count"] = len(output_value)
        if (
            output_value
            and len(output_value) <= _SUMMARY_SCALAR_PREVIEW_LIMIT
            and all(_is_scalarish(item) for item in output_value)
        ):
            summary["scalar_preview"] = [_truncate_preview(item) for item in output_value]
    else:
        summary["scalar_preview"] = _truncate_preview(output_value)

    summary["has_extracted_information"] = stats["has_extracted_information"]
    summary["nested_screenshot_count"] = stats["nested_screenshot_count"]
    summary["artifact_id_count"] = stats["artifact_id_count"]

    return summary, stats


def _summarize_artifacts(run: Any, output_stats: dict[str, Any]) -> dict[str, Any]:
    downloaded_files = _jsonable(_get_value(run, "downloaded_files")) or []
    screenshot_urls = _jsonable(_get_value(run, "screenshot_urls")) or []

    summary: dict[str, Any] = {
        "recording_available": bool(_get_value(run, "recording_url")),
        "workflow_screenshot_count": len(screenshot_urls),
        "downloaded_file_count": len(downloaded_files),
        "artifact_id_count": output_stats["artifact_id_count"],
    }

    filenames = [
        filename
        for filename in (_get_value(file_info, "filename") for file_info in downloaded_files)
        if isinstance(filename, str) and filename
    ]
    if filenames:
        summary["downloaded_file_names"] = filenames[:_SUMMARY_SCALAR_PREVIEW_LIMIT]

    if output_stats["artifact_ids_preview"]:
        summary["artifact_ids_preview"] = output_stats["artifact_ids_preview"]

    return summary


def _serialize_run_summary(run: Any) -> dict[str, Any]:
    run_id = _get_run_id(run)
    run_type = _get_value(run, "run_type")
    if run_type is None and _get_value(run, "workflow_run_id"):
        run_type = "workflow_run"

    output_value = _get_value(run, "output")
    if output_value is None and _get_value(run, "outputs") is not None:
        output_value = _get_value(run, "outputs")

    output_summary, output_stats = _summarize_output_value(output_value)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": str(_get_value(run, "status")) if _get_value(run, "status") is not None else None,
        "run_type": str(run_type) if run_type is not None else None,
        "artifact_summary": _summarize_artifacts(run, output_stats),
        "output_summary": output_summary,
    }

    failure_reason = _get_value(run, "failure_reason")
    if failure_reason:
        summary["failure_reason"] = failure_reason

    run_with = _get_value(run, "run_with")
    if run_with:
        summary["run_with"] = run_with

    script_run = _get_value(run, "script_run")
    if script_run is not None:
        sr = _jsonable(script_run)
        if isinstance(sr, dict) and sr.get("ai_fallback_triggered") is not None:
            summary["ai_fallback_triggered"] = sr["ai_fallback_triggered"]

    workflow_title = _get_value(run, "workflow_title")
    if workflow_title:
        summary["workflow_title"] = workflow_title

    step_count = _get_value(run, "step_count")
    total_steps = _get_value(run, "total_steps")
    if step_count is not None:
        summary["step_count"] = step_count
    elif total_steps is not None:
        summary["total_steps"] = total_steps

    return {key: value for key, value in summary.items() if value is not None}


def _serialize_run_full(run: Any) -> dict[str, Any]:
    if not isinstance(run, dict):
        return _serialize_run(run)

    data: dict[str, Any] = {
        "run_id": _get_run_id(run),
        "status": str(_get_value(run, "status")) if _get_value(run, "status") is not None else None,
        "run_type": "workflow_run" if _get_value(run, "workflow_run_id") else _get_value(run, "run_type"),
    }

    for field in (
        "workflow_id",
        "workflow_title",
        "failure_reason",
        "recording_url",
        "screenshot_urls",
        "downloaded_files",
        "downloaded_file_urls",
        "parameters",
        "errors",
        "browser_session_id",
        "browser_profile_id",
        "run_with",
        "total_steps",
        "script_run",
        "ai_fallback",
    ):
        value = _get_value(run, field)
        if value is not None:
            data[field] = _jsonable(value)

    outputs = _get_value(run, "outputs")
    if outputs is not None:
        data["output"] = _jsonable(outputs)

    for ts_field in ("created_at", "modified_at", "started_at", "finished_at", "queued_at"):
        value = _get_value(run, ts_field)
        if value is not None:
            data[ts_field] = _jsonable(value)

    return {key: value for key, value in data.items() if value is not None}


async def _get_workflow_run_status(
    workflow_run_id: str,
    *,
    include_output_details: bool,
) -> dict[str, Any]:
    skyvern = get_skyvern()
    # The generated SDK only exposes get_run() for /v1/runs/{run_id}; wr_... IDs
    # require the workflow-run detail route until a public SDK helper exists.
    response = await skyvern._client_wrapper.httpx_client.request(
        f"api/v1/workflows/runs/{workflow_run_id}",
        method="GET",
        params={"include_output_details": include_output_details},
    )
    if response.status_code == 404:
        raise NotFoundError(body={"detail": f"Workflow run {workflow_run_id!r} not found"})
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    return response.json()


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


_CODE_V2_DEFAULTS: dict[str, Any] = {
    "code_version": 2,
    "run_with": "code",
}
_DEFAULT_MCP_PROXY_LOCATION = ProxyLocation.RESIDENTIAL


def _deep_merge(base: Any, override: Any) -> Any:
    """Recursively merge normalized JSON-like data over the raw payload.

    Unknown fields should survive normalization. Lists are merged by index so
    overlapping items keep raw unknown keys even if normalization changes the
    list length.
    """

    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            if key in result:
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    if isinstance(base, list) and isinstance(override, list):
        merged: list[Any] = []
        for idx in range(max(len(base), len(override))):
            if idx < len(base) and idx < len(override):
                merged.append(_deep_merge(base[idx], override[idx]))
            elif idx < len(override):
                merged.append(override[idx])
            else:
                merged.append(base[idx])
        return merged

    return override


def _normalize_json_definition(raw: Any) -> WorkflowCreateYamlRequest:
    """Normalize JSON workflow definitions through the shared backend schema."""

    if not isinstance(raw, dict):
        raise TypeError("Workflow definition JSON must be an object")

    try:
        normalized = WorkflowCreateYAMLRequestSchema.model_validate(raw)
    except Exception as exc:
        # Internal schema is stricter than the Fern SDK — skip normalization so
        # unknown/future fields are not rejected.
        LOG.warning("Skipping text-prompt normalization; internal schema rejected payload", error=str(exc))
        return WorkflowCreateYamlRequest(**raw)

    merged = _deep_merge(raw, normalized.model_dump(mode="json"))
    return WorkflowCreateYamlRequest(**merged)


def _make_invalid_json_definition_error(exc: Exception) -> dict[str, Any]:
    return make_error(
        ErrorCode.INVALID_INPUT,
        f"Invalid JSON definition: {exc}",
        "Provide a valid JSON object for the workflow definition",
    )


def _load_definition_dict(definition: str, fmt: str) -> tuple[dict[str, Any] | None, str | None]:
    """Best-effort parse of a workflow definition into a mutable dict.

    Used only for tool-side default injection. On parse failure, returns
    ``(None, None)`` so the caller can preserve existing server-side validation
    behavior.
    """

    def _as_dict(value: Any, parsed_format: str) -> tuple[dict[str, Any] | None, str | None]:
        return (value, parsed_format) if isinstance(value, dict) else (None, None)

    if fmt == "json":
        try:
            return _as_dict(json.loads(definition), "json")
        except (json.JSONDecodeError, TypeError):
            return None, None

    if fmt == "yaml":
        try:
            return _as_dict(safe_load_no_dates(definition), "yaml")
        except yaml.YAMLError:
            return None, None

    try:
        return _as_dict(json.loads(definition), "json")
    except (json.JSONDecodeError, TypeError):
        try:
            return _as_dict(safe_load_no_dates(definition), "yaml")
        except yaml.YAMLError:
            return None, None


def _dump_definition_dict(raw: dict[str, Any], parsed_format: str) -> str:
    def _coerce_enums(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {key: _coerce_enums(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_coerce_enums(item) for item in value]
        return value

    raw = _coerce_enums(raw)
    if parsed_format == "json":
        return json.dumps(raw)
    return yaml.safe_dump(raw, sort_keys=False)


def _inject_missing_top_level_defaults(definition: str, fmt: str, defaults: dict[str, Any]) -> str:
    """Inject missing top-level keys for JSON or YAML workflow definitions."""

    raw, parsed_format = _load_definition_dict(definition, fmt)
    if raw is None or parsed_format is None:
        return definition

    changed = False
    for key, value in defaults.items():
        if key not in raw:
            raw[key] = value
            changed = True

    return _dump_definition_dict(raw, parsed_format) if changed else definition


def _inject_code_v2_defaults(definition: str, fmt: str) -> str:
    """Inject Code 2.0 defaults (code_version=2, run_with=code) when not explicitly set.

    Only modifies JSON definitions (or auto-detected JSON). YAML is returned unchanged.
    """
    if fmt == "yaml":
        return definition

    try:
        raw = json.loads(definition)
    except (json.JSONDecodeError, TypeError):
        return definition  # let _parse_definition handle the error

    changed = False
    for key, value in _CODE_V2_DEFAULTS.items():
        if key not in raw:
            raw[key] = value
            changed = True

    return json.dumps(raw) if changed else definition


async def _inject_workflow_update_proxy_default(definition: str, fmt: str, workflow_id: str) -> str:
    """Preserve or default workflow proxy location when MCP update omits it."""

    raw, parsed_format = _load_definition_dict(definition, fmt)
    if raw is None or parsed_format is None or "proxy_location" in raw:
        return definition

    existing_workflow = await _get_workflow_by_id(workflow_id)
    raw["proxy_location"] = existing_workflow.get("proxy_location") or _DEFAULT_MCP_PROXY_LOCATION
    return _dump_definition_dict(raw, parsed_format)


# Parameter types that are auto-managed (credentials and secrets set via the UI) and should
# always be preserved from the existing workflow during MCP updates, regardless of what the
# caller sends. These should NEVER be modifiable via MCP — only via the UI credential picker.
# Derived from the enum to stay in sync when new secret types are added.
_AUTO_MANAGED_PARAMETER_TYPES = frozenset(pt.value for pt in ParameterType if pt.is_secret_or_credential())
_PROTECTED_WORKFLOW_PARAMETER_TYPES = frozenset(pt.value for pt in WorkflowParameterType if pt.is_credential_type())
# Login-capable credential types — subset of protected params that carry username/password data.
# Derived from the enum to stay in sync when new login-capable types are added.
_LOGIN_CREDENTIAL_PARAMETER_TYPES = frozenset(pt.value for pt in ParameterType if pt.is_login_credential())

# Runtime-only fields returned by GET /api/v1/workflows/{id} that must be stripped before
# re-injecting parameters into a YAML/JSON definition.  Uses a suffix-based deny-list so
# new parameter types with the standard *_parameter_id / workflow_id / timestamp pattern
# are handled automatically.
_RUNTIME_FIELD_SUFFIXES = ("_parameter_id", "_at")
# workflow_id is the only runtime field not caught by the suffix rules above.
_RUNTIME_EXACT_FIELDS = frozenset({"workflow_id"})


def _strip_runtime_fields(param: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *param* with runtime-only fields removed."""
    return {
        k: v
        for k, v in param.items()
        if k not in _RUNTIME_EXACT_FIELDS and not any(k.endswith(s) for s in _RUNTIME_FIELD_SUFFIXES)
    }


def _is_protected_update_parameter(param: Any) -> bool:
    """Return True when *param* should be preserved from the existing workflow.

    This includes:
    - Secret/credential parameter types managed directly by the UI.
    - Workflow input parameters whose type is `credential_id`, where the
      selected credential lives in `default_value`.
    """
    if not isinstance(param, dict):
        return False

    key = param.get("key")
    parameter_type = param.get("parameter_type")
    if not key or not parameter_type:
        return False
    if parameter_type in _AUTO_MANAGED_PARAMETER_TYPES:
        return True
    return (
        parameter_type == ParameterType.WORKFLOW.value
        and param.get("workflow_parameter_type") in _PROTECTED_WORKFLOW_PARAMETER_TYPES
    )


def _is_login_credential_reference(param: dict[str, Any]) -> bool:
    """Return True when *param* represents a credential reference usable by login blocks."""

    parameter_type = param.get("parameter_type")
    if not parameter_type:
        return False
    if parameter_type in _LOGIN_CREDENTIAL_PARAMETER_TYPES:
        return True
    return (
        parameter_type == ParameterType.WORKFLOW.value
        and param.get("workflow_parameter_type") in _PROTECTED_WORKFLOW_PARAMETER_TYPES
    )


def _iter_blocks_flat(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return all block dicts from a block list, recursing into for_loop nested blocks."""
    result: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        result.append(block)
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            result.extend(_iter_blocks_flat(loop_blocks))
    return result


async def _inject_workflow_update_parameters(definition: str, fmt: str, workflow_id: str) -> str:
    """Preserve protected credential/secret parameters during MCP workflow updates.

    Credential references should NEVER be modifiable via MCP — the existing workflow's
    values always win. This function:
      1. Always replaces protected parameters with the existing workflow's versions
         (even if the caller includes them — they may have stale/wrong data).
      2. Injects credential parameter_keys into blocks using type-based matching
         (login blocks always get ALL credential keys) with label-based fallback
         for non-login blocks.
    """

    raw, parsed_format = _load_definition_dict(definition, fmt)
    if raw is None or parsed_format is None:
        return definition

    wf_def = raw.get("workflow_definition")
    if not isinstance(wf_def, dict):
        return definition

    update_params: list[dict[str, Any]] = wf_def.get("parameters", [])

    existing_workflow = await _get_workflow_by_id(workflow_id)
    existing_wf_def = existing_workflow.get("workflow_definition")
    if not isinstance(existing_wf_def, dict):
        return definition

    existing_params: list[dict[str, Any]] = existing_wf_def.get("parameters", [])

    modified = False

    # --- Step 1: Always replace protected parameters with existing values ---
    # Credential/secret parameters — including workflow inputs of type credential_id —
    # should NEVER be modifiable via MCP. The existing workflow's values always win,
    # even if the caller includes them with different data. This means callers cannot
    # swap credential references or remove credential params via MCP; those operations
    # must go through the UI credential picker.
    protected_keys: set[str] = set()
    for param in existing_params:
        if _is_protected_update_parameter(param):
            protected_keys.add(param["key"])

    if protected_keys:
        # Remove any protected params the caller may have included (may have stale data)
        update_params = [p for p in update_params if not (isinstance(p, dict) and p.get("key") in protected_keys)]
        # Inject all protected params from the existing workflow, stripping runtime-only
        # fields that come from the GET API response (e.g. *_parameter_id, workflow_id,
        # created_at, modified_at, deleted_at) to keep the definition YAML-clean.
        for param in existing_params:
            if _is_protected_update_parameter(param):
                update_params.append(_strip_runtime_fields(param))
        modified = True
        wf_def["parameters"] = update_params

    # --- Step 2: Inject credential parameter keys into blocks ---
    # Login blocks get credential-type keys via type-based matching (resilient to label
    # renames by Claude). Non-login blocks fall back to label-based matching — so if Claude
    # renames a non-login block that references aws_secret/bitwarden/etc., the key reference
    # is lost. This asymmetry is accepted because login blocks are the critical path for
    # credential injection; non-login secret refs are rare and still work when labels match.
    all_cred_keys: set[str] = set()
    login_cred_keys: set[str] = set()
    for param in existing_params:
        if _is_protected_update_parameter(param):
            all_cred_keys.add(param["key"])
            if _is_login_credential_reference(param):
                login_cred_keys.add(param["key"])

    if all_cred_keys:
        existing_blocks: list[dict[str, Any]] = existing_wf_def.get("blocks", [])
        update_blocks: list[dict[str, Any]] = wf_def.get("blocks", [])

        # Build label-based map for fallback (non-login blocks)
        existing_block_cred_keys: dict[str, list[str]] = {}
        for block in _iter_blocks_flat(existing_blocks):
            label = block.get("label")
            if not label:
                continue
            existing_pkeys = block.get("parameter_keys") or []
            cred_keys = [k for k in existing_pkeys if k in all_cred_keys]
            if cred_keys:
                existing_block_cred_keys[label] = cred_keys

        for block in _iter_blocks_flat(update_blocks):
            block_type = block.get("block_type")
            label = block.get("label")

            keys_to_inject: list[str] = []
            if block_type == "login":
                keys_to_inject = sorted(login_cred_keys)
            elif label and label in existing_block_cred_keys:
                keys_to_inject = sorted(existing_block_cred_keys[label])

            if keys_to_inject:
                block_pkeys: list[str] = list(block.get("parameter_keys") or [])
                current_keys = set(block_pkeys)
                for cred_key in keys_to_inject:
                    if cred_key not in current_keys:
                        block_pkeys.append(cred_key)
                        modified = True
                block["parameter_keys"] = block_pkeys

    if not modified:
        return definition

    return _dump_definition_dict(raw, parsed_format)


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
        except (json.JSONDecodeError, TypeError) as e:
            return None, None, _make_invalid_json_definition_error(e)
        try:
            return _normalize_json_definition(raw), None, None
        except Exception as e:
            return None, None, _make_invalid_json_definition_error(e)
    elif fmt == "yaml":
        return None, definition, None
    else:
        # auto: try JSON first, fall back to YAML
        try:
            raw = json.loads(definition)
        except (json.JSONDecodeError, TypeError):
            return None, definition, None
        try:
            return _normalize_json_definition(raw), None, None
        except Exception as e:
            return None, None, _make_invalid_json_definition_error(e)


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
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_get"):
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

    By default, workflows created via MCP use Code 2.0 (code_version=2, run_with="code").
    To disable this, explicitly set "code_version": 1 and/or "run_with": null in your definition.

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

    # Default MCP-created workflows to the same editor defaults while preserving
    # any explicit user-supplied values.
    definition = _inject_code_v2_defaults(definition, format)
    definition = _inject_missing_top_level_defaults(
        definition,
        format,
        {"proxy_location": _DEFAULT_MCP_PROXY_LOCATION},
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
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_update"):
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

    try:
        definition = await _inject_workflow_update_proxy_default(definition, format, workflow_id)
        definition = await _inject_workflow_update_parameters(definition, format, workflow_id)
    except NotFoundError:
        return make_result(
            "skyvern_workflow_update",
            ok=False,
            error=make_error(
                ErrorCode.WORKFLOW_NOT_FOUND,
                f"Workflow {workflow_id!r} not found",
                "Verify the workflow ID with skyvern_workflow_list",
            ),
        )
    except Exception as e:
        LOG.warning("workflow_update_proxy_default_injection_failed", workflow_id=workflow_id, error=str(e))
        return make_result(
            "skyvern_workflow_update",
            ok=False,
            error=make_error(
                ErrorCode.API_ERROR,
                str(e),
                "Check the workflow ID and Skyvern connection before retrying the update",
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
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_delete"):
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


async def skyvern_workflow_update_folder(
    workflow_id: Annotated[str, "Workflow permanent ID (wpid_...)"],
    folder_id: Annotated[
        str | None,
        "Folder ID (fld_...) to assign, or null to remove the workflow from its folder",
    ] = None,
) -> dict[str, Any]:
    """Assign a workflow to a folder, or remove it from its current folder."""
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_update_folder"):
        return err
    if folder_id is not None and (err := validate_folder_id(folder_id, "skyvern_workflow_update_folder")):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            workflow = await skyvern.update_workflow_folder(workflow_id, folder_id=folder_id)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_update_folder",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.WORKFLOW_NOT_FOUND,
                    f"Workflow {workflow_id!r} not found",
                    "Verify the workflow ID with skyvern_workflow_list.",
                ),
            )
        except BadRequestError as e:
            return make_result(
                "skyvern_workflow_update_folder",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    str(e),
                    "Verify the folder ID with skyvern_folder_list or pass null to remove the folder assignment.",
                ),
            )
        except Exception as e:
            return make_result(
                "skyvern_workflow_update_folder",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.API_ERROR, str(e), "Check the workflow ID, folder ID, and your permissions."
                ),
            )

    data = _serialize_workflow(workflow)
    data["sdk_equivalent"] = f"await skyvern.update_workflow_folder({workflow_id!r}, folder_id={folder_id!r})"
    return make_result("skyvern_workflow_update_folder", data=data, timing_ms=timer.timing_ms)


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
    run_with: Annotated[
        str | None,
        Field(
            description="Execution mode override (e.g., 'code' for cached script execution). Null inherits from workflow setting."
        ),
    ] = None,
) -> dict[str, Any]:
    """Run a Skyvern workflow with parameters. Use when you need to execute an automation workflow.
    Returns immediately by default (async) — set wait=true to block until completion.
    Default timeout is 300s (5 minutes). For longer workflows, increase timeout_seconds
    or use wait=false and poll with skyvern_workflow_status."""
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_run"):
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
                run_with=run_with,
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
    verbosity: Annotated[
        Literal["summary", "full"],
        Field(description="`summary` returns a compact status payload. `full` includes outputs, timestamps, and URLs."),
    ] = "summary",
) -> dict[str, Any]:
    """Check the status and progress of a workflow or task run. Use when you need to monitor
    a running workflow, check if it completed, or retrieve its output."""
    if err := validate_run_id(run_id, "skyvern_workflow_status"):
        return err
    if verbosity not in {"summary", "full"}:
        return make_result(
            "skyvern_workflow_status",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid verbosity: {verbosity!r}",
                "Use verbosity='summary' for compact status or verbosity='full' for full detail.",
            ),
        )

    with Timer() as timer:
        try:
            if run_id.startswith("wr_"):
                run = await _get_workflow_run_status(
                    run_id,
                    include_output_details=verbosity == "full",
                )
            else:
                skyvern = get_skyvern()
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

    data = _serialize_run_full(run) if verbosity == "full" else _serialize_run_summary(run)
    if run_id.startswith("wr_"):
        data["sdk_equivalent"] = f"await skyvern_workflow_status(run_id={run_id!r}, verbosity={verbosity!r})"
    else:
        verbosity_arg = "" if verbosity == "summary" else f", verbosity={verbosity!r}"
        data["sdk_equivalent"] = (
            f"await skyvern.get_run({run_id!r})  # or skyvern_workflow_status(run_id={run_id!r}{verbosity_arg})"
        )
    return make_result("skyvern_workflow_status", data=data, timing_ms=timer.timing_ms)


async def skyvern_workflow_cancel(
    run_id: Annotated[str, "Run ID to cancel (wr_... for workflow runs, tsk_v2_... for task runs)"],
) -> dict[str, Any]:
    """Cancel a running workflow or task. Use when you need to stop a workflow that is taking
    too long, is stuck, or is no longer needed."""
    if err := validate_run_id(run_id, "skyvern_workflow_cancel"):
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
