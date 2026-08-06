from __future__ import annotations

import structlog

from skyvern.constants import DEFAULT_WORKFLOW_TITLES
from skyvern.forge import app
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.streaming_adapter import emit_title_update
from skyvern.forge.sdk.copilot.turn_intent import (
    MUTATING_CLASSIFIER_MODES,
    TurnIntent,
    TurnIntentClassifierResult,
    sanitize_workflow_title_candidate,
)
from skyvern.forge.sdk.copilot.workflow_yaml import with_workflow_yaml_title, workflow_yaml_title
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

LOG = structlog.get_logger()

# Naming follows authoring: the modes that may mutate the workflow are the ones
# whose stated goal is worth a name.
NAMING_MODES = MUTATING_CLASSIFIER_MODES


def derive_agent_title(classifier_result: TurnIntentClassifierResult | None) -> str | None:
    """The classifier's title for this turn, or nothing.

    Deliberately never falls back to raw user text: the only filter available here is the
    same pattern scrub the policy layer already applies, so a credential shape it misses
    would land in a durably stored, org-visible title.
    """
    classification = classifier_result.classification if classifier_result is not None else None
    return sanitize_workflow_title_candidate(classification.workflow_title if classification else None)


def _submitted_title_is_user_chosen(workflow_yaml: str | None) -> bool:
    title = workflow_yaml_title(workflow_yaml)
    return bool(title) and title not in DEFAULT_WORKFLOW_TITLES


async def maybe_name_agent(
    ctx: CopilotContext,
    *,
    classifier_result: TurnIntentClassifierResult | None,
) -> str | None:
    """Name a still-unnamed agent from the turn's intent; returns the title if it renamed.

    Fails open in every direction: naming is a courtesy, never a precondition for building.
    """
    intent = ctx.turn_intent
    if not isinstance(intent, TurnIntent) or intent.mode not in NAMING_MODES:
        return None
    # An unsaved rename in the editor outranks anything derived here; canonical can still
    # read as default while the user has already typed a name they have not saved.
    if _submitted_title_is_user_chosen(ctx.workflow_yaml):
        return None
    # RequestPolicy owns ambiguous credential semantics and states them in two places:
    # raw_secret_detected is the pattern verdict, raw_secret_handling the classifier's.
    # A title outlives the turn, so either one withholds naming.
    policy = ctx.request_policy
    if isinstance(policy, RequestPolicy) and (policy.raw_secret_detected or policy.raw_secret_handling != "none"):
        return None

    classification = classifier_result.classification if classifier_result is not None else None
    title = derive_agent_title(classifier_result)
    if not title:
        return None

    try:
        renamed = await app.DATABASE.workflows.rename_workflow_if_still_default(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            title=title,
        )
    except Exception:
        LOG.warning(
            "copilot_agent_naming_persist_failed",
            workflow_permanent_id=ctx.workflow_permanent_id,
            exc_info=True,
        )
        return None

    if not renamed:
        LOG.info(
            "copilot_agent_naming_skipped",
            workflow_permanent_id=ctx.workflow_permanent_id,
            turn_intent_mode=intent.mode.value,
        )
        return None

    LOG.info(
        "copilot_agent_named",
        workflow_permanent_id=ctx.workflow_permanent_id,
        turn_intent_mode=intent.mode.value,
        from_classifier=bool(classification and classification.workflow_title),
    )
    return title


async def name_agent_and_publish(
    ctx: CopilotContext,
    chat_request: WorkflowCopilotChatRequest,
    prompt_workflow_yaml: str,
    *,
    classifier_result: TurnIntentClassifierResult | None,
) -> tuple[str | None, str]:
    """Name the agent, show the model the name, and tell the client.

    Returns ``(title, prompt YAML)``; the caller must rebind its local to the returned YAML
    so the prompt carries the derived title.
    """
    title = await maybe_name_agent(ctx, classifier_result=classifier_result)
    if not title:
        return None, prompt_workflow_yaml

    ctx.workflow_yaml = with_workflow_yaml_title(ctx.workflow_yaml, title)
    chat_request.workflow_yaml = with_workflow_yaml_title(chat_request.workflow_yaml, title)
    try:
        await emit_title_update(ctx.stream, ctx, title)
    except Exception as emit_err:
        LOG.warning("copilot_title_update_emit_failed", error=str(emit_err))
    return title, with_workflow_yaml_title(prompt_workflow_yaml, title)
