"""Persist skills — both v3 agents can write new script versions.

Two skills:

- ``persist_block_edit`` — wraps the existing v2 helper
  ``create_script_version_from_review`` (libcst-based block-body patch).
- ``persist_script_rewrite`` — wraps the new (PR 1) helper
  ``create_script_version_from_full_code`` (verbatim main.py replacement).

Both share the same wrapping concerns:

1. Daily per-wpid cap (atomic via ``WorkflowService._check_and_increment_cap_v3``).
2. Redis lock ``v3_persist:{script_id}`` so concurrent agents don't race on
   the same script.
3. Pinned-script handling — the underlying helpers return ``None`` rather than
   raising; we surface that as ``{ok, persisted: False, reason: 'pinned'}``.
4. Context update — on success, ``context.script_revision_id`` is advanced so
   subsequent skills (and the post-run agent) operate on the new revision.

Note: the underlying helpers compile-check internally too — the agent SHOULD
have called ``compile_check`` first, but defense-in-depth is preserved.
"""

from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillError, SkillResult
from skyvern.services.script_reviewer_v3.types import PostRunContext

LOG = structlog.get_logger()


def _ctx_field(context: Any, name: str) -> Any:
    if hasattr(context, name):
        return getattr(context, name)
    inner = getattr(context, "context", None)
    if inner is not None and hasattr(inner, name):
        return getattr(inner, name)
    return None


async def _resolve_persist_context(context: Any) -> dict[str, Any]:
    """Pull the fields needed by the persist helpers from any agent context."""
    org_id = _ctx_field(context, "organization_id")
    wpid = _ctx_field(context, "workflow_permanent_id")
    rev_id = _ctx_field(context, "script_revision_id")
    wrid = _ctx_field(context, "workflow_run_id")
    if not (org_id and wpid and rev_id and wrid):
        raise SkillError(
            "missing required context fields (organization_id / workflow_permanent_id / "
            "script_revision_id / workflow_run_id)"
        )

    base_script = await app.DATABASE.scripts.get_script_revision(
        script_revision_id=str(rev_id),
        organization_id=str(org_id),
    )
    if base_script is None:
        raise SkillError(f"no script revision {rev_id!r} for org {org_id!r}")

    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=str(wpid),
        organization_id=str(org_id),
    )
    if workflow is None:
        raise SkillError(f"no workflow for wpid {wpid!r}")

    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=str(wrid),
        organization_id=str(org_id),
    )
    if workflow_run is None:
        raise SkillError(f"no workflow_run {wrid!r}")

    return {
        "organization_id": str(org_id),
        "workflow_permanent_id": str(wpid),
        "base_script": base_script,
        "workflow": workflow,
        "workflow_run": workflow_run,
    }


def _update_context_revision(
    new_script_id: str | None,
    new_revision_id: str | None,
    agent_context: Any = None,
) -> None:
    """Advance script_revision_id on BOTH SkyvernContext AND the agent's
    context object (PostRunContext / FailureContext) so subsequent skill
    calls inside the same agent loop see the post-persist revision.

    Without the agent_context mutation, the second persist in a single
    post-run would fork from the stale base revision recorded in
    PostRunContext at build time."""
    if not new_revision_id:
        return
    ctx = skyvern_context.current()
    if ctx is not None:
        ctx.script_revision_id = new_revision_id
        if new_script_id:
            ctx.script_id = new_script_id
    # Also mutate the agent's context object (PostRunContext / FailureContext)
    # so _ctx_field reads the new revision from the top-level context object.
    if agent_context is not None:
        if hasattr(agent_context, "script_revision_id"):
            agent_context.script_revision_id = new_revision_id
        if new_script_id and hasattr(agent_context, "script_id"):
            agent_context.script_id = new_script_id


def _record_v3_persist_cap_acquisition(agent_context: Any) -> None:
    """Mark contexts that already consumed the v3 persist cap."""
    if not isinstance(agent_context, PostRunContext):
        return
    agent_context.v3_persist_cap_consumed = True
    current_count = getattr(agent_context, "v3_persist_cap_acquisitions", 0) or 0
    agent_context.v3_persist_cap_acquisitions = int(current_count) + 1


