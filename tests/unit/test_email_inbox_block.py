from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.email_inbox_block import EmailInboxBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.emails import EmailAttachment, EmailMessage
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
        self.block_metadata: dict[str, dict[str, Any]] = {}

    def get_block_metadata(self, label: str) -> dict[str, Any]:
        return self.block_metadata.get(label, {})

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
    block_metadata: dict[str, dict[str, Any]] | None = None,
) -> BlockResult:
    ctx = _FakeContextForTest(organization_id=organization_id)
    ctx.block_metadata = block_metadata or {}

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
    assert list_messages.await_args.kwargs["include_attachments"] is False


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
async def test_execute_excludes_malformed_llm_output_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=_messages()))
    responses = iter([{"reasoning": "missing matches"}, {"reasoning": "matches", "matches": True}])

    async def match_llm(**_kwargs: Any) -> dict[str, Any]:
        return next(responses)

    monkeypatch.setattr(email.inbox.app, "SECONDARY_LLM_API_HANDLER", match_llm)

    result = await _execute_block_for_test(_make_block(prompt="Find the second email"))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["candidate_count"] == 2
    assert result.output_parameter_value["matched_count"] == 1
    assert [message["id"] for message in result.output_parameter_value["emails"]] == ["msg_2"]


@pytest.mark.asyncio
async def test_execute_propagates_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=_messages()))

    async def fail_llm(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(email.inbox.app, "SECONDARY_LLM_API_HANDLER", fail_llm)

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        await _execute_block_for_test(_make_block(prompt="Find invoices"))


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
                    "download_attachments": True,
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
    assert block.download_attachments is True


@pytest.mark.asyncio
async def test_download_attachments_are_opt_in_and_preserve_the_legacy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_messages = AsyncMock(
        return_value=[EmailMessage(id="msg_1", attachments=[EmailAttachment(name="sample.pdf", size=4)])]
    )
    download_attachment = AsyncMock(return_value=b"data")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", list_messages)
    monkeypatch.setattr(email, "download_attachment", download_attachment)

    result = await _execute_block_for_test(_make_block())

    assert result.success is True
    assert result.output_parameter_value is not None
    assert "downloaded_files" not in result.output_parameter_value
    assert result.output_parameter_value["emails"][0]["attachments"] == [
        {"name": "sample.pdf", "mime_type": None, "size": 4, "attachment_id": None}
    ]
    download_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_downloads_matched_attachments_without_email_body_and_registers_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    message = EmailMessage(
        id="msg_1",
        attachments=[
            EmailAttachment(name="../invoice:1.pdf", mime_type="application/pdf", size=None, attachment_id="att_1")
        ],
    )
    list_messages = AsyncMock(return_value=[message])
    download_attachment = AsyncMock(return_value=b"pdf!")
    file_info = FileInfo(url="https://example.test/attachment", filename="_invoice_1-0003-0002.pdf")
    save_downloaded_files = AsyncMock()
    get_downloaded_files = AsyncMock(return_value=[file_info])
    download_dir = tmp_path / "workflow-downloads"
    (download_dir / "_invoice_1-0003.pdf").parent.mkdir(parents=True)
    (download_dir / "_invoice_1-0003.pdf").write_bytes(b"old")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", list_messages)
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: download_dir,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", save_downloaded_files)
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", get_downloaded_files)

    result = await _execute_block_for_test(
        _make_block(include_body=False, download_attachments=True),
        block_metadata={"read_email": {"current_index": 2}},
    )

    assert result.success is True
    assert result.output_parameter_value is not None
    list_messages.assert_awaited_once()
    assert list_messages.await_args.kwargs["include_body"] is False
    assert list_messages.await_args.kwargs["include_attachments"] is True
    download_attachment.assert_awaited_once()
    assert (download_dir / "_invoice_1-0003-0002.pdf").stat().st_size == 4
    assert result.output_parameter_value["downloaded_files"] == [file_info.model_dump()]
    assert result.output_parameter_value["downloaded_file_urls"] == [file_info.url]
    assert result.output_parameter_value["emails"][0]["attachments"][0] == {
        "name": "../invoice:1.pdf",
        "mime_type": "application/pdf",
        "size": None,
        "attachment_id": "att_1",
        "file_name": "_invoice_1-0003-0002.pdf",
        "file_path": str(download_dir / "_invoice_1-0003-0002.pdf"),
        "url": file_info.url,
    }
    save_downloaded_files.assert_awaited_once_with(organization_id="org_1", run_id="wr_test")


@pytest.mark.asyncio
async def test_unknown_attachment_size_is_checked_before_saving(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    message = EmailMessage(
        id="msg_1", attachments=[EmailAttachment(name="large.pdf", size=None, attachment_id="att_1")]
    )
    download_attachment = AsyncMock(return_value=b"too large")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.settings.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES", 4
    )

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["emails"][0]["attachments"][0]["download_error"] == "file_size_limit"
    assert list(tmp_path.iterdir()) == []
    download_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_saves_gmail_embedded_attachment_with_byte_bounded_non_ascii_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    name = "請求書" * 40 + "\x00\ud800.pdf"
    message = EmailMessage(
        id="msg_1",
        attachments=[
            EmailAttachment(name=name, size=4, embedded_data="JVBERg"),
            EmailAttachment(name="invoice.CRDOWNLOAD", size=4, embedded_data="JVBERg"),
            EmailAttachment(name="a" * 250 + ".pdf", size=4, embedded_data="JVBERg"),
        ],
    )
    download_attachment = AsyncMock()
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", AsyncMock(return_value=[]))

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert all("download_error" not in a for a in result.output_parameter_value["emails"][0]["attachments"])
    json.dumps(result.output_parameter_value, ensure_ascii=False).encode()
    assert (tmp_path / "invoice_CRDOWNLOAD").read_bytes() == b"%PDF"
    assert (tmp_path / ("a" * 196 + ".pdf")).read_bytes() == b"%PDF"
    [saved] = [
        path for path in tmp_path.iterdir() if path.name != "invoice_CRDOWNLOAD" and not path.name.startswith("a")
    ]
    assert saved.read_bytes() == b"%PDF"
    assert saved.suffix == ".pdf"
    assert len(saved.name.encode()) <= 255
    download_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_downloads_attachments_only_for_emails_matching_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    messages = [
        EmailMessage(
            id=message_id, attachments=[EmailAttachment(name=f"{message_id}.pdf", size=1, attachment_id=message_id)]
        )
        for message_id in ("msg_unmatched", "msg_matched")
    ]
    messages[0].attachment_list_error = "ServiceUnavailable"

    async def match_email(*, email: EmailMessage, **_kwargs: Any) -> bool:
        return email.id == "msg_matched"

    download_attachment = AsyncMock(return_value=b"x")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=messages))
    monkeypatch.setattr(email, "match_email", match_email)
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(
        app.STORAGE,
        "get_current_attempt_downloaded_files",
        AsyncMock(return_value=[FileInfo(url="https://example.test/matched", filename="msg_matched.pdf")]),
    )

    result = await _execute_block_for_test(
        _make_block(prompt="match one", download_attachments=True),
    )

    assert result.success is True
    assert result.output_parameter_value is not None
    assert [call.kwargs["message_id"] for call in download_attachment.await_args_list] == ["msg_matched"]
    assert [message["id"] for message in result.output_parameter_value["emails"]] == ["msg_matched"]

    messages[1].attachment_list_error = "attachment_list_truncated"
    failed = await _execute_block_for_test(_make_block(prompt="match one", download_attachments=True))
    assert failed.success is False
    assert "Could not list every attachment" in (failed.failure_reason or "")


