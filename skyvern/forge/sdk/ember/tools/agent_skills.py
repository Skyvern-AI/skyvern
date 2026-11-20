from __future__ import annotations

from typing import Literal
from uuid import uuid4

import yaml
from agents import function_tool
from agents.tool_context import ToolContext
from pydantic import JsonValue

from skyvern.forge import app
from skyvern.forge.agent_functions import EmberSkillWriteResult, SkillWriteConfirm
from skyvern.forge.sdk.agents.skills import (
    DELETE_ORG_SKILL_TOOL_NAME,
    SAVE_ORG_SKILL_TOOL_NAME,
    SkillWriteReason,
)
from skyvern.forge.sdk.ember.ask_user import QuestionInteraction, SkillWriteReview, wait_for_interaction
from skyvern.forge.sdk.ember.context import EmberContext
from skyvern.forge.sdk.ember.secret_scrub import registered_scrub_values

from .errors import ember_tool_failure, finish_tool_call
from .guardrails import _authority_tool_error

SkillScope = Literal["organization", "workflow"]

NEXT_MESSAGE_NOTE = "The skill list in this chat shows this change from the next message on."
SECRET_REFUSAL = "Nothing was changed: the skill text contains a value this chat holds as a secret."
SECRET_NAME_REFUSAL = "Nothing was changed: the skill name contains a value this chat holds as a secret."
STEERED_REFUSAL = (
    "Nothing was changed: someone other than the person who started this turn sent a message into it or answered "
    "its question, and a workflow skill change is recorded as that person's, so it needs their own request in a new "
    "message."
)

SAVE_ORG_SKILL_DESCRIPTION = (
    "Save one of this organization's skills as its next version, written by the signed-in user. The SKILL.md is\n"
    "`name` and `description` as frontmatter, then `body`. `scope` `organization` offers the skill on every\n"
    "workflow; `workflow` offers it on this workflow only. Saving a name that exists replaces its SKILL.md and keeps\n"
    "its other files; earlier versions stay in its history. `name` is 1-64 lowercase letters, digits and single\n"
    "hyphens; `description` is one line of at most 1024 characters, shown in the skill list.\n"
    "An `organization` save waits for the user to approve it on a card, which shows the SKILL.md.\n"
    "Returns `version`, `previous_version` (null when no version was live) and the stored `skill_md`.\n"
    "A refusal for one of these causes carries `data.reason`: `invalid`, `no_workflow`, `denied`, `conflict`,\n"
    "`secret`, or `declined` when the user did not approve the card."
)
DELETE_ORG_SKILL_DESCRIPTION = (
    "Delete one of this organization's skills with every version of it. `scope` `organization` deletes the\n"
    "org-wide skill; `workflow` deletes this workflow's skill of that name. An `organization` delete waits for the\n"
    "user to approve it on a card. A refusal for one of these causes carries `data.reason`: `no_workflow`,\n"
    "`denied`, `secret` when the name contains a value this chat holds as a secret, `declined` when the user did not\n"
    "approve the card, `folder` for a skill kept in the organization's skills folder, which cannot be deleted here,\n"
    "or `unknown_skill` with `names`, the skills of the requested scope that can be deleted here."
)


def build_skill_md(name: str, description: str, body: str) -> str:
    frontmatter = yaml.safe_dump(
        {"name": name, "description": description}, allow_unicode=True, sort_keys=False, width=2**31 - 1
    )
    return f"---\n{frontmatter}---\n{body}"


def _refusal(error: str, reason: SkillWriteReason | None, names: list[str] | None = None) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"ok": False, "error": error}
    if reason is not None:
        result["data"] = {"reason": reason} if names is None else {"reason": reason, "names": list(names)}
    return result


def _write_refusal(ctx: EmberContext, written: EmberSkillWriteResult, scope: SkillScope) -> dict[str, JsonValue] | None:
    if written.error is None:
        return None
    names = ctx.skill_session.stored_names(scope) if written.reason == "unknown_skill" else None
    return _refusal(written.error, written.reason, names)


def _scope_workflow(ctx: EmberContext, scope: SkillScope) -> str | None:
    return ctx.workflow_permanent_id if scope == "workflow" else None


def _card_for_organization_scope(
    ctx: ToolContext[EmberContext], scope: SkillScope, review: SkillWriteReview
) -> SkillWriteConfirm | None:
    # An org-wide skill reaches every later chat and unattended agent step, so the person approves it first.
    if scope != "organization":
        return None

    async def confirm(live_version: int | None, from_runs: bool) -> bool:
        recorded = await wait_for_interaction(
            ctx.context,
            QuestionInteraction(
                interaction_id=uuid4().hex,
                turn_id=ctx.context.turn_id,
                tool_call_id=ctx.tool_call_id,
                parts=[],
                skill_write_review=review.model_copy(update={"live_version": live_version, "from_runs": from_runs}),
            ),
        )
        decision = recorded.account_group_decision
        return decision is not None and decision.approved

    return confirm


