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
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest as WorkflowCreateYAMLRequestSchema
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern
from ._validation import validate_folder_id, validate_run_id, validate_workflow_id, validate_workflow_run_id
from ._workflow_http import (
    coerce_timestamp,
    create_workflow_raw,
    get_workflow_by_id,
    get_workflow_run_status,
    list_workflow_runs_raw,
    list_workflows_raw,
    retry_workflow_run_raw,
    update_workflow_folder_raw,
    update_workflow_raw,
)

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
    """Pick the fields we expose from a Workflow.

    Accepts both a Fern-generated Workflow pydantic model and a plain dict
    parsed from a raw httpx JSON response. Uses Any to stay decoupled from
    Fern-generated client types.
    """
    status = _get_value(wf, "status")
    data: dict[str, Any] = {
        "workflow_permanent_id": _get_value(wf, "workflow_permanent_id"),
        "workflow_id": _get_value(wf, "workflow_id"),
        "title": _get_value(wf, "title"),
        "version": _get_value(wf, "version"),
        "status": str(status) if status else None,
        "description": _get_value(wf, "description"),
        "is_saved_task": _get_value(wf, "is_saved_task"),
        "folder_id": _get_value(wf, "folder_id"),
        "created_at": coerce_timestamp(_get_value(wf, "created_at")),
        "modified_at": coerce_timestamp(_get_value(wf, "modified_at")),
    }
    for caching_field in ("run_with", "code_version", "adaptive_caching"):
        val = _get_value(wf, caching_field)
        if val is not None:
            data[caching_field] = val
    return data


def _serialize_workflow_full(wf: Any) -> dict[str, Any]:
    """Like _serialize_workflow but includes the full definition."""
    data = _serialize_workflow(wf)
    wf_def = _get_value(wf, "workflow_definition")
    if wf_def is None:
        return data
    if hasattr(wf_def, "model_dump"):
        try:
            data["workflow_definition"] = wf_def.model_dump(mode="json")
        except Exception:
            data["workflow_definition"] = str(wf_def)
    elif isinstance(wf_def, dict):
        data["workflow_definition"] = wf_def
    else:
        data["workflow_definition"] = str(wf_def)
    return data


def _serialize_run(run: Any) -> dict[str, Any]:
    """Pick fields from a run response (GetRunResponse variant or WorkflowRunResponse).

    Run responses still come from Fern SDK models; unlike workflow CRUD, this
    path is not part of the raw dict bypass.
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
    """Read a field from raw workflow dicts or Fern models without enforcing requiredness.

    Workflow serializers are intentionally permissive here so response shaping
    does not fail when the backend adds or omits optional fields.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _api_error_body_message(body: Any) -> str:
    detail = _get_value(body, "detail")
    if isinstance(detail, str):
        return detail
    if detail is not None:
        return json.dumps(_jsonable(detail))
    if isinstance(body, str):
        return body
    if body is None:
        return "Bad request"
    try:
        return json.dumps(_jsonable(body))
    except TypeError:
        return str(body)


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


def _validate_definition_structure(json_def: dict[str, Any] | None, action: str) -> dict[str, Any] | None:
    """Validate required fields in a JSON workflow definition.

    Returns a make_result error dict if validation fails, or None if valid.
    Only validates JSON definitions — YAML is validated server-side.
    """
    if json_def is None:
        return None
    if not json_def.get("title"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Workflow definition missing 'title' field",
                "Add a 'title' field to your workflow definition",
            ),
        )
    if not isinstance(json_def.get("workflow_definition"), dict):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Workflow definition missing 'workflow_definition' object",
                "Add a 'workflow_definition' object with a 'blocks' list",
            ),
        )
    return None