@pytest.mark.asyncio
async def test_skips_inline_and_oversized_attachments_before_fetching(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    message = EmailMessage(
        id="msg_1",
        attachments=[
            EmailAttachment(name="logo.png", size=2, attachment_id="inline", is_inline=True),
            EmailAttachment(name="large.pdf", size=11, attachment_id="large"),
        ],
    )
    download_attachment = AsyncMock(return_value=b"not used")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.settings.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES", 10
    )

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is True
    assert result.output_parameter_value is not None
    attachments = result.output_parameter_value["emails"][0]["attachments"]
    assert attachments[0]["download_error"] == "inline_attachment_skipped"
    assert attachments[1]["download_error"] == "file_size_limit"
    assert result.output_parameter_value["downloaded_files"] == []
    download_attachment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment_size", "existing_file_count", "existing_file_size", "expected_error"),
    [
        (11, 0, 0, "file_size_limit"),
        (5, 1, 9, "run_size_limit"),
        (5, 3, 0, "file_count_limit"),
    ],
)
async def test_attachment_downloads_respect_run_caps_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    attachment_size: int,
    existing_file_count: int,
    existing_file_size: int,
    expected_error: str,
) -> None:
    download_dir = tmp_path / "workflow-downloads"
    download_dir.mkdir()
    for index in range(existing_file_count):
        (download_dir / f"existing-{index}.dat").write_bytes(b"x" * existing_file_size)
    message = EmailMessage(
        id="msg_1",
        attachments=[EmailAttachment(name="sample.bin", size=attachment_size, attachment_id="att_1")],
    )
    download_attachment = AsyncMock(return_value=b"small")
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(email, "download_attachment", download_attachment)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: download_dir,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.settings.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES", 10
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.settings.BROWSER_DOWNLOAD_MAX_RUN_SIZE_BYTES", 10
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.settings.BROWSER_DOWNLOAD_MAX_FILES_PER_RUN", 3
    )

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["emails"][0]["attachments"][0]["download_error"] == expected_error
    download_attachment.assert_not_awaited()
    if not existing_file_count:
        return

    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_download_retry_started_at",
        AsyncMock(return_value=datetime.now(UTC) + timedelta(minutes=1)),
    )
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", AsyncMock(return_value=[]))
    retried = await _execute_block_for_test(_make_block(download_attachments=True))
    assert retried.output_parameter_value is not None
    assert retried.output_parameter_value["emails"][0]["attachments"][0].get("download_error") != expected_error


