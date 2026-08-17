from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot.completion_verification import evaluate_completion_criteria
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.tools.completion import _build_run_evidence_snapshot
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    CodeBlock,
    ErrorCode,
    build_block_failure_output,
    build_user_defined_error_output,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.services import webhook_service

LONG_CREDENTIAL = "templated-password-very-long-13668"
SHORT_CREDENTIAL = "12"
CODE_CREDENTIAL = "587"
GENERIC_REASONING = "CodeBlock failed while running user code."
SAFE_CODE = "DECLARED_FAILURE"
FIXED_NOW = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)


def _context(*secrets: str) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="wf",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        mask_secrets=True,
    )
    context.secrets.update({f"credential_{index}": secret for index, secret in enumerate(secrets)})
    context.workflow = SimpleNamespace(
        enable_self_healing=False,
        workflow_definition=None,
        created_by="copilot",
        edited_by=None,
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
    )
    return context


def _output_parameter() -> OutputParameter:
    now = FIXED_NOW
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="code_output",
        description="test output",
        output_parameter_id="op_code",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


async def _inline_failure_output(
    *,
    error_code: str,
    reasoning: str,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Exercise the inline ErrorCode recognizer and its production output builder."""
    context = _context(*secrets)
    block = CodeBlock(
        label="code_1",
        code=f"raise ErrorCode({error_code!r}, {reasoning!r})",
        error_code_mapping={error_code: "Declared failure"},
        output_parameter=_output_parameter(),
    )
    function = block.generate_async_user_function(block.code, MagicMock())
    with pytest.raises(ErrorCode) as exc_info:
        await function()
    declared = block._extract_declared_error(exc_info.value, context)
    if declared is None:
        return build_block_failure_output(GENERIC_REASONING, ["user_code_error"])
    return build_user_defined_error_output(declared.error_code, declared.reasoning)


async def _status_response_from_captured_output(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> tuple[WorkflowService, Any]:
    now = FIXED_NOW
    workflow = SimpleNamespace(workflow_permanent_id="wpid_test", title="Credential gate test")
    workflow_run = WorkflowRun(
        workflow_run_id="wr_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        status=WorkflowRunStatus.failed,
        failure_reason=GENERIC_REASONING,
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=workflow)),
            observer=SimpleNamespace(get_task_v2_by_workflow_run_id=AsyncMock(return_value=None)),
            tasks=SimpleNamespace(get_tasks_by_workflow_run_id=AsyncMock(return_value=[])),
            workflow_runs=SimpleNamespace(
                get_workflow_run_parameters=AsyncMock(return_value=[]),
                get_workflow_run_block_errors=AsyncMock(
                    return_value=[("wrb_test", ["user_code_error"], GENERIC_REASONING, captured, "code")]
                ),
                get_workflow_run_retried_by=AsyncMock(return_value=None),
            ),
        ),
    )
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", SimpleNamespace(workflow_run_contexts={}))
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(service, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service, "get_output_parameter_workflow_run_output_parameter_tuples", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(service, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(service, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))
    response = await service.build_workflow_run_status_response("wpid_test", "wr_test", "o_test")
    return service, response


async def _sink_payloads(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> dict[str, object]:
    service, response = await _status_response_from_captured_output(monkeypatch, captured)
    workflow_run = WorkflowRun(
        workflow_run_id="wr_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        status=WorkflowRunStatus.failed,
        webhook_callback_url="https://example.com/hook",
        created_at=FIXED_NOW,
        modified_at=FIXED_NOW,
    )
    monkeypatch.setattr(
        webhook_service.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=workflow_run),
        raising=False,
    )
    monkeypatch.setattr(webhook_service.app, "WORKFLOW_SERVICE", service)
    webhook = await webhook_service._build_workflow_payload("o_test", "wr_test")

    run_data = response.model_dump(mode="json")
    run_data["overall_status"] = run_data.pop("status")
    run_data["blocks"] = [{"label": "code_1", "block_type": "code", "extracted_data": captured}]
    snapshot = _build_run_evidence_snapshot(
        SimpleNamespace(
            last_workflow=SimpleNamespace(
                workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="code_1")])
            ),
            last_workflow_yaml=None,
            composition_page_evidence=None,
            registered_artifact_evidence=None,
            pre_run_page_reference=None,
        ),
        {"data": run_data},
    )
    seen: dict[str, str] = {}

    async def verifier_handler(**kwargs: object) -> dict[str, object]:
        seen["prompt"] = str(kwargs["prompt"])
        return {"verdicts": [{"criterion_id": "c0", "satisfied": False, "reason_code": "evidence_contradicts"}]}

    await evaluate_completion_criteria(
        [CompletionCriterion(id="c0", outcome="The code block completed successfully")], snapshot, verifier_handler
    )
    return {
        "run_status": response.model_dump(mode="json"),
        # Same response model, deliberately retained only as serialization-shape coverage.
        "studio_serialization_shape": response.model_dump(by_alias=True, mode="json"),
        "webhook": webhook.payload,
        "completion_verifier": seen["prompt"],
    }


def _serialized(payload: object) -> str:
    return json.dumps(payload, default=str)


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _string_values(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _string_values(item)]
    return []


@pytest.mark.asyncio
async def test_inline_accepted_typed_error_reaches_every_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _inline_failure_output(error_code=SAFE_CODE, reasoning="Declared safe failure")
    assert captured["errors"][0]["error_type"] == "USER_DEFINED_ERROR"
    for payload in (await _sink_payloads(monkeypatch, captured)).values():
        serialized = _serialized(payload)
        assert SAFE_CODE in serialized
        assert "USER_DEFINED_ERROR" in serialized


@pytest.mark.asyncio
async def test_inline_secret_in_code_becomes_generic_failure_at_every_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _inline_failure_output(
        error_code=f"ERR_{CODE_CREDENTIAL}", reasoning="Declared failure", secrets=(CODE_CREDENTIAL,)
    )
    assert all(error.get("error_type") != "USER_DEFINED_ERROR" for error in captured["errors"])
    for payload in (await _sink_payloads(monkeypatch, captured)).values():
        serialized = _serialized(payload)
        # Short secrets can collide with numeric sink metadata; inspect recursively collected strings only.
        assert all(CODE_CREDENTIAL not in text for text in _string_values(payload))
        assert "USER_DEFINED_ERROR" not in serialized
        assert "user_code_error" in serialized
        assert GENERIC_REASONING in serialized


@pytest.mark.asyncio
async def test_inline_secrets_in_reasoning_are_redacted_at_every_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _inline_failure_output(
        error_code=SAFE_CODE,
        reasoning=f"failure contained {LONG_CREDENTIAL} and {SHORT_CREDENTIAL}",
        secrets=(LONG_CREDENTIAL, SHORT_CREDENTIAL),
    )
    assert captured["errors"][0]["error_type"] == "USER_DEFINED_ERROR"
    for payload in (await _sink_payloads(monkeypatch, captured)).values():
        serialized = _serialized(payload)
        assert SAFE_CODE in serialized
        assert "USER_DEFINED_ERROR" in serialized
        assert "[redacted]" in serialized
        assert LONG_CREDENTIAL not in serialized
        # Short secrets can collide with numeric sink metadata; inspect recursively collected strings only.
        assert all(SHORT_CREDENTIAL not in text for text in _string_values(payload))


@pytest.mark.asyncio
async def test_inline_code_containment_gate_disabled_would_leak_to_every_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CodeBlock, "_contains_registered_secret", classmethod(lambda cls, value, context: False))
    captured = await _inline_failure_output(
        error_code=f"ERR_{CODE_CREDENTIAL}", reasoning="Declared failure", secrets=(CODE_CREDENTIAL,)
    )
    assert captured["errors"][0]["error_type"] == "USER_DEFINED_ERROR"
    for payload in (await _sink_payloads(monkeypatch, captured)).values():
        # Presence must come from an attacker-visible string, not an accidental numeric metadata match.
        assert any(CODE_CREDENTIAL in text for text in _string_values(payload))
