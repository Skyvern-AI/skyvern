import typing as t
from collections.abc import Awaitable, Callable
from http import HTTPStatus

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import BrowserSeedSource, WorkflowRunTriggerType
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.exceptions import InvalidTemplateWorkflowPermanentId
from skyvern.forge.sdk.workflow.models.tags import TagWriteContext
from skyvern.forge.sdk.workflow.models.validators import drop_reserved_tag_values
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody, WorkflowRun
from skyvern.schemas.runs import (
    BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY,
    BROWSER_SESSION_SERVER_ASSIGNED_CONTEXT_KEY,
    RunStatus,
    RunType,
    WorkflowRunRequest,
    WorkflowRunResponse,
    read_browser_type,
)
from skyvern.utils.contained_effects import contained_effect
from skyvern.webeye.real_browser_manager import (
    SelectedBrowserTypeUnsupportedError,
    ensure_runtime_supports_browser_type,
)

LOG = structlog.get_logger(__name__)
# Reconstruction re-materializes the server's own persisted row (browser_type may coexist with a
# server-generated session/address), so mark both as server-assigned to excuse the attach-conflict
# validator — caller ingress, which never sets this context, is still rejected.
_SERVER_ASSIGNED_BROWSER_ATTACHMENT_CONTEXT = {
    BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY: True,
    BROWSER_SESSION_SERVER_ASSIGNED_CONTEXT_KEY: True,
}


def workflow_request_body_from_existing_run(
    workflow_run: WorkflowRun,
    parameters: dict[str, t.Any] | None = None,
    run_metadata: dict[str, str] | None = None,
) -> WorkflowRequestBody:
    return WorkflowRequestBody.model_validate(
        {
            "data": parameters,
            "proxy_location": workflow_run.proxy_location,
            "webhook_callback_url": workflow_run.webhook_callback_url,
            "totp_verification_url": workflow_run.totp_verification_url,
            "totp_identifier": workflow_run.totp_identifier,
            # A fresh run created under FORCE_BROWSER_SESSION carries a generated PBS on browser_session_id;
            # replaying it into the retry alongside start_fresh_browser trips the mutually-exclusive validator.
            # Fresh intent wins — omit the session id so the retry re-resolves a fresh browser.
            "browser_session_id": None if workflow_run.start_fresh_browser else workflow_run.browser_session_id,
            # A retry re-resolves the seed instead of pinning a runtime-stamped profile (the old bug: a
            # credential/own-memory profile stamped at setup would ride into the retry as a fake override,
            # suppressing re-resolution and write-back). Only a genuine per-run override propagates. Legacy
            # rows (seed source unknown) keep today's propagate behavior so pre-S retries don't regress.
            "browser_profile_id": (
                workflow_run.browser_profile_id
                if workflow_run.browser_seed_source in (None, BrowserSeedSource.override)
                else None
            ),
            # The original request's fresh-browser intent propagates to the retry (re-resolves fresh).
            "start_fresh_browser": bool(workflow_run.start_fresh_browser),
            "reuse_browser_session": workflow_run.reuse_browser_session,
            "max_screenshot_scrolls": workflow_run.max_screenshot_scrolls,
            "max_elapsed_time_minutes": workflow_run.max_elapsed_time_minutes,
            "extra_http_headers": workflow_run.extra_http_headers,
            "cdp_connect_headers": workflow_run.cdp_connect_headers,
            "browser_address": workflow_run.browser_address,
            "run_with": workflow_run.run_with,
            "ai_fallback": workflow_run.ai_fallback,
            # Preserve the original run-level browser_type so a retry (manual or credential fallback)
            # keeps the user's engine override instead of silently reverting to routing.
            "browser_type": read_browser_type(workflow_run),
            # Replayed tags may predate the reserved-value rule; drop those entries rather than
            # letting the request-model validator fail the whole retry.
            "run_metadata": drop_reserved_tag_values(run_metadata),
        },
        context=_SERVER_ASSIGNED_BROWSER_ATTACHMENT_CONTEXT,
    )


async def prepare_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,  # this is the deprecated workflow request body
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    request_id: str | None = None,
    debug_session_id: str | None = None,
    code_gen: bool | None = None,
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    workflow_run_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    copilot_session_id: str | None = None,
    resolved_workflow_id: str | None = None,
    tag_write_context: TagWriteContext | None = None,
    block_scoped: bool = False,
    created_by: str | None = None,
    refuse_unusable_parameters_before_create: bool = False,
) -> WorkflowRun:
    """
    Prepare a workflow to be run.

    ``resolved_workflow_id`` pins the exact workflow version row; when None, resolve by
    permanent id + version.
    """
    workflow_run, _ = await _prepare_workflow(
        workflow_id=workflow_id,
        organization=organization,
        workflow_request=workflow_request,
        template=template,
        version=version,
        max_steps=max_steps,
        request_id=request_id,
        debug_session_id=debug_session_id,
        code_gen=code_gen,
        parent_workflow_run_id=parent_workflow_run_id,
        trigger_type=trigger_type,
        workflow_schedule_id=workflow_schedule_id,
        workflow_run_id=workflow_run_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        fallback_attempt=fallback_attempt,
        ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
        copilot_session_id=copilot_session_id,
        resolved_workflow_id=resolved_workflow_id,
        tag_write_context=tag_write_context,
        block_scoped=block_scoped,
        created_by=created_by,
        refuse_unusable_parameters_before_create=refuse_unusable_parameters_before_create,
    )
    return workflow_run


