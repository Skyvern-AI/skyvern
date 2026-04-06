"""Tests for default task_version behavior (SDK + API).

Covers:
- Schema rejects absent task_version without injection
- After injecting default "v1", schema validates correctly
- Explicit v1/v2 pass through unchanged
- Invalid task_version is rejected
- Service method default parameter is "v1"
"""

import inspect

import pytest
from pydantic import TypeAdapter, ValidationError

from skyvern.forge.sdk.schemas.prompts import (
    CreateFromPromptRequest,
    CreateWorkflowFromPromptRequestV1,
    CreateWorkflowFromPromptRequestV2,
    PromptedTaskRequestOptionalUrl,
)

_ta: TypeAdapter[CreateFromPromptRequest] = TypeAdapter(CreateFromPromptRequest)


class TestCreateFromPromptRequestSchema:
    def test_explicit_v1_accepted(self) -> None:
        body = {"task_version": "v1", "request": {"user_prompt": "hello", "url": "https://example.com"}}
        data = _ta.validate_python(body)
        assert data.task_version == "v1"
        assert isinstance(data, CreateWorkflowFromPromptRequestV1)

    def test_explicit_v2_accepted(self) -> None:
        body = {"task_version": "v2", "request": {"user_prompt": "hello"}}
        data = _ta.validate_python(body)
        assert data.task_version == "v2"
        assert isinstance(data, CreateWorkflowFromPromptRequestV2)

    def test_absent_task_version_fails_without_injection(self) -> None:
        """Without injection the schema rejects absent task_version."""
        body = {"request": {"user_prompt": "hello", "url": "https://example.com"}}
        with pytest.raises(ValidationError):
            _ta.validate_python(body)

    def test_injected_v1_default_validates(self) -> None:
        """After injecting 'v1' default (as the route does), schema validates."""
        body: dict = {"request": {"user_prompt": "hello", "url": "https://example.com"}}
        if "task_version" not in body:
            body["task_version"] = "v1"
        data = _ta.validate_python(body)
        assert data.task_version == "v1"
        assert isinstance(data, CreateWorkflowFromPromptRequestV1)

    def test_v1_without_url_accepted(self) -> None:
        """V1 request without url should pass — url is inferred by LLM in the service."""
        body = {"task_version": "v1", "request": {"user_prompt": "hello"}}
        data = _ta.validate_python(body)
        assert data.task_version == "v1"
        assert isinstance(data, CreateWorkflowFromPromptRequestV1)
        assert isinstance(data.request, PromptedTaskRequestOptionalUrl)
        assert data.request.url is None

    def test_invalid_task_version_rejected(self) -> None:
        body = {"task_version": "v3", "request": {"user_prompt": "hello"}}
        with pytest.raises(ValidationError):
            _ta.validate_python(body)


class TestServiceDefault:
    def test_service_create_workflow_from_prompt_default_is_v1(self) -> None:
        """The service method must default task_version to 'v1'."""
        from skyvern.forge.sdk.workflow.service import WorkflowService

        sig = inspect.signature(WorkflowService.create_workflow_from_prompt)
        assert sig.parameters["task_version"].default == "v1"