def _extract_workflow_blocks(definition: dict[str, Any]) -> list[Any]:
    block_lists: list[list[Any]] = []
    top_level_blocks = definition.get("blocks")
    if isinstance(top_level_blocks, list):
        block_lists.append(top_level_blocks)

    workflow_definition = definition.get("workflow_definition")
    if isinstance(workflow_definition, dict):
        workflow_blocks = workflow_definition.get("blocks")
        if isinstance(workflow_blocks, list) and workflow_blocks is not top_level_blocks:
            block_lists.append(workflow_blocks)

    return [block for blocks in block_lists for block in blocks]


def _validate_code_only_workflow_blocks(definition: dict[str, Any], action: str) -> dict[str, Any] | None:
    blocks = _extract_workflow_blocks(definition)
    if not blocks:
        return None

    # Preserve the lightweight-install constraint; copilot helpers require server-only deps.
    from skyvern.forge.sdk.copilot.tools.banned_blocks import (  # noqa: PLC0415
        collect_code_only_banned_items,
    )

    banned_items = collect_code_only_banned_items(blocks)
    if not banned_items:
        return None

    labels = ", ".join(sorted({label for label, _ in banned_items}))
    types = ", ".join(sorted({block_type for _, block_type in banned_items}))
    return make_result(
        action,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            f"Block type(s) {types} are not allowed in code-only mode (offending labels: {labels})",
            "In code-only mode, use a `code` block for durable browser/page work instead of "
            "task/navigation/extraction/etc.",
        ),
    )


def _validate_code_only_definition(definition: str, fmt: str, action: str) -> dict[str, Any] | None:
    raw, _ = _load_definition_dict(definition, fmt)
    if raw is None:
        return None
    return _validate_code_only_workflow_blocks(raw, action)


_CODE_V2_DEFAULTS: dict[str, Any] = {
    "code_version": 2,
    "run_with": "agent",
}
_DEFAULT_MCP_PROXY_LOCATION = ProxyLocation.RESIDENTIAL
_WORKFLOW_UPDATE_PRESERVED_TOP_LEVEL_FIELDS = ("run_sequentially", "sequential_key")


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


