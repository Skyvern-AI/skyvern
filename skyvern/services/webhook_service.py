from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

import httpx
import structlog
from fastapi import status

from skyvern.config import settings
from skyvern.exceptions import (
    BlockedHost,
    MissingApiKey,
    MissingWebhookTarget,
    SkyvernHTTPException,
    TaskNotFound,
    WebhookReplayError,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import (
    WorkflowRun,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
)
from skyvern.schemas.runs import (
    ProxyLocation,
    RunStatus,
    RunType,
    TaskRunRequest,
    TaskRunResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from skyvern.schemas.webhooks import RunWebhookPreviewResponse, RunWebhookReplayResponse
from skyvern.services import run_service, task_v2_service
from skyvern.utils.url_validators import validate_url

LOG = structlog.get_logger()

RESPONSE_BODY_TRUNCATION_LIMIT = 2048


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_sample_task_payload(run_id: str | None = None) -> dict:
    """
    Build a sample task webhook payload using the real TaskResponse + TaskRunResponse models
    so schema changes are reflected automatically.
    """
    task_id = run_id or "tsk_sample_123456789"
    now = _now()

    task_request = TaskRequest(
        url="https://example.com/start",
        webhook_callback_url="https://webhook.example.com/receive",
        navigation_goal="Visit the sample site and capture details",
        data_extraction_goal="Collect sample output data",
        navigation_payload={"sample_field": "sample_value"},
        proxy_location=ProxyLocation.RESIDENTIAL,
        extra_http_headers={"x-sample-header": "value"},
    )

    task_response = TaskResponse(
        request=task_request,
        task_id=task_id,
        status=TaskStatus.completed,
        created_at=now,
        modified_at=now,
        queued_at=now,
        started_at=now,
        finished_at=now,
        extracted_information={
            "sample_field": "sample_value",
            "example_data": "This is sample extracted data from the task",
        },
        action_screenshot_urls=[
            "https://example.com/screenshots/task-action-1.png",
            "https://example.com/screenshots/task-action-2.png",
        ],
        screenshot_url="https://example.com/screenshots/task-final.png",
        recording_url="https://example.com/recordings/task.mp4",
        downloaded_files=[],
        downloaded_file_urls=[],
        errors=[],
        max_steps_per_run=10,
    )

    payload_dict = json.loads(task_response.model_dump_json(exclude={"request"}))

    task_run_response = TaskRunResponse(
        run_id=task_id,
        run_type=RunType.task_v1,
        status=RunStatus.completed,
        output=payload_dict.get("extracted_information"),
        downloaded_files=None,
        recording_url=payload_dict.get("recording_url"),
        screenshot_urls=payload_dict.get("action_screenshot_urls"),
        failure_reason=payload_dict.get("failure_reason"),
        created_at=now,
        modified_at=now,
        queued_at=now,
        started_at=now,
        finished_at=now,
        app_url=f"https://app.skyvern.com/tasks/{task_id}",
        browser_session_id="pbs_sample_123456",
        max_screenshot_scrolls=payload_dict.get("max_screenshot_scrolls"),
        script_run=None,
        errors=payload_dict.get("errors"),
        run_request=TaskRunRequest(
            prompt="Visit the sample site and collect information",
            url=task_request.url,
            webhook_url=task_request.webhook_callback_url,
            data_extraction_schema=task_request.extracted_information_schema,
            error_code_mapping=task_request.error_code_mapping,
            proxy_location=task_request.proxy_location,
            extra_http_headers=task_request.extra_http_headers,
            browser_session_id=None,
        ),
    )

    payload_dict.update(json.loads(task_run_response.model_dump_json(exclude_unset=True)))
    return payload_dict


def build_sample_workflow_run_payload(run_id: str | None = None) -> dict:
    """
    Build a sample workflow webhook payload using the real WorkflowRunResponseBase + WorkflowRunResponse models
    so schema changes are reflected automatically.
    """
    workflow_run_id = run_id or "wr_sample_123456789"
    workflow_id = "wpid_sample_123"
    now = _now()

    workflow_base = WorkflowRunResponseBase(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        status=WorkflowRunStatus.completed,
        proxy_location=ProxyLocation.RESIDENTIAL,
        webhook_callback_url="https://webhook.example.com/receive",
        queued_at=now,
        started_at=now,
        finished_at=now,
        created_at=now,
        modified_at=now,
        parameters={"sample_param": "sample_value"},
        screenshot_urls=["https://example.com/screenshots/workflow-step.png"],
        recording_url="https://example.com/recordings/workflow.mp4",
        downloaded_files=[],
        downloaded_file_urls=[],
        outputs={"result": "success", "data": "Sample workflow output"},
        total_steps=5,
        total_cost=0.05,
        workflow_title="Sample Workflow",
        browser_session_id="pbs_sample_123456",
        errors=[],
    )

    payload_dict = json.loads(workflow_base.model_dump_json())

    app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run_id}"

    workflow_run_response = WorkflowRunResponse(
        run_id=workflow_run_id,
        run_type=RunType.workflow_run,
        status=RunStatus.completed,
        output=payload_dict.get("outputs"),
        downloaded_files=None,
        recording_url=payload_dict.get("recording_url"),
        screenshot_urls=payload_dict.get("screenshot_urls"),
        failure_reason=payload_dict.get("failure_reason"),
        created_at=now,
        modified_at=now,
        queued_at=payload_dict.get("queued_at"),
        started_at=payload_dict.get("started_at"),
        finished_at=payload_dict.get("finished_at"),
        app_url=app_url,
        browser_session_id=payload_dict.get("browser_session_id"),
        max_screenshot_scrolls=payload_dict.get("max_screenshot_scrolls"),
        script_run=None,
        errors=payload_dict.get("errors"),
        run_request=WorkflowRunRequest(
            workflow_id=workflow_id,
            title=payload_dict.get("workflow_title"),
            parameters=payload_dict.get("parameters"),
            proxy_location=ProxyLocation.RESIDENTIAL,
            webhook_url=payload_dict.get("webhook_callback_url"),
        ),
    )

    payload_dict.update(json.loads(workflow_run_response.model_dump_json(exclude_unset=True)))
    return payload_dict


@dataclass
class _WebhookPayload:
    run_id: str
    run_type: str
    payload: dict
    default_webhook_url: str | None


async def build_run_preview(organization_id: str, run_id: str) -> RunWebhookPreviewResponse:
    """Return the payload and headers that would be used for a replay."""
    payload = await _build_webhook_payload(organization_id=organization_id, run_id=run_id)
    api_key = await _get_api_key(organization_id=organization_id)
    signed_data = generate_skyvern_webhook_signature(payload=payload.payload, api_key=api_key)
    return RunWebhookPreviewResponse(
        run_id=payload.run_id,
        run_type=payload.run_type,
        default_webhook_url=payload.default_webhook_url,
        payload=signed_data.signed_payload,
        headers=signed_data.headers,
    )


async def replay_run_webhook(
    organization_id: str,
    run_id: str,
    target_url: str | None,
    api_key: str | None = None,
) -> RunWebhookReplayResponse:
    """
    Send the webhook payload for a run to either the stored URL or a caller-provided override.

    If `api_key` is provided, it will be used to sign the webhook payload instead of looking up the organization's
    API key from the database. This is useful for endpoints that authenticate with an API key and want the replay
    signature to match the caller-provided key.
    """
    payload = await _build_webhook_payload(organization_id=organization_id, run_id=run_id)
    signing_key = api_key if api_key else await _get_api_key(organization_id=organization_id)
    signed_data = generate_skyvern_webhook_signature(payload=payload.payload, api_key=signing_key)

    url_to_use: str | None = target_url if target_url else payload.default_webhook_url

    if not url_to_use:
        raise MissingWebhookTarget()

    validated_url = _validate_target_url(url_to_use)

    status_code, latency_ms, response_body, error = await _deliver_webhook(
        url=validated_url,
        payload=signed_data.signed_payload,
        headers=signed_data.headers,
    )

    return RunWebhookReplayResponse(
        run_id=payload.run_id,
        run_type=payload.run_type,
        default_webhook_url=payload.default_webhook_url,
        target_webhook_url=validated_url,
        payload=signed_data.signed_payload,
        headers=signed_data.headers,
        status_code=status_code,
        latency_ms=latency_ms,
        response_body=response_body,
        error=error,
    )


async def _build_webhook_payload(organization_id: str, run_id: str) -> _WebhookPayload:
    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        # Attempt to resolve task v2 runs that may not yet be in the runs table.
        task_v2 = await app.DATABASE.get_task_v2(run_id, organization_id=organization_id)
        if task_v2:
            return await _build_task_v2_payload(task_v2)
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=run_id,
            organization_id=organization_id,
        )
        if workflow_run:
            return await _build_workflow_payload(
                organization_id=organization_id,
                workflow_run_id=run_id,
            )
        raise SkyvernHTTPException(
            f"Run {run_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    run_type = _as_run_type_str(run.task_run_type)
    if run.task_run_type in {
        RunType.task_v1,
        RunType.openai_cua,
        RunType.anthropic_cua,
        RunType.ui_tars,
    }:
        return await _build_task_payload(
            organization_id=organization_id,
            run_id=run.run_id,
            run_type_str=run_type,
        )
    if run.task_run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(run.run_id, organization_id=organization_id)
        if not task_v2:
            raise SkyvernHTTPException(
                f"Task v2 run {run_id} missing task record",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return await _build_task_v2_payload(task_v2)
    if run.task_run_type == RunType.workflow_run:
        return await _build_workflow_payload(organization_id=organization_id, workflow_run_id=run.run_id)

    raise WebhookReplayError(f"Run type {run_type} is not supported for webhook replay.")


async def _build_task_payload(organization_id: str, run_id: str, run_type_str: str) -> _WebhookPayload:
    task: Task | None = await app.DATABASE.get_task(run_id, organization_id=organization_id)
    if not task:
        raise TaskNotFound(task_id=run_id)
    if not task.status.is_final():
        LOG.warning(
            "Webhook replay requested for non-terminal task run",
            run_id=run_id,
            status=task.status,
        )
        raise WebhookReplayError(f"Run {run_id} has not reached a terminal state (status={task.status}).")
    latest_step = await app.DATABASE.get_latest_step(run_id, organization_id=organization_id)
    task_response = await app.agent.build_task_response(task=task, last_step=latest_step)

    payload_dict = json.loads(task_response.model_dump_json(exclude={"request"}))

    run_response = await run_service.get_run_response(run_id=run_id, organization_id=organization_id)
    if isinstance(run_response, TaskRunResponse):
        if not run_response.status.is_final():
            LOG.warning(
                "Webhook replay requested for non-terminal task run response",
                run_id=run_id,
                status=run_response.status,
            )
            raise WebhookReplayError(f"Run {run_id} has not reached a terminal state (status={run_response.status}).")
        run_response_json = run_response.model_dump_json(exclude={"run_request"})
        payload_dict.update(json.loads(run_response_json))

    return _WebhookPayload(
        run_id=run_id,
        run_type=run_type_str,
        payload=payload_dict,
        default_webhook_url=task.webhook_callback_url,
    )


async def _build_task_v2_payload(task_v2: TaskV2) -> _WebhookPayload:
    if not task_v2.status.is_final():
        LOG.warning(
            "Webhook replay requested for non-terminal task v2 run",
            run_id=task_v2.observer_cruise_id,
            status=task_v2.status,
        )
        raise WebhookReplayError(
            f"Run {task_v2.observer_cruise_id} has not reached a terminal state (status={task_v2.status})."
        )
    task_run_response = await task_v2_service.build_task_v2_run_response(task_v2)
    if not task_run_response.status.is_final():
        LOG.warning(
            "Webhook replay requested for non-terminal task v2 run response",
            run_id=task_v2.observer_cruise_id,
            status=task_run_response.status,
        )
        raise WebhookReplayError(
            f"Run {task_v2.observer_cruise_id} has not reached a terminal state (status={task_run_response.status})."
        )
    task_run_response_json = task_run_response.model_dump_json(exclude={"run_request"})
    return _WebhookPayload(
        run_id=task_v2.observer_cruise_id,
        run_type=RunType.task_v2.value,
        payload=json.loads(task_run_response_json),
        default_webhook_url=task_v2.webhook_callback_url,
    )


async def _build_workflow_payload(
    organization_id: str,
    workflow_run_id: str,
) -> _WebhookPayload:
    workflow_run: WorkflowRun | None = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    if not workflow_run:
        raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
    if not workflow_run.status.is_final():
        LOG.warning(
            "Webhook replay requested for non-terminal workflow run",
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
        )
        raise WebhookReplayError(
            f"Run {workflow_run_id} has not reached a terminal state (status={workflow_run.status})."
        )

    status_response = await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=workflow_run.organization_id,
    )
    if not status_response.status.is_final():
        LOG.warning(
            "Webhook replay requested for non-terminal workflow run response",
            workflow_run_id=workflow_run_id,
            status=status_response.status,
        )
        raise WebhookReplayError(
            f"Run {workflow_run_id} has not reached a terminal state (status={status_response.status})."
        )

    app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}"

    run_response = WorkflowRunResponse(
        run_id=workflow_run.workflow_run_id,
        run_type=RunType.workflow_run,
        status=RunStatus(status_response.status),
        output=status_response.outputs,
        downloaded_files=status_response.downloaded_files,
        recording_url=status_response.recording_url,
        screenshot_urls=status_response.screenshot_urls,
        failure_reason=status_response.failure_reason,
        app_url=app_url,
        script_run=status_response.script_run,
        created_at=status_response.created_at,
        modified_at=status_response.modified_at,
        errors=status_response.errors,
    )

    payload_dict = json.loads(
        status_response.model_dump_json(
            exclude={
                "webhook_callback_url",
                "totp_verification_url",
                "totp_identifier",
                "extra_http_headers",
            }
        )
    )
    payload_dict.update(json.loads(run_response.model_dump_json(exclude={"run_request"})))

    return _WebhookPayload(
        run_id=workflow_run.workflow_run_id,
        run_type=RunType.workflow_run.value,
        payload=payload_dict,
        default_webhook_url=workflow_run.webhook_callback_url,
    )


