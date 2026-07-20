from __future__ import annotations

import structlog
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.sdk.workflow.models.validators import is_reserved_tag_key
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus

LOG = structlog.get_logger()

CREDENTIAL_FAILURES = "credential_failures"
ANY_FAILURE = "any_failure"
VALID_FALLBACK_TRIGGERS = frozenset({CREDENTIAL_FAILURES, ANY_FAILURE})
CREDENTIAL_FAILURE_CATEGORIES = frozenset({"AUTH_FAILURE", "CREDENTIAL_ERROR"})
CREDENTIAL_FALLBACK_RETRY_FLAG = "CREDENTIAL_FALLBACK_RETRY"
# Terminal statuses that any fallback trigger may retry from. timed_out counts because it is a
# failure elsewhere; credential_failures narrows further to failed/terminated in _trigger_matches.
RETRYABLE_STATUSES = frozenset({WorkflowRunStatus.failed, WorkflowRunStatus.terminated, WorkflowRunStatus.timed_out})


async def _retry_enabled_for_organization(organization_id: str, workflow_run: WorkflowRun) -> bool:
    """Gate the auto-retry rollout. Fails closed: a retry run costs credits and re-fires webhooks.

    distinct_id = organization_id (deliberately NOT run-level). backend.md's per-run
    randomization convention serves per-request experiments; retry behavior must be
    deterministic per customer, so a given org is fully in-or-out rather than retrying
    on a random subset of its runs. Do not "fix" this to workflow_run_id.
    """
    provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not provider:
        return False
    try:
        return bool(
            await provider.is_feature_enabled_cached(
                CREDENTIAL_FALLBACK_RETRY_FLAG,
                organization_id,
                properties={
                    "organization_id": organization_id,
                    "workflow_permanent_id": workflow_run.workflow_permanent_id,
                },
            )
        )
    except Exception:
        LOG.warning(
            "credential_fallback.flag_error",
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return False


def _failure_category_names(workflow_run: WorkflowRun) -> set[str]:
    categories = workflow_run.failure_category
    if not categories:
        categories = classify_from_failure_reason(workflow_run.failure_reason)
    return {
        category
        for entry in categories or []
        if isinstance(entry, dict) and isinstance(category := entry.get("category"), str)
    }


def _trigger_matches(workflow_run: WorkflowRun, trigger: str | None) -> bool:
    effective_trigger = trigger or CREDENTIAL_FAILURES
    if effective_trigger == ANY_FAILURE:
        return workflow_run.status in RETRYABLE_STATUSES
    if effective_trigger != CREDENTIAL_FAILURES:
        return False
    # A timeout is not a credential failure, so credential_failures keeps rejecting timed_out.
    if workflow_run.status not in {WorkflowRunStatus.failed, WorkflowRunStatus.terminated}:
        return False
    return bool(_failure_category_names(workflow_run) & CREDENTIAL_FAILURE_CATEGORIES)


async def _reload_user_run_metadata(workflow_run: WorkflowRun, organization_id: str) -> dict[str, str] | None:
    """Reload the failed run's user-writable tags so the retry keeps them (matches the manual
    retry path in agent_protocol). Reserved keys are dropped — they are re-derived per run."""
    try:
        grouped = await app.DATABASE.tags.get_active_grouped_tags_for_run(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to reload run metadata for fallback retry; continuing without it",
            workflow_run_id=workflow_run.workflow_run_id,
            exc_info=True,
        )
        return None
    if not grouped:
        return None
    return {key: value for key, value in grouped.items() if not is_reserved_tag_key(key)} or None


async def maybe_start_credential_fallback_retry(workflow_run: WorkflowRun, organization_id: str) -> str | None:
    try:
        return await _maybe_start_credential_fallback_retry(workflow_run, organization_id)
    except Exception:
        LOG.warning(
            "Failed to start credential fallback retry",
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None


async def _maybe_start_credential_fallback_retry(workflow_run: WorkflowRun, organization_id: str) -> str | None:
    # Scheduled from clean_up_workflow, which runs for every terminal status. Bail on non-failure
    # runs before the flag check and DB reads so the success path stays cheap. _trigger_matches
    # below still applies the finer per-parameter trigger (credential_failures vs any_failure).
    if workflow_run.status not in RETRYABLE_STATUSES:
        return None

    # copilot_session_id marks a Copilot-scoped test run: it supplies block_labels directly without
    # the debug block-run marker checked below, so guard on it too or a Copilot test would spawn a
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

    try:
        workflow = await app.WORKFLOW_SERVICE.get_workflow(
            workflow_id=workflow_run.workflow_id,
            organization_id=organization_id,
        )
    except WorkflowNotFound:
        return None
    if workflow is None:
        return None

    credential_parameters = [
        parameter
        for parameter in workflow.workflow_definition.parameters
        if isinstance(parameter, CredentialParameter) and bool(parameter.fallback_credential_ids)
    ]
    if not credential_parameters:
        return None

    existing_retry_run_id = await app.DATABASE.workflow_runs.get_workflow_run_retried_by(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )
    if existing_retry_run_id:
        return existing_retry_run_id

    prior_selections = await app.DATABASE.workflow_run_credential_selections.get_selections_for_run(
        workflow_run.workflow_run_id
    )

    next_attempt = (workflow_run.fallback_attempt or 0) + 1
    triggered_parameters = [
        parameter for parameter in credential_parameters if _trigger_matches(workflow_run, parameter.fallback_trigger)
    ]
    if not triggered_parameters:
        return None

    all_fallback_ids = list(
        dict.fromkeys(
            credential_id
            for parameter in triggered_parameters
            for credential_id in parameter.fallback_credential_ids or []
        )
    )
    existing_credentials = await app.DATABASE.credentials.get_credentials_by_ids(
        all_fallback_ids, organization_id=organization_id
    )
    found_credential_ids = {credential.credential_id for credential in existing_credentials}
    missing_credential_ids = [
        credential_id for credential_id in all_fallback_ids if credential_id not in found_credential_ids
    ]
    if missing_credential_ids:
        LOG.warning(
            "Skipping credential fallback candidates whose credentials no longer exist",
            workflow_run_id=workflow_run.workflow_run_id,
            missing_credential_ids=missing_credential_ids,
        )

    overrides: dict[str, str] = {}
    for parameter in triggered_parameters:
        fallback_ids = parameter.fallback_credential_ids or []
        prior = prior_selections.get(parameter.key)
        # A prior selection only marks fallback progress on a fallback retry; on an original
        # run it may come from rotation and coincidentally appear in the fallback list.
        base_index = fallback_ids.index(prior) + 1 if workflow_run.fallback_attempt and prior in fallback_ids else 0
        candidate = next(
            (
                credential_id
                for credential_id in fallback_ids[base_index:]
                if credential_id in found_credential_ids and credential_id != prior
            ),
            None,
        )
        if candidate is not None:
            overrides[parameter.key] = candidate
    if not overrides:
        return None

    advanced_parameter_keys = list(overrides)
    credential_parameter_keys = {
        parameter.key
        for parameter in workflow.workflow_definition.parameters
        if isinstance(parameter, CredentialParameter)
    }
    for parameter_key, credential_id in prior_selections.items():
        if parameter_key in credential_parameter_keys and parameter_key not in overrides:
            overrides[parameter_key] = credential_id

    parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
        workflow_run_id=workflow_run.workflow_run_id,
    )
    original_parameters = {
        workflow_parameter.key: workflow_run_parameter.value
        for workflow_parameter, workflow_run_parameter in parameter_tuples
    }
    retry_parameters = {**original_parameters, **overrides}

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
    # Shed every handle to the failed run's browser so the fallback credential gets a clean session
    # instead of reconnecting to the old account's cookies/authenticated state.
    workflow_request.browser_session_id = None
    workflow_request.browser_profile_id = None
    workflow_request.browser_address = None
    workflow_request.cdp_connect_headers = None
    workflow_request.extra_http_headers = app.AGENT_FUNCTION.strip_proxy_session_extra_http_headers(
        workflow_request.extra_http_headers
    )
    try:
        # A fresh context keeps run creation (and the background execution task it spawns,
        # which inherits contextvars) from attributing the retry to the failed run.
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
        "Started credential fallback retry",
        workflow_run_id=workflow_run.workflow_run_id,
        new_run_id=retry_run.workflow_run_id,
        fallback_attempt=next_attempt,
        advanced_parameter_keys=advanced_parameter_keys,
    )
    return retry_run.workflow_run_id
