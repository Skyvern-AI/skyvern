"""Terminal skills for the v3 agent loops.

Mid-run terminals: ``declare_success`` (Class A), ``give_up`` (Class B).
Post-run per-episode: ``declare_review_complete``, ``give_up_episode``,
``demote_class_a``.
Post-run global: ``declare_post_run_complete``, ``abandon_post_run``.

Each is a thin schema + a no-op handler. The agent loop turns a successful
terminal call into a :class:`Decision` via the schema-driven default builder.
The handlers exist only so the loop can wrap them in the standard skill
contract (timeout, error capture) and so the schema is discoverable through
the registry.
"""

from __future__ import annotations

from typing import Any

from skyvern.services.script_reviewer_v3.skills.base import Skill, SkillResult


def _ok_handler() -> Any:
    async def _h(args: dict[str, Any], context: Any) -> SkillResult:
        return SkillResult.ok(data=dict(args))

    return _h


_MIDRUN_ONLY = frozenset({"midrun"})
_POSTRUN_ONLY = frozenset({"postrun"})


def declare_success_skill() -> Skill:
    return Skill(
        name="declare_success",
        is_terminal=True,
        available_to=_MIDRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "declare_success",
            "description": (
                "Mid-run terminal. Call ONLY after a successful live_try_* mutation. "
                "Mark the failed action as resolved (Class A). The workflow continues from "
                "the post-mutation state. Include a 1-2 sentence investigation_summary "
                "explaining what fixed it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "investigation_summary": {"type": "string"},
                    "applied_fix_description": {
                        "type": "object",
                        "description": "Optional: structured description of the in-flight fix.",
                    },
                },
                "required": ["reason", "investigation_summary"],
            },
        },
    )


def give_up_skill() -> Skill:
    return Skill(
        name="give_up",
        is_terminal=True,
        available_to=_MIDRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "give_up",
            "description": (
                "Mid-run terminal. Call when you cannot apply an in-flight fix. The episode "
                "is marked Class B; the workflow falls through to agent fallback; post-run "
                "v3 will retrospectively analyze this episode."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    )


def declare_review_complete_skill() -> Skill:
    return Skill(
        name="declare_review_complete",
        is_terminal=True,
        available_to=_POSTRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "declare_review_complete",
            "description": (
                "Post-run per-episode terminal. The episode has been investigated and any "
                "remediation persisted via persist_block_edit / persist_script_rewrite. "
                "Marks the episode reviewed=True with reviewer_version='v3'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "episode_id": {"type": "string"},
                    "investigation_summary": {"type": "string"},
                },
                "required": ["episode_id", "investigation_summary"],
            },
        },
    )


def give_up_episode_skill() -> Skill:
    return Skill(
        name="give_up_episode",
        is_terminal=True,
        available_to=_POSTRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "give_up_episode",
            "description": (
                "Post-run per-episode terminal. You could not analyze this specific episode "
                "(missing context, ambiguous failure, etc.). Episode stays reviewed=False so "
                "future post-run passes may revisit it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "episode_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["episode_id", "reason"],
            },
        },
    )


def demote_class_a_skill() -> Skill:
    return Skill(
        name="demote_class_a",
        is_terminal=True,
        available_to=_POSTRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "demote_class_a",
            "description": (
                "Post-run per-episode terminal. The mid-run agent declared Class A on this "
                "episode but post-run evidence (screenshots, recording, final outcome) "
                "indicates the fix was a false positive. Episode is tagged "
                "reviewer_version='v3-post-run-demoted' for analytics. Does NOT roll back "
                "the persisted script version (rollback is a v3.1 consideration)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "episode_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["episode_id", "reason"],
            },
        },
    )


def declare_post_run_complete_skill() -> Skill:
    return Skill(
        name="declare_post_run_complete",
        is_terminal=True,
        available_to=_POSTRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "declare_post_run_complete",
            "description": (
                "Post-run global terminal. The entire post-run pass is complete. Per-episode "
                "decisions have already been emitted via declare_review_complete / "
                "give_up_episode / demote_class_a. Provide a 1-3 sentence "
                "investigation_summary covering the run's overall failure modes and what was "
                "fixed (if anything)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "investigation_summary": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    )


def abandon_post_run_skill() -> Skill:
    return Skill(
        name="abandon_post_run",
        is_terminal=True,
        available_to=_POSTRUN_ONLY,
        handler=_ok_handler(),
        schema={
            "name": "abandon_post_run",
            "description": (
                "Post-run global terminal. Give up on the whole pass (e.g., fundamental "
                "context-loading failure, unreadable run state). Any episodes not "
                "individually addressed stay reviewed=False."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    )


def all_terminal_skills() -> list[Skill]:
    return [
        declare_success_skill(),
        give_up_skill(),
        declare_review_complete_skill(),
        give_up_episode_skill(),
        demote_class_a_skill(),
        declare_post_run_complete_skill(),
        abandon_post_run_skill(),
    ]


__all__ = ["all_terminal_skills"]