async def _prepare_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    request_id: str | None = None,
    debug_session_id: str | None = None,
    code_gen: bool | None = None,
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    workflow_run_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    copilot_session_id: str | None = None,
    resolved_workflow_id: str | None = None,
    tag_write_context: TagWriteContext | None = None,
    block_scoped: bool = False,
    created_by: str | None = None,
    refuse_unusable_parameters_before_create: bool = False,
    post_dispatch_effects: list[Callable[[], Awaitable[None]]] | None = None,
) -> tuple[WorkflowRun, Workflow]:
    if template:
        if workflow_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_id)

    # One ambient session keeps the version lookup in the same transaction as the run insert, as it
    # was when setup_workflow_run resolved the version itself, and one connection across setup's commits.
    async with app.DATABASE.workflow_runs.Session.pinned():
        workflow = await app.WORKFLOW_SERVICE.resolve_workflow_for_run(
            workflow_permanent_id=workflow_id,
            organization=organization,
            is_template_workflow=template,
            version=version,
            resolved_workflow_id=resolved_workflow_id,
        )
        workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
            request_id=request_id,
            workflow_request=workflow_request,
            workflow_permanent_id=workflow_id,
            organization=organization,
            version=version,
            max_steps_override=max_steps,
            reject_empty_workflow=True,
            is_template_workflow=template,
            debug_session_id=debug_session_id,
            code_gen=code_gen,
            workflow_run_id=workflow_run_id,
            parent_workflow_run_id=parent_workflow_run_id,
            trigger_type=trigger_type,
            workflow_schedule_id=workflow_schedule_id,
            retried_from_workflow_run_id=retried_from_workflow_run_id,
            fallback_attempt=fallback_attempt,
            ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
            copilot_session_id=copilot_session_id,
            resolved_workflow_id=resolved_workflow_id,
            tag_write_context=tag_write_context,
            block_scoped=block_scoped,
            created_by=created_by,
            refuse_unusable_parameters_before_create=refuse_unusable_parameters_before_create,
            workflow=workflow,
            post_dispatch_effects=post_dispatch_effects,
        )
        await app.DATABASE.tasks.create_task_run(
            task_run_type=RunType.workflow_run,
            organization_id=organization.organization_id,
            run_id=workflow_run.workflow_run_id,
            title=workflow.title,
            status=RunStatus.queued,
            workflow_permanent_id=workflow_id,
            parent_workflow_run_id=parent_workflow_run_id,
            debug_session_id=debug_session_id,
        )

    if max_steps:
        LOG.info("Overriding max steps per run", max_steps_override=max_steps)

    return workflow_run, workflow


async def run_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,  # this is the deprecated workflow request body
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    api_key: str | None = None,
    request_id: str | None = None,
    request: Request | None = None,
    background_tasks: BackgroundTasks | None = None,
    block_labels: list[str] | None = None,
    block_outputs: dict[str, t.Any] | None = None,
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    tag_write_context: TagWriteContext | None = None,
    created_by: str | None = None,
    refuse_unusable_parameters_before_create: bool = False,
) -> WorkflowRun:
    workflow_run, _ = await run_workflow_returning_owned_workflow(
        workflow_id=workflow_id,
        organization=organization,
        workflow_request=workflow_request,
        template=template,
        version=version,
        max_steps=max_steps,
        api_key=api_key,
        request_id=request_id,
        request=request,
        background_tasks=background_tasks,
        block_labels=block_labels,
        block_outputs=block_outputs,
        parent_workflow_run_id=parent_workflow_run_id,
        trigger_type=trigger_type,
        workflow_schedule_id=workflow_schedule_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        fallback_attempt=fallback_attempt,
        ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
        tag_write_context=tag_write_context,
        created_by=created_by,
        refuse_unusable_parameters_before_create=refuse_unusable_parameters_before_create,
    )
    return workflow_run