def _normalize_json_definition(raw: Any) -> dict[str, Any]:
    """Normalize JSON workflow definitions through the shared backend schema.

    The MCP tools post through raw HTTP so valid backend payloads are not
    rejected by stale Fern request unions. When the backend schema accepts the
    payload, merge its JSON-compatible normalization over the raw dict; when it
    does not, preserve the caller's raw dict for server-side validation.
    """

    if not isinstance(raw, dict):
        raise TypeError("Workflow definition JSON must be an object")
    if "title" not in raw:
        raise ValueError("Workflow definition missing 'title' field")
    if "workflow_definition" not in raw:
        raise ValueError("Workflow definition missing 'workflow_definition' object")

    if _has_runtime_definition_shape(raw.get("workflow_definition")):
        # An echoed GET payload is in runtime shape; convert before validation so block<->parameter
        # links survive instead of being lost to the raw-dict fallback below.
        raw = {**raw, "workflow_definition": _workflow_definition_to_authoring_shape(raw["workflow_definition"])}

    try:
        normalized = WorkflowCreateYAMLRequestSchema.model_validate(raw)
    except Exception as exc:
        # Internal schema is stricter than the API boundary — skip normalization
        # so unknown/future fields are not rejected by MCP before the backend can
        # decide.
        LOG.warning("Skipping text-prompt normalization; internal schema rejected payload", error=str(exc))
        return raw

    merged = _deep_merge(raw, normalized.model_dump(mode="json"))
    return merged


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
    """Inject Code 2.0 defaults (code_version=2, run_with=agent) when not explicitly set.

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

    existing_workflow = await get_workflow_by_id(workflow_id)
    raw["proxy_location"] = existing_workflow.get("proxy_location") or _DEFAULT_MCP_PROXY_LOCATION
    return _dump_definition_dict(raw, parsed_format)


async def _inject_workflow_update_top_level_settings(definition: str, fmt: str, workflow_id: str) -> str:
    """Preserve workflow-level settings that schema defaults would otherwise clobber."""

    raw, parsed_format = _load_definition_dict(definition, fmt)
    if raw is None or parsed_format is None:
        return definition

    missing_fields = [field for field in _WORKFLOW_UPDATE_PRESERVED_TOP_LEVEL_FIELDS if field not in raw]
    if not missing_fields:
        return definition

    existing_workflow = await get_workflow_by_id(workflow_id)
    changed = False
    for field in missing_fields:
        existing_value = existing_workflow.get(field)
        if existing_value is not None:
            raw[field] = existing_value
            changed = True

    return _dump_definition_dict(raw, parsed_format) if changed else definition


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


def _parameter_to_authoring_shape(param: Any) -> dict[str, Any] | None:
    """Convert one runtime parameter dict to its authoring shape, or None to drop it.

    Auto-generated output parameters are regenerated (and rejected if author-supplied) by the
    converter, so they are dropped. Context parameters carry a resolved ``source`` object at
    runtime but the authoring schema wants ``source_parameter_key``.
    """
    if not isinstance(param, dict):
        return param
    if param.get("parameter_type") == ParameterType.OUTPUT.value:
        return None
    authoring = _strip_runtime_fields(param)
    source = authoring.get("source")
    if isinstance(source, dict) and source.get("key"):
        authoring.pop("source", None)
        authoring.setdefault("source_parameter_key", source["key"])
    return authoring


def _block_to_authoring_shape(block: Any) -> Any:
    """Convert one runtime block dict to its authoring shape, recursing into ``loop_blocks``.

    Runtime blocks expose resolved ``parameters`` objects plus an ``output_parameter`` and (for
    loops) a resolved ``loop_over`` object; the authoring schema uses ``parameter_keys``, carries no
    output parameter, and references the loop source via ``loop_over_parameter_key``. Without this
    inverse, a get -> edit -> update round trip silently drops non-credential block<->parameter links.
    """
    if not isinstance(block, dict):
        return block
    authoring = dict(block)
    label = authoring.get("label")
    output_key = f"{label}_output" if isinstance(label, str) else None

    resolved = authoring.pop("parameters", None)
    authoring.pop("output_parameter", None)
    if isinstance(resolved, list):
        derived = [
            param["key"]
            for param in resolved
            if isinstance(param, dict) and param.get("key") and param.get("key") != output_key
        ]
        if derived:
            existing = [k for k in (authoring.get("parameter_keys") or []) if isinstance(k, str)]
            authoring["parameter_keys"] = existing + [k for k in derived if k not in existing]

    loop_over = authoring.get("loop_over")
    if isinstance(loop_over, dict):
        authoring.pop("loop_over", None)
        if loop_over.get("key"):
            authoring.setdefault("loop_over_parameter_key", loop_over["key"])
    elif loop_over is None and "loop_over" in authoring:
        authoring.pop("loop_over", None)

    loop_blocks = authoring.get("loop_blocks")
    if isinstance(loop_blocks, list):
        authoring["loop_blocks"] = [_block_to_authoring_shape(child) for child in loop_blocks]

    return authoring


def _block_authoring_parameter_keys(block: dict[str, Any]) -> list[str]:
    """The block's parameter links as authoring keys, read from either an explicit ``parameter_keys``
    list (authoring shape) or the resolved ``parameters`` objects (runtime shape returned by GET),
    excluding the block's own auto ``{label}_output``."""
    label = block.get("label")
    output_key = f"{label}_output" if isinstance(label, str) else None
    keys: list[str] = [k for k in (block.get("parameter_keys") or []) if isinstance(k, str)]
    seen = set(keys)
    for param in block.get("parameters") or []:
        if isinstance(param, dict):
            key = param.get("key")
            if isinstance(key, str) and key != output_key and key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def _workflow_definition_to_authoring_shape(wf_def: Any) -> Any:
    """Convert a runtime ``workflow_definition`` dict (as returned by GET) to the authoring shape the
    create/update path consumes. Inverse of the relevant parts of ``convert_workflow_definition``.
    """
    if not isinstance(wf_def, dict):
        return wf_def
    authoring = dict(wf_def)
    params = authoring.get("parameters")
    if isinstance(params, list):
        converted = [shaped for shaped in (_parameter_to_authoring_shape(p) for p in params) if shaped is not None]
        authoring["parameters"] = converted
    blocks = authoring.get("blocks")
    if isinstance(blocks, list):
        authoring["blocks"] = [_block_to_authoring_shape(block) for block in blocks]
    return authoring