@pytest.mark.asyncio
async def test_attachment_download_failure_does_not_fail_block(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    message = EmailMessage(id="msg_1", attachments=[EmailAttachment(name="sample.pdf", size=4, attachment_id="att_1")])
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(
        email,
        "download_attachment",
        AsyncMock(side_effect=email.GmailAPIError(status=500, code="upstream_unavailable", message="retry later")),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is True
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["emails"][0]["attachments"][0]["download_error"] == "download_failed"
    assert result.output_parameter_value["downloaded_files"] == []


@pytest.mark.asyncio
async def test_attachment_reconnect_required_fails_block_without_provider_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    message = EmailMessage(id="msg_1", attachments=[EmailAttachment(name="sample.pdf", size=4, attachment_id="att_1")])
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "get_google_workspace_credentials", AsyncMock(return_value=SimpleNamespace(token="AT"))
    )
    monkeypatch.setattr(email, "list_folder_messages", AsyncMock(return_value=[message]))
    monkeypatch.setattr(
        email,
        "download_attachment",
        AsyncMock(
            side_effect=email.GmailAPIError(status=401, code="reconnect_required", message="private provider text")
        ),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.email_inbox_block.get_path_for_workflow_download_directory",
        lambda _run_id: tmp_path,
    )

    result = await _execute_block_for_test(_make_block(download_attachments=True))

    assert result.success is False
    assert result.output_parameter_value == {
        "status_code": 401,
        "code": "reconnect_required",
        "error": "Reconnect the Gmail account to download attachments",
    }
