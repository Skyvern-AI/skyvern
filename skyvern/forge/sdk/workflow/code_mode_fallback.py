from __future__ import annotations

import structlog
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.validators import is_reserved_tag_key
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

LOG = structlog.get_logger()

CODE_MODE_FALLBACK_RETRY_FLAG = "CODE_MODE_FALLBACK_RETRY"
CODE_RUN_WITH = "code"
AGENT_RUN_WITH = "agent"
# A code-mode run that failed/terminated retries once as a pure-agent run. timed_out is excluded:
# re-running the full (slower) agent path on an already-long run is rarely what the caller wants.
#
# Double-apply risk (accepted, flag-gated): the retry is a fresh full-workflow run, so a run that
# submitted an application and then failed on a later block would submit again. A run that actually
# completed never reaches here (only failed/terminated retry), which covers the common case; the
# submitted-then-errored edge is real, so the feature stays off by default (CODE_MODE_FALLBACK_RETRY)
# and is enabled per-org only with explicit owner sign-off that the target tolerates a possible
# duplicate submission.
RETRYABLE_STATUSES = frozenset({WorkflowRunStatus.failed, WorkflowRunStatus.terminated})


async def _retry_enabled_for_organization(organization_id: str, workflow_run: WorkflowRun) -> bool:
    """Gate the auto-retry rollout. Fails closed: a retry run costs credits and re-fires webhooks.

    distinct_id = organization_id (deliberately NOT run-level), so an org is fully in-or-out
    rather than retrying on a random subset of its runs. Mirrors credential_fallback.
    """
    provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not provider:
        return False
    try:
        return bool(
            await provider.is_feature_enabled_cached(
                CODE_MODE_FALLBACK_RETRY_FLAG,
                organization_id,
                properties={
                    "organization_id": organization_id,
                    "workflow_permanent_id": workflow_run.workflow_permanent_id,
                },
            )
        )
    except Exception:
        LOG.warning(
            "code_mode_fallback.flag_error",
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return False


def _trigger_matches(workflow_run: WorkflowRun) -> bool:
    """A code-mode run in a retryable terminal state is eligible; anything else is not."""
    if workflow_run.status not in RETRYABLE_STATUSES:
        return False
    return (workflow_run.run_with or AGENT_RUN_WITH) == CODE_RUN_WITH


async def _reload_user_run_metadata(workflow_run: WorkflowRun, organization_id: str) -> dict[str, str] | None:
    """Reload the failed run's user-writable tags so the retry keeps them. Reserved keys are dropped —
    they are re-derived per run. Mirrors credential_fallback / the manual retry path."""
    try:
        grouped = await app.DATABASE.tags.get_active_grouped_tags_for_run(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to reload run metadata for code-mode fallback retry; continuing without it",
            workflow_run_id=workflow_run.workflow_run_id,
            exc_info=True,
        )
        return None
    if not grouped:
        return None
    return {key: value for key, value in grouped.items() if not is_reserved_tag_key(key)} or None


async def maybe_start_code_mode_fallback_retry(workflow_run: WorkflowRun, organization_id: str) -> str | None:
    try:
        return await _maybe_start_code_mode_fallback_retry(workflow_run, organization_id)
    except Exception:
        LOG.warning(
            "Failed to start code-mode fallback retry",
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None


async def _maybe_start_code_mode_fallback_retry(workflow_run: WorkflowRun, organization_id: str) -> str | None:
    # Scheduled from clean_up_workflow, which runs for every terminal status. Bail on ineligible runs
    # before the flag check and DB reads so the success path stays cheap.
    if not _trigger_matches(workflow_run):
        return None

    # Never retry a retry: an agent attempt that fails is terminal, and a run that is already a
    # fallback retry (credential or code) must not spawn another. This also bounds the chain.
    if workflow_run.retried_from_workflow_run_id or workflow_run.fallback_attempt:
        return None

    # Block-run / copilot-test / debug runs supply block_labels directly and must not spawn a
    # detached full-workflow run (real credits, real side effects) outside the test.
    if workflow_run.parent_workflow_run_id or workflow_run.debug_session_id or workflow_run.copilot_session_id:
        return None

    if not await _retry_enabled_for_organization(organization_id, workflow_run):
        return None

    is_block_scoped_run = await app.AGENT_FUNCTION.is_block_scoped_workflow_run(workflow_run)
    if not is_block_scoped_run:
        is_block_scoped_run = await app.DATABASE.debug.has_block_run_for_workflow_run(
            organization_id=organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
        )
    if is_block_scoped_run:
        return None

    # Idempotency: the retried_from_workflow_run_id unique index allows one retry per run, shared
    # with credential_fallback — so if credential fallback already claimed the slot, no-op.
    existing_retry_run_id = await app.DATABASE.workflow_runs.get_workflow_run_retried_by(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )
    if existing_retry_run_id:
        return existing_retry_run_id

    try:
        workflow = await app.WORKFLOW_SERVICE.get_workflow(
            workflow_id=workflow_run.workflow_id,
            organization_id=organization_id,
        )
    except WorkflowNotFound:
        return None
    if workflow is None:
        return None

    parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
        workflow_run_id=workflow_run.workflow_run_id,
    )
    retry_parameters = {
        workflow_parameter.key: workflow_run_parameter.value
        for workflow_parameter, workflow_run_parameter in parameter_tuples
    }

    organization = await app.DATABASE.organizations.get_organization(organization_id)
    if organization is None:
        return None

    from skyvern.services.workflow_service import run_workflow, workflow_request_body_from_existing_run

    run_metadata = await _reload_user_run_metadata(workflow_run, organization_id)
    workflow_request = workflow_request_body_from_existing_run(
        workflow_run=workflow_run,
        parameters=retry_parameters,
        run_metadata=run_metadata,
    )
    # The whole point of the fallback: the retry runs as a pure agent, never code.
    workflow_request.run_with = AGENT_RUN_WITH
    # Shed every handle to the failed run's browser so the agent starts from a clean session instead
    # of reconnecting to the code run's half-filled page / authenticated state.
    workflow_request.browser_session_id = None
    workflow_request.browser_profile_id = None
    workflow_request.browser_address = None
    workflow_request.cdp_connect_headers = None
    workflow_request.extra_http_headers = app.AGENT_FUNCTION.strip_proxy_session_extra_http_headers(
        workflow_request.extra_http_headers
    )

    next_attempt = (workflow_run.fallback_attempt or 0) + 1
    try:
        # A fresh context keeps run creation (and the background execution task it spawns, which
        # inherits contextvars) from attributing the retry to the failed run.
        with skyvern_context.scoped(SkyvernContext()):
            retry_run = await run_workflow(
                workflow_id=workflow_run.workflow_permanent_id,
                organization=organization,
                workflow_request=workflow_request,
                template=False,
                version=workflow.version,
                trigger_type=workflow_run.trigger_type,
                workflow_schedule_id=workflow_run.workflow_schedule_id,
                retried_from_workflow_run_id=workflow_run.workflow_run_id,
                fallback_attempt=next_attempt,
                ignore_inherited_workflow_system_prompt=workflow_run.ignore_inherited_workflow_system_prompt,
            )
    except IntegrityError as exc:
        error_text = str(exc.orig) if exc.orig is not None else str(exc)
        if (
            "ix_workflow_runs_retried_from_workflow_run_id" not in error_text
            and "retried_from_workflow_run_id" not in error_text
        ):
            raise
        existing_retry_run_id = await app.DATABASE.workflow_runs.get_workflow_run_retried_by(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
        )
        if not existing_retry_run_id:
            raise
        return existing_retry_run_id

    LOG.info(
        "Started code-mode fallback retry",
        workflow_run_id=workflow_run.workflow_run_id,
        new_run_id=retry_run.workflow_run_id,
        fallback_attempt=next_attempt,
    )
    return retry_run.workflow_run_id
