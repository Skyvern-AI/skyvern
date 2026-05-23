"""Investigate-Artifacts skills — post-run only.

Wraps the cross-cutting "what happened during the run" queries. Three skills
backed by DB rows that already live in ``skyvern/`` (run outcome, block
outcomes, episodes-for-run), plus three skills whose implementation is
necessarily cloud-only (recording URL, screenshots, Datadog logs).

For the cloud-only skills, this module exposes stubs that return
``status='not_available'`` locally. The production path routes through
:class:`AgentFunction` in ``cloud/agent_functions.py`` — see the dispatcher
in ``_resolve_artifact_provider``. The agent prompt instructs the LLM to
gracefully skip skills that return ``not_available``.

Context shape: :class:`PostRunContext`.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillError, SkillResult

LOG = structlog.get_logger()


def _ctx_field(context: Any, name: str) -> Any:
    if hasattr(context, name):
        return getattr(context, name)
    return None


async def _handler_get_workflow_run_outcome(args: dict[str, Any], context: Any) -> SkillResult:
    """Return status + duration + last-block info for the run.

    Used by the post-run agent's prompt to decide which blocks to focus on.
    """
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    org_id = _ctx_field(context, "organization_id")
    if not workflow_run_id or not org_id:
        raise SkillError("workflow_run_id and organization_id are required")
    try:
        wr = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=str(workflow_run_id),
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")
    if wr is None:
        return SkillResult.not_available(f"no workflow_run {workflow_run_id!r}")

    duration: float | None = None
    if wr.started_at and wr.modified_at:
        try:
            duration = (wr.modified_at - wr.started_at).total_seconds()
        except Exception:
            duration = None

    return SkillResult.ok(
        data={
            "workflow_run_id": wr.workflow_run_id,
            "status": getattr(wr.status, "value", str(wr.status)) if wr.status else None,
            "failure_reason": _truncate(getattr(wr, "failure_reason", None), 500),
            "started_at": wr.started_at.isoformat() if wr.started_at else None,
            "modified_at": wr.modified_at.isoformat() if wr.modified_at else None,
            "duration_seconds": duration,
        }
    )


async def _handler_get_block_outcomes_for_run(args: dict[str, Any], context: Any) -> SkillResult:
    """Per-block status + label + task_id for the run, newest-first."""
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    org_id = _ctx_field(context, "organization_id")
    if not workflow_run_id or not org_id:
        raise SkillError("workflow_run_id and organization_id are required")
    try:
        blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=str(workflow_run_id),
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")

    out = [
        {
            "block_label": getattr(b, "label", None),
            "block_type": getattr(b, "block_type", None) and str(b.block_type),
            "status": getattr(b, "status", None) and str(b.status),
            "task_id": getattr(b, "task_id", None),
            "failure_reason": _truncate(getattr(b, "failure_reason", None), 300),
            "created_at": getattr(b, "created_at", None) and b.created_at.isoformat(),
        }
        for b in blocks
    ]
    return SkillResult.ok(data={"workflow_run_id": str(workflow_run_id), "blocks": out, "count": len(out)})


async def _handler_get_episodes_for_run(args: dict[str, Any], context: Any) -> SkillResult:
    """All fallback episodes for the run (any reviewed-state, any type)."""
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    org_id = _ctx_field(context, "organization_id")
    if not workflow_run_id or not org_id:
        raise SkillError("workflow_run_id and organization_id are required")
    try:
        episodes = await app.DATABASE.scripts.get_all_episodes_by_workflow_run_id(
            workflow_run_id=str(workflow_run_id),
            organization_id=str(org_id),
        )
    except Exception as exc:
        raise SkillError(f"db_error: {type(exc).__name__}: {exc}")

    out = [
        {
            "episode_id": e.episode_id,
            "block_label": e.block_label,
            "fallback_type": e.fallback_type,
            "error_message": _truncate(e.error_message, 400),
            "classify_result": e.classify_result,
            "reviewed": e.reviewed,
            "reviewer_version": e.reviewer_version,
            "fallback_succeeded": e.fallback_succeeded,
            "page_url": e.page_url,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "reviewer_output": _truncate(e.reviewer_output, 200),
        }
        for e in episodes
    ]
    return SkillResult.ok(data={"workflow_run_id": str(workflow_run_id), "episodes": out, "count": len(out)})


async def _handler_get_workflow_recording_url(args: dict[str, Any], context: Any) -> SkillResult:
    """Cloud-only stub. Production override lives in ``cloud/agent_functions.py``.

    Locally we surface a structured ``not_available`` so the agent prompt can
    instruct the LLM to skip the skill rather than reason about an empty
    response.
    """
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    # If app.AGENT_FUNCTION exposes a hook, call it.
    fn = getattr(app, "AGENT_FUNCTION", None)
    if fn is not None and hasattr(fn, "get_workflow_recording_url"):
        try:
            url = await fn.get_workflow_recording_url(workflow_run_id=str(workflow_run_id))  # type: ignore[attr-defined]
            if url:
                return SkillResult.ok(data={"workflow_run_id": str(workflow_run_id), "recording_url": url})
        except Exception as exc:
            LOG.debug("AGENT_FUNCTION.get_workflow_recording_url failed", exc_info=True)
            return SkillResult.error(f"recording_url_error: {type(exc).__name__}: {exc}")
    return SkillResult.not_available("recording URL not available in this environment")


async def _handler_get_screenshots_for_block(args: dict[str, Any], context: Any) -> SkillResult:
    """Cloud-only stub. See module docstring."""
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    block_label = args.get("block_label")
    if not block_label:
        raise SkillError("block_label is required")
    fn = getattr(app, "AGENT_FUNCTION", None)
    if fn is not None and hasattr(fn, "get_screenshots_for_block"):
        try:
            urls = await fn.get_screenshots_for_block(  # type: ignore[attr-defined]
                workflow_run_id=str(workflow_run_id),
                block_label=str(block_label),
            )
            if urls:
                return SkillResult.ok(
                    data={
                        "workflow_run_id": str(workflow_run_id),
                        "block_label": block_label,
                        "screenshot_urls": list(urls)[:20],
                    }
                )
        except Exception as exc:
            return SkillResult.error(f"screenshots_error: {type(exc).__name__}: {exc}")
    return SkillResult.not_available("screenshots not available in this environment")


async def _handler_get_datadog_logs(args: dict[str, Any], context: Any) -> SkillResult:
    """Cloud-only stub. See module docstring."""
    workflow_run_id = args.get("workflow_run_id") or _ctx_field(context, "workflow_run_id")
    log_filter = args.get("log_filter") or "errors"
    fn = getattr(app, "AGENT_FUNCTION", None)
    if fn is not None and hasattr(fn, "get_datadog_logs"):
        try:
            logs = await fn.get_datadog_logs(  # type: ignore[attr-defined]
                workflow_run_id=str(workflow_run_id),
                log_filter=log_filter,
            )
            return SkillResult.ok(
                data={
                    "workflow_run_id": str(workflow_run_id),
                    "log_filter": log_filter,
                    "logs": logs,
                }
            )
        except Exception as exc:
            return SkillResult.error(f"datadog_error: {type(exc).__name__}: {exc}")
    return SkillResult.not_available("datadog logs not available in this environment")


def _truncate(s: str | None, max_chars: int) -> str | None:
    if s is None:
        return None
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"...<truncated {len(s) - max_chars} chars>"


_POSTRUN_ONLY = frozenset({"postrun"})


def all_artifact_skills() -> list[Skill]:
    return [
        Skill(
            name="get_workflow_run_outcome",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_workflow_run_outcome,
            schema={
                "name": "get_workflow_run_outcome",
                "description": (
                    "Return the final status, failure_reason, started_at, modified_at, and "
                    "duration_seconds for a workflow run. Call once at the start of post-run "
                    "review to orient yourself."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"workflow_run_id": {"type": "string"}},
                    "required": [],
                },
            },
        ),
        Skill(
            name="get_block_outcomes_for_run",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_block_outcomes_for_run,
            schema={
                "name": "get_block_outcomes_for_run",
                "description": "Per-block status / type / failure_reason for the run, newest-first.",
                "input_schema": {
                    "type": "object",
                    "properties": {"workflow_run_id": {"type": "string"}},
                    "required": [],
                },
            },
        ),
        Skill(
            name="get_episodes_for_run",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_episodes_for_run,
            schema={
                "name": "get_episodes_for_run",
                "description": (
                    "Return every fallback episode for the run (any reviewed-state, any type, "
                    "any reviewer_version). The post-run agent typically calls this first to "
                    "pick which episodes to review per-episode."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"workflow_run_id": {"type": "string"}},
                    "required": [],
                },
            },
        ),
        Skill(
            name="get_workflow_recording_url",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_workflow_recording_url,
            schema={
                "name": "get_workflow_recording_url",
                "description": (
                    "Return a presigned URL to the browser session recording for the run. "
                    "Cloud-only — returns status='not_available' in OSS/local."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"workflow_run_id": {"type": "string"}},
                    "required": [],
                },
            },
        ),
        Skill(
            name="get_screenshots_for_block",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_screenshots_for_block,
            schema={
                "name": "get_screenshots_for_block",
                "description": (
                    "Return up to 20 presigned URLs to step screenshots from the specified "
                    "block. Cloud-only — returns status='not_available' in OSS/local."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "workflow_run_id": {"type": "string"},
                        "block_label": {"type": "string"},
                    },
                    "required": ["block_label"],
                },
            },
        ),
        Skill(
            name="get_datadog_logs",
            available_to=_POSTRUN_ONLY,
            handler=_handler_get_datadog_logs,
            schema={
                "name": "get_datadog_logs",
                "description": (
                    "Query Datadog for structured log events tied to the workflow_run. "
                    "log_filter is one of 'errors', 'llm_calls', 'all'. Cloud-only — returns "
                    "status='not_available' in OSS/local."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "workflow_run_id": {"type": "string"},
                        "log_filter": {"type": "string", "enum": ["errors", "llm_calls", "all"]},
                    },
                    "required": [],
                },
            },
        ),
    ]


__all__ = ["all_artifact_skills"]
