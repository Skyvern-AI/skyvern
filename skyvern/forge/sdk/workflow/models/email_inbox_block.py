import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx
import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_path_for_workflow_download_directory, resolve_run_download_id
from skyvern.forge.sdk.artifact.storage.base import get_download_retry_started_at, is_file_from_retry_attempt
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    register_workflow_downloads,
    render_failure_reason,
    sanitize_filename,
)
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.services import email

LOG = structlog.get_logger()


class EmailInboxBlock(Block):
    block_type: Literal[BlockType.EMAIL_INBOX] = BlockType.EMAIL_INBOX  # type: ignore

    email_client: Literal["gmail", "outlook"]
    credential_id: str | None = None
    folder: str | None = None
    prompt: str | None = None
    sender: str | None = None
    subject: str | None = None
    newer_than_days: int | None = None
    max_results: int = 25
    include_body: bool = True
    download_attachments: bool = False
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({"credential_id", "folder", "prompt", "sender", "subject"})

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.credential_id:
            self.credential_id = self.render_templatable_field(
                "credential_id", self.credential_id, workflow_run_context
            )
        if self.folder:
            self.folder = self.render_templatable_field("folder", self.folder, workflow_run_context)
        if self.prompt:
            self.prompt = self.render_templatable_field("prompt", self.prompt, workflow_run_context)
        if self.sender:
            self.sender = self.render_templatable_field("sender", self.sender, workflow_run_context)
        if self.subject:
            self.subject = self.render_templatable_field("subject", self.subject, workflow_run_context)

    async def _failure(
        self,
        *,
        failure_reason: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        output_parameter_value: dict[str, Any] | None = None,
    ) -> BlockResult:
        return await self.build_block_result(
            success=False,
            failure_reason=failure_reason,
            output_parameter_value=output_parameter_value,
            status=BlockStatus.failed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def _match_emails(
        self,
        candidates: list[email.EmailMessage],
        *,
        criteria: str,
        organization_id: str,
    ) -> list[email.EmailMessage]:
        semaphore = asyncio.Semaphore(8)

        async def _match(candidate: email.EmailMessage) -> bool:
            async with semaphore:
                return await email.match_email(
                    criteria=criteria,
                    email=candidate,
                    organization_id=organization_id,
                )

        results = await asyncio.gather(*(_match(candidate) for candidate in candidates))
        return [candidate for candidate, matches in zip(candidates, results) if matches]

    async def _download_matched_attachments(
        self,
        messages: list[email.EmailMessage],
        *,
        access_token: str,
        organization_id: str,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
    ) -> tuple[list[dict[str, Any]], list[FileInfo]]:
        failed = next((message for message in messages if message.attachment_list_error), None)
        if failed is not None:
            raise email.OutlookAPIError(
                status=502,
                code=failed.attachment_list_error,
                message="Could not list every attachment on a matched email",
            )
        email_outputs = [message.model_dump(mode="json") for message in messages]
        if not any(message.attachments for message in messages):
            return email_outputs, []

        run_download_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run_id)
        download_dir = get_path_for_workflow_download_directory(run_download_id)
        # A retried run keeps earlier attempts' files; only this attempt's files count toward the caps.
        attempt_started_at = await get_download_retry_started_at(organization_id, run_download_id)
        existing_files = [
            path
            for path in download_dir.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and not path.name.endswith(BROWSER_DOWNLOADING_SUFFIX)
            and is_file_from_retry_attempt(str(path), attempt_started_at)
        ]
        file_count = len(existing_files)
        run_size = sum(path.stat().st_size for path in existing_files)
        file_limit = settings.BROWSER_DOWNLOAD_MAX_FILE_SIZE_BYTES
        run_limit = settings.BROWSER_DOWNLOAD_MAX_RUN_SIZE_BYTES
        count_limit = settings.BROWSER_DOWNLOAD_MAX_FILES_PER_RUN
        loop_index = workflow_run_context.get_block_metadata(self.label).get("current_index")
        saved_names: set[str] = set()

        async with httpx.AsyncClient(timeout=20.0) as client:
            for message, email_output in zip(messages, email_outputs):
                attachment_outputs = email_output.get("attachments") or []
                for attachment, attachment_output in zip(message.attachments, attachment_outputs):
                    if attachment.is_inline:
                        attachment_output["download_error"] = "inline_attachment_skipped"
                        continue
                    if not attachment.attachment_id and attachment.embedded_data is None:
                        attachment_output["download_error"] = "missing_attachment_id"
                        continue
                    if attachment.size is not None and attachment.size < 0:
                        attachment_output["download_error"] = "invalid_size"
                        continue
                    if file_count >= count_limit:
                        attachment_output["download_error"] = "file_count_limit"
                        continue
                    if attachment.size is not None:
                        if attachment.size > file_limit:
                            attachment_output["download_error"] = "file_size_limit"
                            continue
                        if run_size + attachment.size > run_limit:
                            attachment_output["download_error"] = "run_size_limit"
                            continue

                    try:
                        if attachment.embedded_data is not None:
                            content = email.gmail.decode_attachment_data(
                                attachment.embedded_data, max_size_bytes=file_limit
                            )
                        else:
                            content = await email.download_attachment(
                                email_client=self.email_client,
                                access_token=access_token,
                                message_id=message.id,
                                attachment_id=attachment.attachment_id or "",
                                max_size_bytes=file_limit,
                                client=client,
                            )
                    except (email.GmailAPIError, email.OutlookAPIError) as exc:
                        if exc.code == "reconnect_required":
                            safe_message = f"Reconnect the {self.email_client.title()} account to download attachments"
                            if isinstance(exc, email.GmailAPIError):
                                raise email.GmailAPIError(
                                    status=exc.status, code=exc.code, message=safe_message
                                ) from exc
                            raise email.OutlookAPIError(status=exc.status, code=exc.code, message=safe_message) from exc
                        attachment_output["download_error"] = (
                            "file_size_limit" if exc.code == "file_size_limit" else "download_failed"
                        )
                        continue

                    if len(content) > file_limit:
                        attachment_output["download_error"] = "file_size_limit"
                        continue
                    if run_size + len(content) > run_limit:
                        attachment_output["download_error"] = "run_size_limit"
                        continue

                    # Split before sanitizing: its 200-character cap would otherwise cut off a long name's extension.
                    raw_stem, raw_extension = os.path.splitext(attachment.name)
                    stem = sanitize_filename(raw_stem, default="attachment")
                    extension = sanitize_filename(raw_extension, default="")
                    extension = f".{extension}" if extension else ""
                    if extension.lower() == BROWSER_DOWNLOADING_SUFFIX:
                        # The download subsystem treats this suffix as an in-progress browser download.
                        stem, extension = f"{stem}_{extension[1:]}", ""
                    # sanitize_filename bounds characters, but the filesystem limit (255) is in bytes.
                    extension = extension.encode()[:32].decode(errors="ignore")
                    stem = stem.encode()[: 200 - len(extension.encode())].decode(errors="ignore") or "attachment"
                    if isinstance(loop_index, int) and not isinstance(loop_index, bool):
                        stem = f"{stem}-{loop_index + 1:04d}"
                    suffix = 1
                    saved_path: Path | None = None
                    while True:
                        filename = f"{stem}{extension}" if suffix == 1 else f"{stem}-{suffix:04d}{extension}"
                        path = download_dir / filename
                        file_created = False
                        try:
                            with path.open("xb") as file:
                                file_created = True
                                file.write(content)
                            saved_path = path
                            break
                        except FileExistsError:
                            suffix += 1
                        except OSError:
                            attachment_output["download_error"] = "save_failed"
                            if file_created:
                                try:
                                    path.unlink()
                                except FileNotFoundError:
                                    pass
                                except OSError:
                                    LOG.warning(
                                        "Failed to remove incomplete email attachment download",
                                        workflow_run_block_id=workflow_run_block_id,
                                    )
                            break
                    if saved_path is None:
                        continue
                    file_count += 1
                    run_size += len(content)
                    saved_names.add(saved_path.name)
                    attachment_output["file_name"] = saved_path.name
                    attachment_output["file_path"] = str(saved_path)

        registered_files = (
            await register_workflow_downloads(
                organization_id=organization_id,
                run_download_id=run_download_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
            )
            if saved_names
            else []
        )
        registered_by_name = {file.filename: file for file in registered_files if file.filename in saved_names}
        for email_output in email_outputs:
            for attachment_output in email_output.get("attachments") or []:
                file_info = registered_by_name.get(attachment_output.get("file_name"))
                if file_info:
                    attachment_output["url"] = file_info.url
        block_files = [file for file in registered_files if file.filename in saved_names]
        return email_outputs, block_files

    async def execute(
        self,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
        **kwargs: Any,
    ) -> BlockResult:
        workflow_run_context = self.get_workflow_run_context(workflow_run_id)

        try:
            self._render_templates(workflow_run_context)
        except Exception as e:
            return await self._template_format_failure_result(
                e,
                render_failure_reason(
                    "Failed to format jinja template:", e, self.secret_redactor(workflow_run_context)
                ),
                workflow_run_context,
                workflow_run_id,
                workflow_run_block_id,
                organization_id,
            )

        if self.email_client not in {"gmail", "outlook"}:
            return await self._failure(
                failure_reason=f"Unsupported email_client: {self.email_client}",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        if not self.credential_id:
            return await self._failure(
                failure_reason="credential_id is required",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        effective_org_id = organization_id or workflow_run_context.organization_id
        if not effective_org_id:
            return await self._failure(
                failure_reason="organization_id is required to load email credentials",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        if self.email_client == "gmail":
            creds = await app.AGENT_FUNCTION.get_google_workspace_credentials(
                organization_id=effective_org_id,
                credential_id=self.credential_id,
                required_scopes=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
            )
            access_token = creds.token if creds else None
            reconnect_label = "Gmail"
        else:
            access_token = await app.AGENT_FUNCTION.get_microsoft_credentials(
                organization_id=effective_org_id,
                credential_id=self.credential_id,
                required_scopes=["Mail.Read"],
            )
            reconnect_label = "Outlook"
        if not access_token:
            return await self._failure(
                failure_reason=f"Reconnect the {reconnect_label} account: no valid access token",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        folder_used = self.folder or ("INBOX" if self.email_client == "gmail" else "inbox")
        try:
            candidates = await email.list_folder_messages(
                email_client=self.email_client,
                access_token=access_token,
                folder=folder_used,
                sender=self.sender,
                subject=self.subject,
                newer_than_days=self.newer_than_days,
                max_results=self.max_results,
                include_body=self.include_body,
                include_attachments=self.download_attachments,
            )
        except email.GmailAPIError as e:
            return await self._api_failure(
                provider="Gmail",
                exc=e,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        except email.OutlookAPIError as e:
            return await self._api_failure(
                provider="Outlook",
                exc=e,
                workflow_run_context=workflow_run_context,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )

        criteria = self.prompt.strip() if self.prompt else ""
        matched = (
            await self._match_emails(candidates, criteria=criteria, organization_id=effective_org_id)
            if criteria
            else candidates
        )
        emails = [message.model_dump(mode="json") for message in matched]
        downloaded_files: list[FileInfo] = []
        if self.download_attachments:
            try:
                emails, downloaded_files = await self._download_matched_attachments(
                    matched,
                    access_token=access_token,
                    organization_id=effective_org_id,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                )
            except email.GmailAPIError as e:
                return await self._api_failure(
                    provider="Gmail",
                    exc=e,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
            except email.OutlookAPIError as e:
                return await self._api_failure(
                    provider="Outlook",
                    exc=e,
                    workflow_run_context=workflow_run_context,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    organization_id=organization_id,
                )
        output_data: dict[str, Any] = {
            "email_client": self.email_client,
            "folder": folder_used,
            "candidate_count": len(candidates),
            "matched_count": len(matched),
            "emails": emails,
        }
        if self.download_attachments:
            output_data["downloaded_files"] = [file.model_dump() for file in downloaded_files]
            output_data["downloaded_file_urls"] = [file.url for file in downloaded_files]
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, output_data)
        return await self.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value=output_data,
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
        )

    async def _api_failure(
        self,
        *,
        provider: str,
        exc: email.GmailAPIError | email.OutlookAPIError,
        workflow_run_context: WorkflowRunContext,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
    ) -> BlockResult:
        error_data = {"status_code": exc.status, "code": exc.code, "error": exc.message}
        await self.record_output_parameter_value(workflow_run_context, workflow_run_id, error_data)
        if exc.code == "reconnect_required":
            failure_reason = f"Reconnect the {provider} account: {exc.message}"
        else:
            failure_reason = f"{provider} email fetch failed (HTTP {exc.status}): {exc.message}"
        return await self._failure(
            failure_reason=failure_reason,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            output_parameter_value=error_data,
        )
