import asyncio
import os
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import quote

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
from skyvern.schemas.emails import (
    CONNECTED_ACCOUNT_OWNER_UNCONFIRMED_REASON,
    CONNECTED_ACCOUNT_RESAVE_REASON,
    GmailHopRefusal,
)
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.services import email
from skyvern.services.email.gmail import _message_from_gmail
from skyvern.services.email.gmail_client import GMAIL_API_BASE, get_json

LOG = structlog.get_logger()


class EmailInboxBlock(Block):
    block_type: Literal[BlockType.EMAIL_INBOX] = BlockType.EMAIL_INBOX  # type: ignore

    email_client: Literal["gmail", "outlook"]
    credential_source: Literal["native", "nango"] = "native"
    connection_id: str | None = None
    credential_id: str | None = None
    message_id: str | None = None
    folder: str | None = None
    prompt: str | None = None
    sender: str | None = None
    subject: str | None = None
    newer_than_days: int | None = None
    max_results: int = 25
    include_body: bool = True
    download_attachments: bool = False
    parameters: list[PARAMETER_TYPE] = []

    TEMPLATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"credential_id", "folder", "prompt", "sender", "subject", "message_id"}
    )

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.credential_id and self.credential_source == "native":
            self.credential_id = self.render_templatable_field(
                "credential_id", self.credential_id, workflow_run_context
            )
        if self.message_id:
            self.message_id = self.render_templatable_field("message_id", self.message_id, workflow_run_context).strip()
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
        access_token: str | None,
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

                    # None means the bytes are streamed from a connected account straight into the file below.
                    content: bytes | None = None
                    try:
                        if attachment.embedded_data is not None:
                            content = email.gmail.decode_attachment_data(
                                attachment.embedded_data, max_size_bytes=file_limit
                            )
                        elif self.credential_source == "nango":
                            if not attachment.download_grant:
                                attachment_output["download_error"] = "missing_attachment_id"
                                continue
                        else:
                            content = await email.download_attachment(
                                email_client=self.email_client,
                                access_token=access_token or "",
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

                    if content is not None and len(content) > file_limit:
                        attachment_output["download_error"] = "file_size_limit"
                        continue
                    if content is not None and run_size + len(content) > run_limit:
                        attachment_output["download_error"] = "run_size_limit"
                        continue
                    max_bytes = min(file_limit, run_limit - run_size)

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
                                if content is None:
                                    written = await app.AGENT_FUNCTION.download_gmail_attachment_for_run(
                                        organization_id=organization_id,
                                        workflow_run_id=workflow_run_id,
                                        workflow_run_block_id=workflow_run_block_id,
                                        connection_id=self.connection_id or "",
                                        hop_token=workflow_run_context.get_gmail_hop_token(self.label),
                                        message_id=message.id,
                                        attachment_id=attachment.attachment_id or "",
                                        download_grant=attachment.download_grant or "",
                                        max_bytes=max_bytes,
                                        destination=file,
                                    )
                                    # A stream cut short can still end cleanly; the listed size catches it.
                                    if attachment.size is not None and written != attachment.size:
                                        raise email.GmailAPIError(
                                            status=502, code="download_failed", message="Incomplete attachment"
                                        )
                                else:
                                    file.write(content)
                                    written = len(content)
                            saved_path = path
                            break
                        except FileExistsError:
                            suffix += 1
                        except (OSError, email.GmailAPIError) as exc:
                            if not isinstance(exc, email.GmailAPIError):
                                attachment_output["download_error"] = "save_failed"
                            elif exc.code == "file_size_limit":
                                # ponytail: the stream stops at the tighter bound, so name the limit that set it.
                                attachment_output["download_error"] = (
                                    "file_size_limit" if max_bytes == file_limit else "run_size_limit"
                                )
                            else:
                                attachment_output["download_error"] = "download_failed"
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
                            if isinstance(exc, email.GmailAPIError) and exc.code == "reconnect_required":
                                raise email.GmailAPIError(
                                    status=exc.status,
                                    code=exc.code,
                                    message="Reconnect the Gmail account to download attachments",
                                ) from exc
                            break
                        except BaseException:
                            # A cancelled run must not leave a partial file under a final name: run cleanup
                            # would persist it as a complete download.
                            if file_created:
                                try:
                                    path.unlink(missing_ok=True)
                                except OSError:
                                    LOG.warning(
                                        "Failed to remove incomplete email attachment download",
                                        workflow_run_block_id=workflow_run_block_id,
                                    )
                            raise
                    if saved_path is None:
                        continue
                    file_count += 1
                    run_size += written
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
        if self.message_id and (self.email_client != "gmail" or self.credential_source != "native"):
            return await self._failure(
                failure_reason="Fetching by message ID requires a native Gmail connection",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
            )
        if self.credential_source == "native" and not self.credential_id:
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

        access_token: str | None = None
        folder_used = None if self.message_id else self.folder or ("INBOX" if self.email_client == "gmail" else "inbox")
        try:
            if self.credential_source == "nango":
                if (
                    self.email_client != "gmail"
                    or not self.connection_id
                    or any(marker in self.connection_id for marker in ("{{", "{%", "{#"))
                ):
                    raise email.GmailAPIError(
                        status=404, code="integration_unavailable", message="Gmail integration unavailable"
                    )
                assert folder_used is not None
                if not workflow_run_context.get_gmail_hop_token(self.label):
                    refusal = workflow_run_context.get_gmail_hop_refusal(self.label)
                    if refusal == GmailHopRefusal.reconnect:
                        raise email.GmailAPIError(
                            status=403,
                            code="reconnect_required",
                            message="reconnect it on the Integrations page, then select it again in this block",
                        )
                    if refusal == GmailHopRefusal.resave_required:
                        raise email.GmailAPIError(
                            status=403, code="resave_required", message=CONNECTED_ACCOUNT_RESAVE_REASON
                        )
                    if refusal == GmailHopRefusal.owner_unconfirmed:
                        raise email.GmailAPIError(
                            status=403, code="owner_unconfirmed", message=CONNECTED_ACCOUNT_OWNER_UNCONFIRMED_REASON
                        )
                data = await app.AGENT_FUNCTION.execute_gmail_inbox_for_run(
                    organization_id=effective_org_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                    connection_id=self.connection_id,
                    hop_token=workflow_run_context.get_gmail_hop_token(self.label),
                    folder=folder_used,
                    sender=self.sender,
                    subject=self.subject,
                    newer_than_days=max(1, self.newer_than_days) if self.newer_than_days is not None else None,
                    max_results=max(1, min(self.max_results, 100)),
                    include_body=self.include_body,
                    include_attachments=self.download_attachments,
                )
                try:
                    candidates = [email.EmailMessage.model_validate(item) for item in data]
                except Exception:  # noqa: BLE001 - validation errors may contain message content.
                    raise email.GmailAPIError(
                        status=502, code="provider_unavailable", message="Gmail read unavailable"
                    ) from None
            else:
                assert self.credential_id is not None
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
                if self.message_id:
                    async with httpx.AsyncClient() as client:
                        raw = await get_json(
                            client,
                            f"{GMAIL_API_BASE}/users/me/messages/{quote(self.message_id, safe='')}",
                            access_token=access_token,
                            params={"format": "full" if self.include_body or self.download_attachments else "metadata"},
                        )
                    message = _message_from_gmail(raw, self.include_body, include_attachments=self.download_attachments)
                    if message is None:
                        raise email.GmailAPIError(status=502, code=None, message="Invalid Gmail response")
                    candidates = [message]
                else:
                    assert folder_used is not None
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

        # A connected-account message too large to read in full is left out, but named in the output so the skip
        # is visible; the id stays out of logs.
        candidate_count = len(candidates)
        skipped = [c for c in candidates if c.attachment_list_error == "message_too_large"]
        if skipped:
            LOG.warning(
                "Skipped Gmail messages over the connected-account read limit",
                workflow_run_block_id=workflow_run_block_id,
                skipped_count=len(skipped),
            )
            candidates = [c for c in candidates if c.attachment_list_error != "message_too_large"]
        criteria = self.prompt.strip() if self.prompt else ""
        matched = (
            await self._match_emails(candidates, criteria=criteria, organization_id=effective_org_id)
            if criteria
            else candidates
        )
        emails = [message.model_dump(mode="json") for message in matched]
        downloaded_files: list[FileInfo] = []
        if self.download_attachments:
            assert access_token is not None or self.credential_source == "nango"
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
            "candidate_count": candidate_count,
            "matched_count": len(matched),
            "emails": emails,
        }
        if skipped:
            output_data["skipped_messages"] = [{"id": c.id, "reason": "message_too_large"} for c in skipped]
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
        elif exc.code in {"resave_required", "owner_unconfirmed"}:
            failure_reason = exc.message
        else:
            failure_reason = f"{provider} email fetch failed (HTTP {exc.status}): {exc.message}"
        return await self._failure(
            failure_reason=failure_reason,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            output_parameter_value=error_data,
        )
