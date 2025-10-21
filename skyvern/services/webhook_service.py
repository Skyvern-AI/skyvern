import json
from datetime import datetime, timezone

from skyvern.forge.sdk.schemas.tasks import TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunResponseBase, WorkflowRunStatus
from skyvern.schemas.runs import (
    ProxyLocation,
    RunStatus,
    RunType,
    TaskRunRequest,
    TaskRunResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_sample_task_payload(run_id: str | None = None) -> str:
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
    return json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)


def build_sample_workflow_run_payload(run_id: str | None = None) -> str:
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
        app_url=f"https://app.skyvern.com/workflows/{workflow_id}/{workflow_run_id}",
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
    return json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
