"""The model's own work plan: replaced whole, stored as sent, projected back, never read to decide
anything."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge import app
from skyvern.utils.strings import escape_code_fences

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

MAX_ITEMS = 40
MAX_ITEM_CHARS = 400


class WorkPlanArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[str] = Field(default_factory=list)


def work_plan_size_error(items: Sequence[str]) -> str | None:
    if len(items) > MAX_ITEMS:
        return f"A work plan holds at most {MAX_ITEMS} items; resend with {MAX_ITEMS} or fewer."
    if any(len(item) > MAX_ITEM_CHARS for item in items):
        return f"A work plan item holds at most {MAX_ITEM_CHARS} characters; resend with shorter items."
    return None


def _flatten(item: str) -> str:
    return item.replace("\r", " ").replace("\n", " ")


def work_plan_prompt(items: Sequence[str] | None) -> str:
    """Each item is one bullet in a prompt that also carries fenced sections, so fences are escaped
    and newlines flattened for the prompt only; the stored text keeps the model's own layout."""
    if not items:
        return ""
    lines = "\n".join(f"- {_flatten(escape_code_fences(item))}" for item in items)
    # This header is measured, not prose: an imperative one took authoring from 12/20 to 0/20 in replay.
    # Re-measure with the arms in cloud_docs/workflow-copilot/architecture/offline-replay.md before editing.
    return "\n\nYOUR WORK PLAN (your own notes, carried over from earlier in this chat):\n" + lines


async def set_work_plan(ctx: CopilotContext, arguments: WorkPlanArguments) -> dict[str, Any]:
    error = work_plan_size_error(arguments.items)
    if error is not None:
        return {"ok": False, "error": error, "items": list(ctx.work_plan or [])}
    items = list(arguments.items)
    if ctx.workflow_copilot_chat_id:
        # Assigned only after the row is written, so a failed write cannot leave the turn
        # projecting a plan a reload would not restore.
        await app.DATABASE.workflow_params.update_workflow_copilot_chat(
            organization_id=ctx.organization_id,
            workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
            work_plan=items,
        )
    ctx.work_plan = items
    return {"ok": True, "items": items}


async def hydrate_work_plan(ctx: CopilotContext) -> None:
    """Leaves ``work_plan`` at None whenever no chat row answered — a turn that never got here, or one
    whose row is missing — so the terminal frame sends None and the client keeps the plan it renders."""
    if ctx.work_plan is not None:
        return
    if not ctx.workflow_copilot_chat_id:
        return
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=ctx.organization_id,
        workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
    )
    if chat is not None:
        ctx.work_plan = list(chat.work_plan)