def _has_runtime_definition_shape(wf_def: Any) -> bool:
    """Whether a ``workflow_definition`` carries runtime-only fields, so it needs authoring conversion."""
    if not isinstance(wf_def, dict):
        return False
    params = wf_def.get("parameters")
    if isinstance(params, list):
        for param in params:
            if isinstance(param, dict) and (
                param.get("parameter_type") == ParameterType.OUTPUT.value or isinstance(param.get("source"), dict)
            ):
                return True

    def _block_has_runtime(block: Any) -> bool:
        if not isinstance(block, dict):
            return False
        if (
            isinstance(block.get("parameters"), list)
            or "output_parameter" in block
            or isinstance(block.get("loop_over"), dict)
        ):
            return True
        loop_blocks = block.get("loop_blocks")
        return isinstance(loop_blocks, list) and any(_block_has_runtime(child) for child in loop_blocks)

    blocks = wf_def.get("blocks")
    return isinstance(blocks, list) and any(_block_has_runtime(block) for block in blocks)


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


def _iter_positional_block_matches(
    existing_blocks: list[dict[str, Any]],
    update_blocks: list[dict[str, Any]],
    *,
    parent_has_identity: bool = True,
) -> list[tuple[str | None, bool]]:
    """Return positional block types plus whether position identifies the block.

    A position is considered identifying only when the sibling shape is unchanged
    and the block either kept its label or is the sole child under an identified
    parent. This lets us repair dropped block_type values for singleton edits
    without using bare index to swap credentials across reordered siblings.
    """
    result: list[tuple[str | None, bool]] = []
    existing_dict_blocks = [block for block in existing_blocks if isinstance(block, dict)]
    update_dict_blocks = [block for block in update_blocks if isinstance(block, dict)]
    same_sibling_count = len(existing_dict_blocks) == len(update_dict_blocks)

    for index, update_block in enumerate(update_dict_blocks):
        existing_block = existing_dict_blocks[index] if same_sibling_count else None
        raw_existing_block_type = existing_block.get("block_type") if existing_block else None
        existing_block_type = raw_existing_block_type if isinstance(raw_existing_block_type, str) else None
        existing_label = existing_block.get("label") if existing_block else None
        update_label = update_block.get("label")
        stable_label_match = bool(existing_label and update_label and existing_label == update_label)
        singleton_position = same_sibling_count and len(update_dict_blocks) == 1
        has_identity = parent_has_identity and same_sibling_count and (stable_label_match or singleton_position)
        result.append((existing_block_type, has_identity))

        update_loop_blocks = update_block.get("loop_blocks")
        if not isinstance(update_loop_blocks, list):
            continue

        existing_loop_blocks = existing_block.get("loop_blocks") if existing_block else None
        if not isinstance(existing_loop_blocks, list):
            existing_loop_blocks = []
        result.extend(
            _iter_positional_block_matches(
                existing_loop_blocks,
                update_loop_blocks,
                parent_has_identity=has_identity,
            )
        )

    return result