def _steered_workflow_write(ctx: EmberContext, scope: SkillScope) -> bool:
    # A workflow write takes no card, so the starter's own request is its only authorization; an organization write
    # waits on the starter's approval card.
    return scope == "workflow" and ctx.steered_by_another


def _holds_secret(ctx: EmberContext, *texts: str) -> bool:
    return any(secret in text for secret in registered_scrub_values(ctx) for text in texts)


@function_tool(
    failure_error_function=ember_tool_failure,
    name_override=SAVE_ORG_SKILL_TOOL_NAME,
    description_override=SAVE_ORG_SKILL_DESCRIPTION,
)
async def save_org_skill_tool(
    ctx: ToolContext[EmberContext], name: str, scope: SkillScope, description: str, body: str
) -> str:
    ember_ctx = ctx.context
    arguments: dict[str, str] = {"name": name, "scope": scope}
    if _holds_secret(ember_ctx, name, description, body):
        return finish_tool_call(
            ember_ctx, SAVE_ORG_SKILL_TOOL_NAME, {"scope": scope}, _refusal(SECRET_REFUSAL, "secret")
        )
    if _steered_workflow_write(ember_ctx, scope):
        return finish_tool_call(ember_ctx, SAVE_ORG_SKILL_TOOL_NAME, arguments, _refusal(STEERED_REFUSAL, "denied"))
    authority_error = _authority_tool_error(ember_ctx, SAVE_ORG_SKILL_TOOL_NAME)
    if authority_error:
        return finish_tool_call(ember_ctx, SAVE_ORG_SKILL_TOOL_NAME, arguments, {"ok": False, "error": authority_error})
    skill_md = build_skill_md(name, description, body)
    saved = await app.AGENT_FUNCTION.save_ember_skill(
        organization_id=ember_ctx.organization_id,
        workflow_permanent_id=_scope_workflow(ember_ctx, scope),
        name=name,
        skill_md=skill_md,
        ember_chat_id=ember_ctx.workflow_copilot_chat_id,
        confirm=_card_for_organization_scope(
            ctx, scope, SkillWriteReview(action="save", name=name, live_version=None, skill_md=skill_md)
        ),
    )
    result = _write_refusal(ember_ctx, saved, scope) or {
        "ok": True,
        "data": {
            "name": name,
            "scope": scope,
            "version": saved.version,
            "previous_version": saved.previous_version,
            "skill_md": saved.skill_md,
            "note": NEXT_MESSAGE_NOTE,
        },
    }
    return finish_tool_call(ember_ctx, SAVE_ORG_SKILL_TOOL_NAME, arguments, result)


@function_tool(
    failure_error_function=ember_tool_failure,
    name_override=DELETE_ORG_SKILL_TOOL_NAME,
    description_override=DELETE_ORG_SKILL_DESCRIPTION,
)
async def delete_org_skill_tool(ctx: ToolContext[EmberContext], name: str, scope: SkillScope) -> str:
    ember_ctx = ctx.context
    arguments: dict[str, str] = {"name": name, "scope": scope}
    # The call's frame and span already left the name out; refusing here keeps it out of the result, logs and audit.
    if _holds_secret(ember_ctx, name):
        return finish_tool_call(
            ember_ctx, DELETE_ORG_SKILL_TOOL_NAME, {"scope": scope}, _refusal(SECRET_NAME_REFUSAL, "secret")
        )
    if _steered_workflow_write(ember_ctx, scope):
        return finish_tool_call(ember_ctx, DELETE_ORG_SKILL_TOOL_NAME, arguments, _refusal(STEERED_REFUSAL, "denied"))
    authority_error = _authority_tool_error(ember_ctx, DELETE_ORG_SKILL_TOOL_NAME)
    if authority_error:
        return finish_tool_call(
            ember_ctx, DELETE_ORG_SKILL_TOOL_NAME, arguments, {"ok": False, "error": authority_error}
        )
    deleted = await app.AGENT_FUNCTION.delete_ember_skill(
        organization_id=ember_ctx.organization_id,
        workflow_permanent_id=_scope_workflow(ember_ctx, scope),
        name=name,
        ember_chat_id=ember_ctx.workflow_copilot_chat_id,
        confirm=_card_for_organization_scope(
            ctx, scope, SkillWriteReview(action="delete", name=name, live_version=None)
        ),
    )
    result = _write_refusal(ember_ctx, deleted, scope) or {
        "ok": True,
        "data": {"name": name, "scope": scope, "note": NEXT_MESSAGE_NOTE},
    }
    return finish_tool_call(ember_ctx, DELETE_ORG_SKILL_TOOL_NAME, arguments, result)
