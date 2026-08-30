"""Cross-block context for a Task V3 block: the framing sentences appended to a block's goal.

Every "block N+1 needs to know X about the workflow" case is meant to become a typed input here,
not another ad-hoc sentence patched onto the goal string in ``ForgeAgent._execute_task_v3``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from skyvern.forge.sdk.schemas.tasks import Task, TaskType

if TYPE_CHECKING:
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
    from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock


def render_block_context(
    task: Task,
    task_block: BaseTaskBlock | None,
    workflow_run_context: WorkflowRunContext | None,
    *,
    page_free_validation: bool = False,
) -> tuple[str, str]:
    """Return ``(framing, section)`` for a block task; both are ``""`` for a bare task.

    ``framing`` is the block-kind guidance (mid-flow / page-free validation / validation / action).
    ``section`` is the rendered workflow-context data section (position, previous-block handoff);
    it renders only fields that are set, so it is empty when there is nothing to say.
    """
    del workflow_run_context
    pieces: list[str] = []
    if task_block is not None and not page_free_validation:
        # A block resumes mid-workflow: an earlier block may already have satisfied this one's
        # criterion (the step engine's per-step goal check gives it this for free).
        pieces.append(
            "This task is one block of a larger workflow and starts mid-flow. First read "
            "the full page text (get_html) and check whether the completion criterion is ALREADY "
            "satisfied by the page's settled, loaded content - a loading indicator, skeleton, or "
            "empty container does NOT satisfy a criterion about visible content."
            + (
                " When the goal names an action (open/click/submit), perform it unless the page "
                "already shows that action's RESULT."
                if task.task_type != TaskType.validation
                else ""
            )
            + " If the criterion is genuinely satisfied, finish with status=completed "
            "immediately without acting. Stay "
            "within this block's goal: never sign out, navigate away from the current flow, or undo "
            "prior progress unless the goal explicitly asks for it."
        )
    if page_free_validation:
        # This mode judges only durable inputs/prior outputs; any perception instruction would
        # contradict it, so it replaces (not extends) the read-the-page framing above.
        pieces.append(
            "This is a page-free assessment task: judge ONLY from the information already "
            "provided above and prior workflow context. Do not call observe or get_html, and do not "
            "modify page state. Evaluate the completion and termination criteria and finish with the "
            "matching status."
        )
    elif task_block is not None and task.task_type == TaskType.validation:
        # ValidationBlock tasks judge, not act; without this the loop can treat the criteria
        # above as something to accomplish by interacting with the page.
        pieces.append(
            "This is an assessment task: do not modify page state. Evaluate the completion "
            "and termination criteria above and finish with the matching status. Ground the judgment "
            "in the page's actual content: read the full page text (get_html) before concluding, and "
            "never finish with status=terminated on element summaries alone — absence must be "
            "confirmed against the full text."
        )
    elif task_block is not None and task.task_type == TaskType.action:
        pieces.append("This is a single, focused action: perform it and finish.")
    return "\n\n".join(pieces), ""
