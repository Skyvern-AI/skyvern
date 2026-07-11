from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.email_inbox_block import EmailInboxBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.emails import EmailMessage
from skyvern.schemas.workflows import BlockResult, BlockStatus, EmailInboxBlockYAML, WorkflowDefinitionYAML
from skyvern.services import email

_NOW = datetime.now(UTC)


class _FakeContextForTest:
    def __init__(self, organization_id: str) -> None:
        self.values: dict[str, Any] = {}
        self.secrets: dict[str, Any] = {}
        self.include_secrets_in_templates = False
        self.workflow_title = "wf-title"
        self.workflow_id = "wf-id"
        self.workflow_permanent_id = "wf-perm-id"
        self.workflow_run_id = "wr_test"
        self.workflow_run_outputs: dict[str, Any] = {}
        self.browser_session_id: str | None = None
        self.organization_id: str | None = organization_id

    def get_block_metadata(self, label: str) -> dict[str, Any]:
        return {}

    def build_workflow_run_summary(self) -> str:
        return ""


def _make_output_parameter(label: str = "read_email") -> OutputParameter:
    return OutputParameter(
        parameter_type="output",
        key=f"{label}_output",
        workflow_id="test_wf",
        output_parameter_id=f"op_{label}",
        created_at=_NOW,
        modified_at=_NOW,
    )


def _make_block(**overrides: Any) -> EmailInboxBlock:
    data: dict[str, Any] = {
        "label": "read_email",
        "output_parameter": _make_output_parameter(),
        "email_client": "gmail",
        "credential_id": "cred_123",
    }
    data.update(overrides)
    return EmailInboxBlock(**data)


async def _execute_block_for_test(
    block: EmailInboxBlock,
    *,
    workflow_run_id: str = "wr_test",
    organization_id: str = "org_1",
) -> BlockResult:
    ctx = _FakeContextForTest(organization_id=organization_id)

    async def _fake_build(self: Block, **kwargs: Any) -> BlockResult:
        return BlockResult(
            success=kwargs.get("success", False),
            failure_reason=kwargs.get("failure_reason"),
            output_parameter=self.output_parameter,
            output_parameter_value=kwargs.get("output_parameter_value"),
            status=kwargs.get("status", BlockStatus.failed),
            workflow_run_block_id=kwargs.get("workflow_run_block_id"),
        )

    with (
        patch.object(EmailInboxBlock, "get_workflow_run_context", staticmethod(lambda _wr_id: ctx)),
        patch.object(EmailInboxBlock, "record_output_parameter_value", new=AsyncMock(return_value=None)),
        patch.object(EmailInboxBlock, "build_block_result", new=_fake_build),
    ):
        return await block.execute(
            workflow_run_id=workflow_run_id,
            workflow_run_block_id="wrb_test",
            organization_id=organization_id,
        )


def _messages() -> list[EmailMessage]:
    return [
        EmailMessage(
            id="msg_1",
            thread_id="thread_1",
            subject="First",
            from_email="sender@example.com",
            snippet="first snippet",
            body_text="first body",
        ),
        EmailMessage(
            id="msg_2",
            thread_id="thread_2",
            subject="Second",
            from_email="sender@example.com",
            snippet="second snippet",
            body_text="second body",
        ),
    ]


@pytest.mark.asyncio
async def test_execute_reads_gmail_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    list_messages = AsyncMock(return_value=_messages())
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", list_messages)

    result = await _execute_block_for_test(_make_block(folder="INBOX"))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["email_client"] == "gmail"
    assert result.output_parameter_value["folder"] == "INBOX"
    assert result.output_parameter_value["candidate_count"] == 2
    assert result.output_parameter_value["matched_count"] == 2
    assert [email["id"] for email in result.output_parameter_value["emails"]] == ["msg_1", "msg_2"]
    list_messages.assert_awaited_once()
    assert list_messages.await_args.kwargs["email_client"] == "gmail"
    assert list_messages.await_args.kwargs["folder"] == "INBOX"


@pytest.mark.asyncio
async def test_execute_reads_outlook_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    list_messages = AsyncMock(return_value=_messages())
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_microsoft_credentials", AsyncMock(return_value="AT"), raising=False)
    monkeypatch.setattr(email, "list_folder_messages", list_messages)

    result = await _execute_block_for_test(_make_block(email_client="outlook", folder="inbox"))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["email_client"] == "outlook"
    assert result.output_parameter_value["folder"] == "inbox"
    assert result.output_parameter_value["candidate_count"] == 2
    assert result.output_parameter_value["matched_count"] == 2
    assert [email["id"] for email in result.output_parameter_value["emails"]] == ["msg_1", "msg_2"]
    list_messages.assert_awaited_once()
    assert list_messages.await_args.kwargs["email_client"] == "outlook"
    assert list_messages.await_args.kwargs["folder"] == "inbox"


@pytest.mark.asyncio
async def test_execute_filters_with_prompt_preserving_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=_messages()))

    async def match_email(*, criteria: str, email: EmailMessage, organization_id: str) -> bool:
        return email.id == "msg_2"

    monkeypatch.setattr(email, "match_email", match_email)

    result = await _execute_block_for_test(_make_block(prompt="Find the second email"))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["candidate_count"] == 2
    assert result.output_parameter_value["matched_count"] == 1
    assert [email["id"] for email in result.output_parameter_value["emails"]] == ["msg_2"]


@pytest.mark.asyncio
async def test_execute_missing_credential_fails() -> None:
    result = await _execute_block_for_test(_make_block(credential_id=None))

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == "credential_id is required"


@pytest.mark.asyncio
async def test_execute_reconnect_required_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(
        email,
        "list_folder_messages",
        AsyncMock(
            side_effect=email.GmailAPIError(
                status=403,
                code="reconnect_required",
                message="Insufficient scopes",
            )
        ),
    )

    result = await _execute_block_for_test(_make_block())

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == "Reconnect the Gmail account: Insufficient scopes"
    assert result.output_parameter_value == {
        "status_code": 403,
        "code": "reconnect_required",
        "error": "Insufficient scopes",
    }


def test_email_inbox_yaml_workflow_converts_to_runtime_block() -> None:
    output = _make_output_parameter("read_email")
    definition = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "email_inbox",
                    "label": "read_email",
                    "email_client": "gmail",
                    "credential_id": "{{ gmail_credential_id }}",
                    "folder": "INBOX",
                    "prompt": "Find receipts",
                    "sender": "billing@example.com",
                    "subject": "receipt",
                    "newer_than_days": 7,
                    "max_results": 10,
                    "include_body": False,
                }
            ],
        }
    )
    block_yaml = definition.blocks[0]
    assert isinstance(block_yaml, EmailInboxBlockYAML)

    block = block_yaml_to_block(block_yaml, {output.key: output})

    assert isinstance(block, EmailInboxBlock)
    assert block.email_client == "gmail"
    assert block.credential_id == "{{ gmail_credential_id }}"
    assert block.folder == "INBOX"
    assert block.prompt == "Find receipts"
    assert block.sender == "billing@example.com"
    assert block.subject == "receipt"
    assert block.newer_than_days == 7
    assert block.max_results == 10
    assert block.include_body is False
