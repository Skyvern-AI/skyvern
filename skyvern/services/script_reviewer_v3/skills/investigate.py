"""Investigate skills — shared between mid-run and post-run agents.

Each skill reads from the scripts repository / workflow tables and returns a
structured payload. None of these skills mutate. The agent uses them to:

- Pull current block/script source so it can produce an edit.
- Pull historical episode records for the same block to spot recurring
  failure modes.
- Pull run-parameter values for the current run (param keys + values) so the
  agent can reference them in any persisted edit.

Context shape: the skill handlers expect a ``context`` object with at least
``organization_id``, ``script_revision_id`` (latest), and either
``workflow_permanent_id`` (mid-run) or ``workflow_run_id`` (post-run). Both
:class:`FailureContext` and :class:`PostRunContext` satisfy this via duck
typing — we read attributes by name.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillError, SkillResult

LOG = structlog.get_logger()


def _ctx_field(context: Any, name: str) -> Any:
    """Pull a field from FailureContext or PostRunContext, or from
    SkyvernContext on FailureContext.context."""
    if hasattr(context, name):
        return getattr(context, name)
    inner = getattr(context, "context", None)
    if inner is not None and hasattr(inner, name):
        return getattr(inner, name)
    return None


async def _read_script_file_content(file_row: Any, organization_id: str) -> str | None:
    """Resolve the actual file contents from S3 via the artifact manager.

    ScriptFile only stores metadata + an artifact_id; the body lives in
    artifact storage. Mirrors the read path used by v2's workflow_script_service
    when loading cached blocks.
    """
    artifact_id = getattr(file_row, "artifact_id", None)
    if not artifact_id:
        return None
    try:
        artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id)
    except Exception as exc:
        raise SkillError(f"artifact_lookup_error: {type(exc).__name__}: {exc}")
    if artifact is None:
        return None
    try:
        content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
    except Exception as exc:
        raise SkillError(f"artifact_retrieve_error: {type(exc).__name__}: {exc}")
    if isinstance(content, (bytes, bytearray)):
        try:
            return content.decode("utf-8")
        except Exception:
            return repr(content)
    return content if isinstance(content, str) else None


async def _resolve_script_id(context: Any) -> str | None:
    """Find the script_id for the current context.

    Mid-run: from SkyvernContext.script_id. Post-run: from PostRunContext or
    derived from the latest workflow_script binding for the wpid.
    """
    sid = _ctx_field(context, "script_id")
    if sid:
        return str(sid)
    rev_id = _ctx_field(context, "script_revision_id")
    org_id = _ctx_field(context, "organization_id")
    if rev_id and org_id:
        script = await app.DATABASE.scripts.get_script_revision(
            script_revision_id=str(rev_id),
            organization_id=str(org_id),
        )
        if script is not None:
            return script.script_id
    return None


async def _handler_get_block_code(args: dict[str, Any], context: Any) -> SkillResult:
    """Return the @cached function body for ``block_label``.

    Reads from ScriptBlock rows on the latest script revision in context. The
    LLM uses this to know what code it's about to patch.
    """
    block_label = args.get("block_label")
    if not block_label or not isinstance(block_label, str):
        raise SkillError("block_label is required")

    org_id = _ctx_field(context, "organization_id")
    rev_id = args.get("script_revision_id") or _ctx_field(context, "script_revision_id")
    if not org_id or not rev_id:
        raise SkillError("missing organization_id or script_revision_id in context")

    try:
        block = await app.DATABASE.scripts.get_script_block_by_label(
            script_revision_id=str(rev_id),
            script_block_label=block_label,
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")

    if block is None:
        return SkillResult.not_available(f"no script_block with label {block_label!r}")

    file_id = getattr(block, "script_file_id", None) or getattr(block, "file_id", None)
    if not file_id:
        return SkillResult.not_available("script_block has no associated file_id")
    try:
        file_row = await app.DATABASE.scripts.get_script_file_by_id(
            script_revision_id=str(rev_id),
            file_id=str(file_id),
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")
    if file_row is None:
        return SkillResult.not_available("script_file row missing")
    content = await _read_script_file_content(file_row, str(org_id))
    if not content:
        return SkillResult.not_available("script_file has no resolvable artifact content")
    return SkillResult.ok(
        data={
            "block_label": block_label,
            "script_revision_id": str(rev_id),
            "file_path": file_row.file_path,
            "content_chars": len(content),
            "content": content,
        }
    )


async def _handler_get_full_script(args: dict[str, Any], context: Any) -> SkillResult:
    """Return the full main.py for the current script revision."""
    org_id = _ctx_field(context, "organization_id")
    rev_id = args.get("script_revision_id") or _ctx_field(context, "script_revision_id")
    if not org_id or not rev_id:
        raise SkillError("missing organization_id or script_revision_id")

    try:
        # Convention: main.py lives at the script root.
        file_row = await app.DATABASE.scripts.get_script_file_by_path(
            script_revision_id=str(rev_id),
            file_path="main.py",
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")
    if file_row is None:
        return SkillResult.not_available("main.py not found on revision")
    content = await _read_script_file_content(file_row, str(org_id))
    if not content:
        return SkillResult.not_available("main.py has no resolvable artifact content")
    return SkillResult.ok(
        data={
            "script_revision_id": str(rev_id),
            "file_path": "main.py",
            "content_chars": len(content),
            "content": content,
        }
    )


async def _handler_get_past_episodes_for_block(args: dict[str, Any], context: Any) -> SkillResult:
    """Cross-run history. Returns up to ``limit`` past episodes whose
    block_label matches, for the same wpid. Newest first.

    Used by the agent to spot recurring failure modes ("same selector has
    failed 3× in 5 runs"). Doesn't include the current run's episodes — the
    agent already has those in its prompt (mid-run) or via
    ``get_episodes_for_run`` (post-run).
    """
    block_label = args.get("block_label")
    if not block_label:
        raise SkillError("block_label is required")
    limit = int(args.get("limit") or 20)
    limit = max(1, min(50, limit))

    org_id = _ctx_field(context, "organization_id")
    wpid = _ctx_field(context, "workflow_permanent_id")
    if not org_id or not wpid:
        raise SkillError("missing organization_id or workflow_permanent_id")

    try:
        episodes = await app.DATABASE.scripts.get_unreviewed_episodes(
            workflow_permanent_id=str(wpid),
            organization_id=str(org_id),
            limit=limit * 3,  # over-fetch to allow client-side block_label filter
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")

    matching = [e for e in episodes if e.block_label == block_label][:limit]
    return SkillResult.ok(
        data={
            "block_label": block_label,
            "limit": limit,
            "matches": len(matching),
            "episodes": [
                {
                    "episode_id": e.episode_id,
                    "workflow_run_id": e.workflow_run_id,
                    "fallback_type": e.fallback_type,
                    "error_message": _truncate(e.error_message, 300),
                    "classify_result": e.classify_result,
                    "reviewer_output": _truncate(e.reviewer_output, 200),
                    "reviewer_version": e.reviewer_version,
                    "reviewed": e.reviewed,
                    "fallback_succeeded": e.fallback_succeeded,
                    "page_url": e.page_url,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in matching
            ],
        }
    )


async def _handler_get_run_parameter_values(args: dict[str, Any], context: Any) -> SkillResult:
    """Return non-secret run-parameter key→value pairs for the current run.

    Reuses v2's ``load_filtered_run_param_values`` helper, which already drops
    secret/credential params via ``is_sensitive_workflow_parameter``.
    """
    workflow_run_id = _ctx_field(context, "workflow_run_id")
    if not workflow_run_id:
        raise SkillError("workflow_run_id missing in context")
    try:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        values = await load_filtered_run_param_values(workflow_run_id=str(workflow_run_id))
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")
    return SkillResult.ok(data={"workflow_run_id": str(workflow_run_id), "parameters": values})


async def _handler_get_cross_run_parameter_values(args: dict[str, Any], context: Any) -> SkillResult:
    """Return non-secret parameter values from the N most recent workflow runs.

    KEY USE: lets the agent see how parameter values VARY across runs of the
    same wpid. Any value that varies between runs is runtime data — NOT a
    constant — and must not be baked into selectors or click values. Pair
    this with ``validate_no_hardcoded_values`` before persisting an edit.
    """
    org_id = _ctx_field(context, "organization_id")
    wpid = _ctx_field(context, "workflow_permanent_id")
    if not (org_id and wpid):
        raise SkillError("missing organization_id or workflow_permanent_id")
    limit = int(args.get("limit") or 10)
    limit = max(2, min(30, limit))

    try:
        from skyvern.services.script_reviewer import load_filtered_run_param_values

        runs = await app.DATABASE.workflow_runs.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=str(wpid),
            organization_id=str(org_id),
            page=1,
            page_size=limit,
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")

    per_run: list[dict[str, Any]] = []
    # Aggregate which keys vary across runs and which stay constant.
    key_to_values: dict[str, set[str]] = {}
    for wr in runs:
        try:
            values = await load_filtered_run_param_values(workflow_run_id=wr.workflow_run_id)
        except Exception:
            LOG.debug(
                "Failed to load run params for cross-run skill", workflow_run_id=wr.workflow_run_id, exc_info=True
            )
            values = {}
        per_run.append(
            {
                "workflow_run_id": wr.workflow_run_id,
                "status": getattr(wr.status, "value", str(wr.status)) if getattr(wr, "status", None) else None,
                "created_at": wr.created_at.isoformat() if getattr(wr, "created_at", None) else None,
                "parameters": values,
            }
        )
        for k, v in values.items():
            key_to_values.setdefault(k, set()).add(v)

    variable_keys = sorted(k for k, vs in key_to_values.items() if len(vs) > 1)
    constant_keys = sorted(k for k, vs in key_to_values.items() if len(vs) == 1)

    return SkillResult.ok(
        data={
            "workflow_permanent_id": str(wpid),
            "runs_inspected": len(per_run),
            "variable_keys": variable_keys,
            "constant_across_runs": constant_keys,
            "hint": (
                "variable_keys hold values that change across runs — these strings MUST be referenced "
                "via context.parameters['key'], never baked into selectors or click values. "
                "constant_across_runs are values that happened to be identical across the sample; "
                "treat them as runtime-variable too unless the workflow definition declares them constant."
            ),
            "per_run": per_run,
        }
    )


def _truncate(s: str | None, max_chars: int) -> str | None:
    if s is None:
        return None
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"...<truncated {len(s) - max_chars} chars>"


def all_investigate_skills() -> list[Skill]:
    return [
        Skill(
            name="get_block_code",
            handler=_handler_get_block_code,
            schema={
                "name": "get_block_code",
                "description": (
                    "Return the @cached function body for a specific block_label from the "
                    "current script revision. Call this BEFORE proposing a persist_block_edit "
                    "so your patch is based on the actual code."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "block_label": {"type": "string"},
                        "script_revision_id": {
                            "type": "string",
                            "description": "Optional explicit revision; defaults to the latest in context.",
                        },
                    },
                    "required": ["block_label"],
                },
            },
        ),
        Skill(
            name="get_full_script",
            handler=_handler_get_full_script,
            schema={
                "name": "get_full_script",
                "description": (
                    "Return the full main.py for the current (or specified) script revision. "
                    "Call this before persist_script_rewrite so your rewrite is based on the "
                    "actual current code."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "script_revision_id": {"type": "string"},
                    },
                    "required": [],
                },
            },
        ),
        Skill(
            name="get_past_episodes_for_block",
            handler=_handler_get_past_episodes_for_block,
            schema={
                "name": "get_past_episodes_for_block",
                "description": (
                    "Return up to ``limit`` historical fallback episodes whose block_label "
                    "matches, across recent runs of the same wpid. Useful for detecting "
                    "recurring selector failures."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "block_label": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    },
                    "required": ["block_label"],
                },
            },
        ),
        Skill(
            name="get_run_parameter_values",
            handler=_handler_get_run_parameter_values,
            schema={
                "name": "get_run_parameter_values",
                "description": (
                    "Return the non-secret workflow parameter key→value pairs for the current "
                    "workflow run. Use these values when building selector predicates that "
                    "should reference parameters by key (e.g., page.fill(prompt='${user.email}'))."
                ),
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
        ),
        Skill(
            name="get_cross_run_parameter_values",
            handler=_handler_get_cross_run_parameter_values,
            schema={
                "name": "get_cross_run_parameter_values",
                "description": (
                    "Return the non-secret workflow parameter values from the N most recent runs "
                    "of the same wpid. CRITICAL for cross-run safety: shows which parameter values "
                    "VARY across runs (those strings are runtime data; must NEVER be baked into "
                    "selectors or click values) vs which happened to be identical (still treat as "
                    "runtime data). Call this BEFORE proposing any selector that contains string "
                    "literals from get_run_parameter_values."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 2, "maximum": 30, "default": 10}},
                    "required": [],
                },
            },
        ),
    ]


__all__ = ["all_investigate_skills"]