async def run_workflow_returning_owned_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    api_key: str | None = None,
    request_id: str | None = None,
    request: Request | None = None,
    background_tasks: BackgroundTasks | None = None,
    block_labels: list[str] | None = None,
    block_outputs: dict[str, t.Any] | None = None,
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    tag_write_context: TagWriteContext | None = None,
    created_by: str | None = None,
    refuse_unusable_parameters_before_create: bool = False,
) -> tuple[WorkflowRun, Workflow | None]:
    # Fail fast before the run is prepared/persisted: reject a run-level browser_type this runtime
    # cannot honor with a 4xx, rather than accepting it and failing at launch. No-op when unset or on
    # a runtime that supports an explicit selection (cloud).
    try:
        ensure_runtime_supports_browser_type(read_browser_type(workflow_request))
    except SelectedBrowserTypeUnsupportedError as e:
        raise SkyvernHTTPException(str(e), HTTPStatus.BAD_REQUEST) from e
    # Best-effort bookkeeping that no dispatch gate reads is written after the Temporal submit, off the
    # created-to-queued path; it still runs if the submit fails, as it did when it ran before the submit.
    post_dispatch_effects: list[Callable[[], Awaitable[None]]] = []
    try:
        workflow_run, workflow = await _prepare_workflow(
            workflow_id=workflow_id,
            organization=organization,
            workflow_request=workflow_request,
            template=template,
            version=version,
            max_steps=max_steps,
            request_id=request_id,
            parent_workflow_run_id=parent_workflow_run_id,
            trigger_type=trigger_type,
            workflow_schedule_id=workflow_schedule_id,
            retried_from_workflow_run_id=retried_from_workflow_run_id,
            fallback_attempt=fallback_attempt,
            ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
            tag_write_context=tag_write_context,
            created_by=created_by,
            refuse_unusable_parameters_before_create=refuse_unusable_parameters_before_create,
            post_dispatch_effects=post_dispatch_effects,
        )
        # A template run resolves its workflow without an org filter, so it can be another org's row, which must
        # reach neither the executor nor a caller that echoes it back.
        owned_workflow = workflow if workflow.organization_id == organization.organization_id else None

        await AsyncExecutorFactory.get_executor().execute_workflow(
            request=request,
            background_tasks=background_tasks,
            organization=organization,
            workflow_id=workflow_run.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            max_steps_override=max_steps,
            browser_session_id=workflow_run.browser_session_id,
            api_key=api_key,
            block_labels=block_labels,
            block_outputs=block_outputs,
            resolved_workflow=owned_workflow,
        )
    finally:
        for effect in post_dispatch_effects:
            with contained_effect("post-dispatch bookkeeping"):
                await effect()

    return workflow_run, owned_workflow


async def get_workflow_run_response(
    workflow_run_id: str, organization_id: str | None = None, cap_output_values: bool = False
) -> WorkflowRunResponse | None:
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        return None
    workflow_run_resp = await app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
        include_step_count=True,
        cap_output_values=cap_output_values,
        workflow_run=workflow_run,
    )
    app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}"
    # A fresh run reads/writes no saved memory; its run_request echoes start_fresh_browser and drops the
    # session/profile so it stays valid (the mutually-exclusive validators) and reflects what was asked.
    fresh_browser = bool(workflow_run.start_fresh_browser)
    return WorkflowRunResponse(
        run_id=workflow_run_id,
        run_type=RunType.workflow_run,
        status=RunStatus(workflow_run.status),
        attempt=workflow_run_resp.attempt,
        retry_pending=workflow_run_resp.retry_pending,
        next_attempt_at=workflow_run_resp.next_attempt_at,
        attempts=workflow_run_resp.attempts,
        output=workflow_run_resp.outputs,
        downloaded_files=workflow_run_resp.downloaded_files,
        recording_url=workflow_run_resp.recording_url,
        recording_archived=workflow_run_resp.recording_archived,
        screenshot_urls=workflow_run_resp.screenshot_urls,
        failure_reason=workflow_run_resp.failure_reason,
        queued_at=workflow_run.queued_at,
        started_at=workflow_run.started_at,
        finished_at=workflow_run.finished_at,
        app_url=app_url,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        run_with=workflow_run.run_with,
        ai_fallback=workflow_run.ai_fallback,
        browser_session_id=workflow_run.browser_session_id,
        browser_profile_id=workflow_run.browser_profile_id,
        browser_seed_source=workflow_run.browser_seed_source,
        browser_settings_receipt=workflow_run.browser_settings_receipt,
        max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
        script_run=workflow_run.script_run,
        script_id=workflow_run.script_run.script_id if workflow_run.script_run else None,
        run_request=WorkflowRunRequest.model_validate(
            {
                "workflow_id": workflow_run.workflow_permanent_id,
                "title": workflow_run_resp.workflow_title,
                "parameters": workflow_run_resp.parameters,
                "proxy_location": workflow_run.proxy_location,
                "webhook_url": workflow_run.webhook_callback_url or None,
                "totp_url": workflow_run.totp_verification_url or None,
                "totp_identifier": workflow_run.totp_identifier,
                "max_screenshot_scrolls": workflow_run.max_screenshot_scrolls,
                "browser_address": workflow_run.browser_address,
                "browser_profile_id": None if fresh_browser else workflow_run.browser_profile_id,
                "browser_session_id": None if fresh_browser else workflow_run.browser_session_id,
                "start_fresh_browser": fresh_browser,
                "browser_type": read_browser_type(workflow_run),
            },
            context=_SERVER_ASSIGNED_BROWSER_ATTACHMENT_CONTEXT,
        ),
        errors=workflow_run_resp.errors,
        step_count=workflow_run_resp.total_steps,
    )