async def _inject_workflow_update_parameters(definition: str, fmt: str, workflow_id: str) -> tuple[str, list[str]]:
    """Preserve protected credential/secret parameters during MCP workflow updates.

    Credential references should NEVER be modifiable via MCP — the existing workflow's
    values always win. This function:
      1. Always replaces protected parameters with the existing workflow's versions
         (even if the caller includes them — they may have stale/wrong data).
      2. Injects credential parameter_keys into blocks using per-block matches
         with label-based fallback.
      3. Re-attaches non-credential block<->parameter links the caller dropped for a
         still-declared parameter (best-effort, surfaced as warnings).

    Returns the (possibly rewritten) definition string and any human-facing warnings.
    """

    warnings: list[str] = []

    raw, parsed_format = _load_definition_dict(definition, fmt)
    if raw is None or parsed_format is None:
        return definition, warnings

    wf_def = raw.get("workflow_definition")
    if not isinstance(wf_def, dict):
        return definition, warnings

    update_params: list[dict[str, Any]] = wf_def.get("parameters", [])

    existing_workflow = await get_workflow_by_id(workflow_id)
    existing_wf_def = existing_workflow.get("workflow_definition")
    if not isinstance(existing_wf_def, dict):
        return definition, warnings

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
        existing_blocks_flat = _iter_blocks_flat(existing_blocks)
        update_blocks_flat = _iter_blocks_flat(update_blocks)
        n_existing_blocks = len(existing_blocks_flat)
        n_update_blocks = len(update_blocks_flat)
        positional_block_matches = _iter_positional_block_matches(existing_blocks, update_blocks)

        existing_block_type_by_label: dict[str, str] = {}
        # Login labels resolve through existing_block_login_cred_keys; this map
        # keeps all protected keys for non-login label recovery.
        existing_block_cred_keys: dict[str, list[str]] = {}
        existing_block_login_cred_keys: dict[str, list[str]] = {}
        existing_login_block_login_keys: list[list[str]] = []
        existing_login_block_count = 0
        for block in existing_blocks_flat:
            raw_existing_block_type = block.get("block_type")
            existing_block_type = raw_existing_block_type if isinstance(raw_existing_block_type, str) else None
            existing_pkeys = block.get("parameter_keys") or []
            cred_keys = sorted(k for k in existing_pkeys if k in all_cred_keys)
            login_keys = sorted(k for k in existing_pkeys if k in login_cred_keys)
            if existing_block_type == "login":
                existing_login_block_count += 1
                if login_keys:
                    existing_login_block_login_keys.append(login_keys)

            label = block.get("label")
            if not label:
                continue
            if existing_block_type:
                existing_block_type_by_label[label] = existing_block_type
            if cred_keys:
                existing_block_cred_keys[label] = cred_keys
            if login_keys:
                existing_block_login_cred_keys[label] = login_keys

        # Label matches identify block ownership. Positional reuse can only restore
        # a dropped login type when the login credential choice is unambiguous.
        use_positional_correspondence = n_existing_blocks == n_update_blocks
        single_global_login_cred_keys = sorted(login_cred_keys) if len(login_cred_keys) == 1 else []
        single_existing_login_block_keys = (
            existing_login_block_login_keys[0]
            if len(existing_login_block_login_keys) == 1 and existing_login_block_count == 1
            else []
        )
        single_login_cred_keys = single_existing_login_block_keys or single_global_login_cred_keys

        for index, block in enumerate(update_blocks_flat):
            block_type = block.get("block_type")
            label = block.get("label")
            positional_block_type: str | None = None
            positional_has_identity = False
            if use_positional_correspondence and index < len(positional_block_matches):
                positional_block_type, positional_has_identity = positional_block_matches[index]

            keys_to_inject: list[str] = []
            block_type_to_restore: str | None = None
            if block_type == "login":
                if label and label in existing_block_login_cred_keys:
                    keys_to_inject = existing_block_login_cred_keys[label]
                elif single_login_cred_keys:
                    keys_to_inject = single_login_cred_keys
            elif not block_type:
                # Recover by stable label first. If identity is gone, only recover
                # the single-login case; leave other ambiguous secrets untouched.
                label_block_type = existing_block_type_by_label.get(label) if label else None
                if label_block_type == "login":
                    if label and label in existing_block_login_cred_keys:
                        keys_to_inject = existing_block_login_cred_keys[label]
                        block_type_to_restore = label_block_type
                    elif single_login_cred_keys:
                        keys_to_inject = single_login_cred_keys
                        block_type_to_restore = label_block_type
                elif label_block_type:
                    if label and label in existing_block_cred_keys:
                        keys_to_inject = existing_block_cred_keys[label]
                        block_type_to_restore = label_block_type
                elif positional_has_identity and positional_block_type == "login":
                    if single_login_cred_keys:
                        keys_to_inject = single_login_cred_keys
                        block_type_to_restore = positional_block_type
            elif label and label in existing_block_cred_keys:
                keys_to_inject = existing_block_cred_keys[label]

            if keys_to_inject:
                if block_type_to_restore:
                    block["block_type"] = block_type_to_restore
                    modified = True
                block_pkeys: list[str] = list(block.get("parameter_keys") or [])
                current_keys = set(block_pkeys)
                for cred_key in keys_to_inject:
                    if cred_key not in current_keys:
                        block_pkeys.append(cred_key)
                        modified = True
                block["parameter_keys"] = block_pkeys

    # --- Step 3: Re-attach non-credential block<->parameter links the caller dropped ---
    # Plain workflow/context/output parameter links live only in a block's parameter_keys. When the
    # caller (often an LLM) regenerates a block and omits a key that the same-label block carried
    # before — while still declaring the parameter — re-attach it. Unlike credentials this is
    # best-effort and warned: to intentionally drop the link, remove the parameter declaration too.
    declared_keys = {p["key"] for p in update_params if isinstance(p, dict) and p.get("key")}
    prior_block_param_keys: dict[str, list[str]] = {}
    for block in _iter_blocks_flat(existing_wf_def.get("blocks", [])):
        if not isinstance(block, dict):
            continue
        label = block.get("label")
        if isinstance(label, str):
            prior_block_param_keys[label] = _block_authoring_parameter_keys(block)

    for block in _iter_blocks_flat(wf_def.get("blocks", [])):
        if not isinstance(block, dict):
            continue
        label = block.get("label")
        if not isinstance(label, str) or label not in prior_block_param_keys:
            continue
        block_link_keys = _block_authoring_parameter_keys(block)
        block_link_set = set(block_link_keys)
        added = False
        for link_key in prior_block_param_keys[label]:
            if link_key in all_cred_keys or link_key in block_link_set or link_key not in declared_keys:
                continue
            block_link_keys.append(link_key)
            block_link_set.add(link_key)
            warnings.append(
                f"Re-attached parameter '{link_key}' to block '{label}': it was linked in the prior version "
                f"and this update omitted it. To intentionally drop the link, remove the parameter too."
            )
            added = True
            modified = True
        if added:
            block["parameter_keys"] = block_link_keys

    if not modified:
        return definition, warnings

    return _dump_definition_dict(raw, parsed_format), warnings


