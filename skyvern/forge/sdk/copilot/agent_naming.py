from __future__ import annotations

import asyncio
import unicodedata

import structlog

from skyvern.constants import DEFAULT_WORKFLOW_TITLES
from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_secondary_llm_api_handler
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.streaming_adapter import emit_title_update
from skyvern.forge.sdk.copilot.workflow_yaml import workflow_yaml_title

LOG = structlog.get_logger()

_TITLE_MAX_CHARS = 60
_TITLE_TIMEOUT_SECONDS = 8.0
_TITLE_PROMPT_NAME = "copilot-agent-title"
_TITLE_DISALLOWED_UNICODE_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
_NAMING_TASKS: set[asyncio.Task[str | None]] = set()


def sanitize_workflow_title_candidate(value: str | None) -> str | None:
    """Single sink for every naming candidate; a default title is rejected so "still unnamed?" stays value-based."""
    if not value:
        return None
    text = unicodedata.normalize("NFKC", value)
    text = "".join(" " if unicodedata.category(char) in _TITLE_DISALLOWED_UNICODE_CATEGORIES else char for char in text)
    text = " ".join(text.split()).strip().strip("\"'").strip()
    # A title is durable and org-visible, so a candidate carrying a secret withholds the whole name.
    if redact_raw_secrets_for_prompt(text) != text:
        return None
    # Declining beats trimming: a cut name still persists, and a name the user stated would lose to
    # its own truncation because the row is then no longer a default.
    if len(text) > _TITLE_MAX_CHARS:
        return None
    if not text or text in DEFAULT_WORKFLOW_TITLES:
        return None
    return text


async def derive_agent_title(organization_id: str, canonical_user_message: str) -> str | None:
    # No fallback to the user's own words: the pattern scrub is the only filter here, so a
    # credential shape it misses would land in a durable, org-visible title.
    prompt = prompt_engine.load_prompt(
        template=_TITLE_PROMPT_NAME, request=redact_raw_secrets_for_prompt(canonical_user_message)
    )
    handler = get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)
    async with asyncio.timeout(_TITLE_TIMEOUT_SECONDS):
        response = await handler(prompt=prompt, prompt_name=_TITLE_PROMPT_NAME, organization_id=organization_id)
    generated = response.get("title") if isinstance(response, dict) else None
    return sanitize_workflow_title_candidate(generated if isinstance(generated, str) else None)


def _naming_withheld_reason(ctx: CopilotContext, policy: RequestPolicy, product_action: str | None) -> str | None:
    # A product action's message is a server receipt, not text the user wrote.
    if product_action is not None:
        return "product_action"
    # An eval turn drives a fixture workflow whose title the case owns.
    if ctx.eval_mode is not None:
        return "eval_mode"
    # An unsaved editor rename outranks anything derived here.
    submitted_title = workflow_yaml_title(ctx.workflow_yaml)
    if submitted_title and submitted_title not in DEFAULT_WORKFLOW_TITLES:
        return "named"
    # A title outlives the turn, so either secret verdict withholds it. An unavailable safety
    # screen sets handling to "block", which also covers its placeholder canonical message.
    if policy.raw_secret_detected or policy.raw_secret_handling != "none":
        return "secret"
    if not policy.canonical_user_message.strip():
        return "empty_message"
    return None


async def name_agent(ctx: CopilotContext, policy: RequestPolicy) -> str | None:
    """Name a still-default agent from the turn's safe message; returns the title written and never raises."""
    try:
        # The client can hold the placeholder long after the row is named, so without this every later
        # turn pays a metered title call whose write is already guaranteed to be a no-op.
        try:
            persisted = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=ctx.workflow_permanent_id,
                organization_id=ctx.organization_id,
            )
        except WorkflowNotFound:
            persisted = None
        if persisted is not None and persisted.title not in DEFAULT_WORKFLOW_TITLES:
            LOG.info("copilot_agent_naming_skipped", workflow_permanent_id=ctx.workflow_permanent_id, reason="named")
            return None
        try:
            title = await derive_agent_title(ctx.organization_id, policy.canonical_user_message)
        except TimeoutError:
            LOG.info("copilot_agent_naming_skipped", workflow_permanent_id=ctx.workflow_permanent_id, reason="timeout")
            return None
        except Exception as exc:
            # A provider traceback can quote the prompt, which carries the in-URL secret case.
            LOG.warning(
                "copilot_agent_title_derivation_failed",
                workflow_permanent_id=ctx.workflow_permanent_id,
                exception_type=type(exc).__name__,
            )
            return None
        if not title:
            LOG.info("copilot_agent_naming_skipped", workflow_permanent_id=ctx.workflow_permanent_id, reason="no_title")
            return None
        renamed = await app.DATABASE.workflows.rename_workflow_if_still_default(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            title=title,
        )
        if not renamed:
            LOG.info(
                "copilot_agent_naming_skipped",
                workflow_permanent_id=ctx.workflow_permanent_id,
                reason="not_default_or_superseded",
            )
            return None
    except Exception:
        LOG.warning("copilot_agent_naming_failed", workflow_permanent_id=ctx.workflow_permanent_id, exc_info=True)
        return None
    # The row is renamed from here on, so the title is returned even when the client never hears of it.
    ctx.agent_named_title = title
    stream_not_closed = False
    if ctx.stream is not None:
        try:
            stream_not_closed = await emit_title_update(ctx.stream, ctx, title)
        except Exception:
            LOG.warning(
                "copilot_agent_title_update_failed", workflow_permanent_id=ctx.workflow_permanent_id, exc_info=True
            )
    LOG.info(
        "copilot_agent_named", workflow_permanent_id=ctx.workflow_permanent_id, stream_not_closed=stream_not_closed
    )
    return title


def schedule_agent_naming(ctx: CopilotContext, policy: RequestPolicy, product_action: str | None) -> None:
    try:
        reason = _naming_withheld_reason(ctx, policy, product_action)
        if reason is not None:
            LOG.info("copilot_agent_naming_skipped", workflow_permanent_id=ctx.workflow_permanent_id, reason=reason)
            return
        # Deliberately not cancelled at turn end: the persisted title is still worth landing after
        # the stream closes, and a reload shows it.
        task = asyncio.create_task(name_agent(ctx, policy))
        _NAMING_TASKS.add(task)
        task.add_done_callback(_NAMING_TASKS.discard)
    except Exception:
        LOG.warning(
            "copilot_agent_naming_launch_failed", workflow_permanent_id=ctx.workflow_permanent_id, exc_info=True
        )