async def _get_api_key(organization_id: str) -> str:
    api_key_obj = await app.DATABASE.get_valid_org_auth_token(
        organization_id,
        OrganizationAuthTokenType.api.value,
    )
    if not api_key_obj or not api_key_obj.token:
        raise MissingApiKey()
    return api_key_obj.token


async def _deliver_webhook(
    url: str, payload: str, headers: dict[str, str]
) -> tuple[int | None, int, str | None, str | None]:
    start = perf_counter()
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, content=payload, headers=headers, timeout=httpx.Timeout(10.0))
        status_code = response.status_code
        body_text = response.text or ""
        if len(body_text) > RESPONSE_BODY_TRUNCATION_LIMIT:
            response_body = f"{body_text[:RESPONSE_BODY_TRUNCATION_LIMIT]}\n... (truncated)"
        else:
            response_body = body_text or None
    except httpx.TimeoutException:
        error = "Request timed out after 10 seconds."
        LOG.warning("Webhook replay timed out", url=url)
    except httpx.NetworkError as exc:
        error = f"Could not reach URL: {exc}"
        LOG.warning("Webhook replay network error", url=url, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive guard
        error = f"Unexpected error: {exc}"
        LOG.error("Webhook replay unexpected error", url=url, error=str(exc), exc_info=True)

    latency_ms = int((perf_counter() - start) * 1000)
    return status_code, latency_ms, response_body, error


def _as_run_type_str(run_type: RunType | str | None) -> str:
    if isinstance(run_type, RunType):
        return run_type.value
    if isinstance(run_type, str):
        return run_type
    return "unknown"


def _validate_target_url(url: str) -> str:
    try:
        validated_url = validate_url(url)
        if not validated_url:
            raise SkyvernHTTPException("Invalid webhook URL.", status_code=status.HTTP_400_BAD_REQUEST)
        return validated_url
    except BlockedHost as exc:
        raise SkyvernHTTPException(
            message=(
                f"This URL is blocked by SSRF protection. {str(exc)} "
                "Add the host to ALLOWED_HOSTS to test internal endpoints or use an external receiver "
                "such as webhook.site or requestbin.com."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from exc
    except SkyvernHTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        LOG.error("Unexpected error validating webhook URL", url=url, error=str(exc))
        raise SkyvernHTTPException(
            "Unexpected error while validating the webhook URL.",
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from exc