def _parse_definition(definition: str, fmt: str) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Parse a workflow definition string.

    Returns (json_definition, yaml_definition, error).
    Exactly one of the first two will be set on success, or error on failure.
    JSON input is parsed into a plain dict for raw HTTP submission.
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
    query: Annotated[
        str | None,
        "Deprecated alias for search. Use search for new calls.",
    ] = None,
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 10,
    only_workflows: Annotated[bool, "Only return multi-step workflows (exclude saved tasks)"] = False,
) -> dict[str, Any]:
    """Find and browse available Skyvern workflows. Use when you need to discover what workflows exist,
    search for a workflow by name, or list all workflows for an organization."""
    if search is not None and query is not None and search != query:
        return make_result(
            "skyvern_workflow_list",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Provide either search or query, not conflicting values",
                "Use search for new calls; query is a compatibility alias.",
            ),
        )

    effective_search = search if search is not None else query
    with Timer() as timer:
        try:
            workflows = await list_workflows_raw(
                search=effective_search,
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
            "sdk_equivalent": (
                f"await skyvern.get_workflows(search_key={effective_search!r}, page={page}, page_size={page_size})"
            ),
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
            wf_data = await get_workflow_by_id(workflow_id, version)
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

    if isinstance(wf_data, dict) and isinstance(wf_data.get("workflow_definition"), dict):
        # Return the authoring shape so a get -> edit -> update round trip preserves block<->parameter
        # links (parameter_keys / loop_over_parameter_key / source_parameter_key) the runtime shape hides.
        wf_data = {
            **wf_data,
            "workflow_definition": _workflow_definition_to_authoring_shape(wf_data["workflow_definition"]),
        }

    version_str = f", version={version}" if version is not None else ""
    return make_result(
        "skyvern_workflow_get",
        data={
            **wf_data,
            "sdk_equivalent": f"# No SDK method yet — GET /api/v1/workflows/{workflow_id}{version_str}",
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_workflow_run_list(
    workflow_id: Annotated[str, "Workflow permanent ID (starts with wpid_)"],
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 10,
    status: Annotated[list[str] | None, "Filter by one or more workflow run statuses"] = None,
    search_key: Annotated[str | None, Field(description="Search workflow run IDs, parameters, and headers")] = None,
    error_code: Annotated[str | None, Field(description="Filter by task error code")] = None,
) -> dict[str, Any]:
    """List runs for a specific workflow. Use this when you have a workflow permanent ID
    and need to browse its run history with pagination and optional filters."""
    if err := validate_workflow_id(workflow_id, "skyvern_workflow_run_list"):
        return err

    with Timer() as timer:
        try:
            requested_page_size = page_size
            runs = await list_workflow_runs_raw(
                workflow_id,
                page=page,
                page_size=requested_page_size + 1,
                status=status,
                search_key=search_key,
                error_code=error_code,
            )
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_run_list",
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
                "skyvern_workflow_run_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and workflow run filters"),
            )

    has_more = len(runs) > page_size
    runs = runs[:page_size]

    return make_result(
        "skyvern_workflow_run_list",
        data={
            "workflow_id": workflow_id,
            "runs": [_serialize_run_summary(run) for run in runs],
            "page": page,
            "page_size": page_size,
            "count": len(runs),
            "has_more": has_more,
            "sdk_equivalent": (
                f"await skyvern.get_workflow_runs_by_id(workflow_id={workflow_id!r}, "
                f"page={page}, page_size={page_size})"
            ),
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_workflow_create(
    definition: Annotated[str, "Workflow definition as a YAML or JSON string"],
    format: Annotated[  # noqa: A002
        str, Field(description="Definition format: 'json', 'yaml', or 'auto' (tries JSON first, falls back to YAML)")
    ] = "auto",
    folder_id: Annotated[str | None, "Folder ID (fld_...) to organize the workflow in"] = None,
    code_only: Annotated[
        bool,
        Field(
            description="When true, structurally reject non-code browser/page block types before persisting "
            "(code-only mode)"
        ),
    ] = False,
) -> dict[str, Any]:
    """Create a reusable, versioned workflow from a YAML or JSON definition. For multi-page automations,
    scheduling, and repeated runs — not one-off trials (use skyvern_run_task for those).

    One block per step: "navigation" for actions, "extraction" for data. Do NOT use deprecated "task" type.
    Call skyvern_block_schema() for block types and schemas. Use {{parameter_key}} for input references.
    Defaults to AI agent execution (run_with="agent"). For JSON definitions, code_version=2 is also
    injected (YAML definitions go through the backend schema, which currently leaves code_version unset).
    Pass run_with="code" to opt into cached script execution. Blocks share a browser session automatically.

    Leave optional toggles and overrides unset unless the user explicitly asks for them. This
    applies to workflow-level fields (persist_browser_session, pin_saved_session_ip, extra_http_headers,
    totp_verification_url, totp_identifier, etc.) AND block-level overrides (max_retries,
    max_steps_per_run, totp_identifier, complete_criterion, error_code_mapping,
    continue_on_failure, engine, model, etc.). The schema defaults are intentional; silently
    flipping them changes behavior the user did not request.
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

    if code_only:
        if json_def is not None:
            if err := _validate_code_only_workflow_blocks(json_def, "skyvern_workflow_create"):
                return err
        elif err := _validate_code_only_definition(definition, format, "skyvern_workflow_create"):
            return err

    with Timer() as timer:
        try:
            workflow = await create_workflow_raw(
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

    LOG.info("workflow_created", workflow_id=workflow.get("workflow_permanent_id"))
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
    code_only: Annotated[
        bool,
        Field(
            description="When true, structurally reject non-code browser/page block types before persisting "
            "(code-only mode)"
        ),
    ] = False,
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

    if code_only:
        if err := _validate_code_only_definition(definition, format, "skyvern_workflow_update"):
            return err

    param_warnings: list[str] = []
    try:
        definition = await _inject_workflow_update_proxy_default(definition, format, workflow_id)
        definition = await _inject_workflow_update_top_level_settings(definition, format, workflow_id)
        definition, param_warnings = await _inject_workflow_update_parameters(definition, format, workflow_id)
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
        LOG.warning("workflow_update_preprocessing_failed", workflow_id=workflow_id, error=str(e))
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

    with Timer() as timer:
        try:
            workflow = await update_workflow_raw(
                workflow_id,
                json_definition=json_def,
                yaml_definition=yaml_def,
            )
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_update",
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
    if param_warnings:
        data["warnings"] = param_warnings
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

    with Timer() as timer:
        try:
            workflow = await update_workflow_folder_raw(workflow_id, folder_id=folder_id)
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
                run = await get_workflow_run_status(
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


async def skyvern_workflow_retry(
    workflow_run_id: Annotated[str, "Workflow run ID to retry (wr_...)"],
) -> dict[str, Any]:
    """Retry a terminal workflow run. Creates a new workflow run using the original run
    parameters and run settings."""
    if err := validate_workflow_run_id(workflow_run_id, "skyvern_workflow_retry"):
        return err

    with Timer() as timer:
        try:
            run = await retry_workflow_run_raw(workflow_run_id)
            timer.mark("sdk")
        except NotFoundError:
            return make_result(
                "skyvern_workflow_retry",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.RUN_NOT_FOUND,
                    f"Workflow run {workflow_run_id!r} not found",
                    "Verify the workflow run ID with skyvern_workflow_run_list or skyvern_workflow_status",
                ),
            )
        except BadRequestError as e:
            return make_result(
                "skyvern_workflow_retry",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    _api_error_body_message(e.body),
                    "Only terminal workflow runs can be retried.",
                ),
            )
        except Exception as e:
            LOG.error("workflow_retry_failed", workflow_run_id=workflow_run_id, error=str(e))
            return make_result(
                "skyvern_workflow_retry",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the workflow run ID and your API key"),
            )

    retry_run_id = _get_value(run, "run_id") or _get_value(run, "workflow_run_id")
    LOG.info("workflow_retried", workflow_run_id=workflow_run_id, retry_run_id=retry_run_id)
    return make_result(
        "skyvern_workflow_retry",
        data={
            "workflow_run_id": workflow_run_id,
            "retry_run": _serialize_run_summary(run),
            "sdk_equivalent": f"await skyvern.retry_workflow_run({workflow_run_id!r})",
        },
        timing_ms=timer.timing_ms,
    )