async def _enforce_v3_cap(
    workflow_permanent_id: str,
    organization_id: str | None = None,
) -> tuple[bool, int | None]:
    """Atomic cap check + increment for v3 persist. Uses the helper defined in
    workflow/service.py:_check_and_increment_cap_v3 to keep the cap semantics
    identical between mid-run and post-run paths.

    ``organization_id`` is forwarded so org-specific cap overrides via PostHog
    payload are honored. Without it, every org gets the global default cap.
    Returns ``(acquired, new_counter)``.
    """
    try:
        # ``WorkflowService`` is imported lazily because it imports
        # ``workflow_script_service``, which (in the wiring layer) imports
        # back into v3. The circular dep is broken at call time.
        from skyvern.forge.sdk.workflow.service import WorkflowService

        svc = WorkflowService()
        counter = await svc._check_and_increment_cap_v3(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning("v3 cap check failed; failing closed", exc_info=True)
        return False, None
    if counter is None:
        return False, None
    return True, int(counter)


async def _handler_persist_block_edit(args: dict[str, Any], context: Any) -> SkillResult:
    block_label = args.get("block_label")
    code = args.get("code")
    if not block_label or not isinstance(block_label, str):
        raise SkillError("block_label is required")
    if not isinstance(code, str) or not code.strip():
        raise SkillError("code is required (non-empty string)")

    persist_ctx = await _resolve_persist_context(context)
    wpid = persist_ctx["workflow_permanent_id"]
    org_id = persist_ctx["organization_id"]

    acquired, counter = await _enforce_v3_cap(wpid, organization_id=org_id)
    if not acquired:
        return SkillResult.ok(
            data={
                "persisted": False,
                "reason": "daily_cap_exceeded",
                "hint": "Per-wpid daily v3 persist cap reached. No edit was applied.",
            }
        )
    _record_v3_persist_cap_acquisition(context)

    # Per-script Redis lock to serialize concurrent persists. v3_persist scope
    # is distinct from v2's script_reviewer:{} lock so the two don't deadlock.
    cache = CacheFactory.get_cache()
    base_script_id = persist_ctx["base_script"].script_id
    new_script = None
    if cache is not None:
        try:
            lock = cache.get_lock(f"v3_persist:{base_script_id}", blocking_timeout=2, timeout=60)
        except AttributeError:
            lock = None
        if lock is not None:
            try:
                async with lock:
                    new_script = await _do_persist_block(persist_ctx, block_label=block_label, code=code)
            except Exception as exc:
                raise SkillError(f"persist_block_edit_failed: {type(exc).__name__}: {exc}")
        else:
            new_script = await _do_persist_block(persist_ctx, block_label=block_label, code=code)
    else:
        new_script = await _do_persist_block(persist_ctx, block_label=block_label, code=code)

    if new_script is None:
        LOG.info(
            "v3_persist_block_edit_skipped",
            block_label=block_label,
            workflow_permanent_id=wpid,
            reason="helper_returned_none",
        )
        return SkillResult.ok(
            data={
                "persisted": False,
                "reason": "helper_returned_none",
                "hint": "create_script_version_from_review returned None (likely pinned or compile-failed).",
            }
        )

    _update_context_revision(new_script.script_id, new_script.script_revision_id, agent_context=context)
    LOG.info(
        "v3_persist_block_edit_succeeded",
        block_label=block_label,
        workflow_permanent_id=wpid,
        new_script_revision_id=new_script.script_revision_id,
        new_script_version=new_script.version,
        new_script_id=new_script.script_id,
        daily_cap_counter=counter,
        code_chars=len(code),
    )
    return SkillResult.ok(
        data={
            "persisted": True,
            "new_script_revision_id": new_script.script_revision_id,
            "new_script_version": new_script.version,
            "daily_cap_counter": counter,
            "applied_fix_description": {"kind": "block_edit", "block_label": block_label},
            "new_script_id": new_script.script_id,
        }
    )


async def _do_persist_block(persist_ctx: dict[str, Any], block_label: str, code: str) -> Any:
    from skyvern.services.workflow_script_service import create_script_version_from_review

    return await create_script_version_from_review(
        organization_id=persist_ctx["organization_id"],
        workflow_permanent_id=persist_ctx["workflow_permanent_id"],
        base_script=persist_ctx["base_script"],
        updated_blocks={block_label: code},
        workflow=persist_ctx["workflow"],
        workflow_run=persist_ctx["workflow_run"],
    )


async def _handler_persist_script_rewrite(args: dict[str, Any], context: Any) -> SkillResult:
    full_main_py = args.get("full_main_py")
    if not isinstance(full_main_py, str) or not full_main_py.strip():
        raise SkillError("full_main_py is required (non-empty string)")

    persist_ctx = await _resolve_persist_context(context)
    wpid = persist_ctx["workflow_permanent_id"]
    org_id = persist_ctx["organization_id"]

    acquired, counter = await _enforce_v3_cap(wpid, organization_id=org_id)
    if not acquired:
        return SkillResult.ok(
            data={
                "persisted": False,
                "reason": "daily_cap_exceeded",
            }
        )
    _record_v3_persist_cap_acquisition(context)

    cache = CacheFactory.get_cache()
    base_script_id = persist_ctx["base_script"].script_id
    if cache is not None:
        try:
            lock = cache.get_lock(f"v3_persist:{base_script_id}", blocking_timeout=2, timeout=60)
        except AttributeError:
            lock = None
        if lock is not None:
            try:
                async with lock:
                    new_script = await _do_persist_script(persist_ctx, full_main_py=full_main_py)
            except Exception as exc:
                raise SkillError(f"persist_script_rewrite_failed: {type(exc).__name__}: {exc}")
        else:
            new_script = await _do_persist_script(persist_ctx, full_main_py=full_main_py)
    else:
        new_script = await _do_persist_script(persist_ctx, full_main_py=full_main_py)

    if new_script is None:
        LOG.info(
            "v3_persist_script_rewrite_skipped",
            workflow_permanent_id=wpid,
            reason="helper_returned_none",
        )
        return SkillResult.ok(
            data={
                "persisted": False,
                "reason": "helper_returned_none",
                "hint": "create_script_version_from_full_code returned None (pinned or compile-failed).",
            }
        )

    _update_context_revision(new_script.script_id, new_script.script_revision_id, agent_context=context)
    LOG.info(
        "v3_persist_script_rewrite_succeeded",
        workflow_permanent_id=wpid,
        new_script_revision_id=new_script.script_revision_id,
        new_script_version=new_script.version,
        new_script_id=new_script.script_id,
        daily_cap_counter=counter,
        full_main_py_chars=len(full_main_py),
    )
    return SkillResult.ok(
        data={
            "persisted": True,
            "new_script_revision_id": new_script.script_revision_id,
            "new_script_version": new_script.version,
            "daily_cap_counter": counter,
            "applied_fix_description": {"kind": "script_rewrite"},
            "new_script_id": new_script.script_id,
        }
    )


async def _do_persist_script(persist_ctx: dict[str, Any], full_main_py: str) -> Any:
    from skyvern.services.workflow_script_service import create_script_version_from_full_code

    return await create_script_version_from_full_code(
        organization_id=persist_ctx["organization_id"],
        workflow_permanent_id=persist_ctx["workflow_permanent_id"],
        base_script=persist_ctx["base_script"],
        full_main_py=full_main_py,
        workflow=persist_ctx["workflow"],
        workflow_run=persist_ctx["workflow_run"],
    )


def all_persist_skills() -> list[Skill]:
    return [
        Skill(
            name="persist_block_edit",
            handler=_handler_persist_block_edit,
            schema={
                "name": "persist_block_edit",
                "description": (
                    "Apply a narrow edit to a single @cached function body. The edit replaces "
                    "the function body verbatim. RUN compile_check, validate_page_api, AND "
                    "validate_method_kwargs on the code BEFORE calling this skill. Returns "
                    "persisted=False with reason on failure (pinned, daily_cap_exceeded, "
                    "compile_failed)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "block_label": {"type": "string"},
                        "code": {
                            "type": "string",
                            "description": "Complete function source for the block (async def block_<label>(page): ...).",
                        },
                    },
                    "required": ["block_label", "code"],
                },
            },
        ),
        Skill(
            name="persist_script_rewrite",
            handler=_handler_persist_script_rewrite,
            schema={
                "name": "persist_script_rewrite",
                "description": (
                    "Replace the entire main.py with a new version. Use this when the right "
                    "fix is at the orchestrator level (control flow, helper functions, FIELD_MAP "
                    "constants, imports). RUN compile_check, validate_required_blocks_present, "
                    "validate_structural_regression, AND validate_method_kwargs BEFORE calling. "
                    "Returns persisted=False with reason on failure."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "full_main_py": {
                            "type": "string",
                            "description": "Complete main.py source. Must define every @skyvern.cached function from the original.",
                        },
                    },
                    "required": ["full_main_py"],
                },
            },
        ),
    ]


__all__ = ["all_persist_skills"]
