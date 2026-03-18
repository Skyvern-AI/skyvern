"""Tests that webhook payloads include non-null timestamp fields.

Regression test for SKY-7211: queued_at, started_at, finished_at were always
null in webhook payloads because WorkflowRunResponse was constructed without
them, then payload_dict.update() overwrote the correct values with None.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunResponseBase, WorkflowRunStatus
from skyvern.schemas.runs import RunStatus, RunType, WorkflowRunRequest, WorkflowRunResponse


def _make_status_response() -> WorkflowRunResponseBase:
    """Create a WorkflowRunResponseBase with non-null timestamps, as the DB would return."""
    now = datetime.now(tz=timezone.utc)
    return WorkflowRunResponseBase(
        workflow_id="wpid_test",
        workflow_run_id="wr_test",
        status=WorkflowRunStatus.completed,
        created_at=now,
        modified_at=now,
        queued_at=now,
        started_at=now,
        finished_at=now,
        parameters={"key": "value"},
    )


def test_workflow_webhook_payload_includes_timestamps() -> None:
    """Reproduce the payload_dict.update() overwrite pattern from execute_workflow_webhook
    and verify timestamps are preserved."""
    status_response = _make_status_response()

    # This mirrors the construction in execute_workflow_webhook (service.py)
    workflow_run_response = WorkflowRunResponse(
        run_id="wr_test",
        run_type=RunType.workflow_run,
        status=RunStatus(status_response.status),
        output=status_response.outputs,
        downloaded_files=status_response.downloaded_files,
        recording_url=status_response.recording_url,
        screenshot_urls=status_response.screenshot_urls,
        failure_reason=status_response.failure_reason,
        app_url="https://app.skyvern.com/runs/wr_test",
        script_run=status_response.script_run,
        created_at=status_response.created_at,
        modified_at=status_response.modified_at,
        queued_at=status_response.queued_at,
        started_at=status_response.started_at,
        finished_at=status_response.finished_at,
        run_request=WorkflowRunRequest(
            workflow_id="wpid_test",
            title="Test Workflow",
            parameters={"key": "value"},
            proxy_location=None,
            webhook_url=None,
            totp_url=None,
            totp_identifier=None,
        ),
    )

    # This mirrors the payload merge in execute_workflow_webhook
    payload_dict: dict = json.loads(status_response.model_dump_json())
    workflow_run_response_dict = json.loads(workflow_run_response.model_dump_json())
    payload_dict.update(workflow_run_response_dict)

    assert payload_dict["queued_at"] is not None, "queued_at should not be null in webhook payload"
    assert payload_dict["started_at"] is not None, "started_at should not be null in webhook payload"
    assert payload_dict["finished_at"] is not None, "finished_at should not be null in webhook payload"


def test_webhook_replay_payload_includes_timestamps() -> None:
    """Reproduce the payload_dict.update() overwrite pattern from _build_workflow_payload
    and verify timestamps are preserved."""
    status_response = _make_status_response()

    # This mirrors the construction in _build_workflow_payload (webhook_service.py)
    run_response = WorkflowRunResponse(
        run_id="wr_test",
        run_type=RunType.workflow_run,
        status=RunStatus(status_response.status),
        output=status_response.outputs,
        downloaded_files=status_response.downloaded_files,
        recording_url=status_response.recording_url,
        screenshot_urls=status_response.screenshot_urls,
        failure_reason=status_response.failure_reason,
        app_url="https://app.skyvern.com/runs/wr_test",
        script_run=status_response.script_run,
        created_at=status_response.created_at,
        modified_at=status_response.modified_at,
        queued_at=status_response.queued_at,
        started_at=status_response.started_at,
        finished_at=status_response.finished_at,
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

    assert payload_dict["queued_at"] is not None, "queued_at should not be null in webhook replay payload"
    assert payload_dict["started_at"] is not None, "started_at should not be null in webhook replay payload"
    assert payload_dict["finished_at"] is not None, "finished_at should not be null in webhook replay payload"
