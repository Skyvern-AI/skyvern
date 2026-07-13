import asyncio
from typing import Any, Literal

from skyvern.forge import app
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import Block
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.services import email


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
    parameters: list[PARAMETER_TYPE] = []

    def get_all_parameters(self, workflow_run_id: str) -> list[PARAMETER_TYPE]:
        return self.parameters

    def _render_templates(self, workflow_run_context: WorkflowRunContext) -> None:
        if self.credential_id:
            self.credential_id = self.format_block_parameter_template_from_workflow_run_context(
                self.credential_id, workflow_run_context
            )
        if self.folder:
            self.folder = self.format_block_parameter_template_from_workflow_run_context(
                self.folder, workflow_run_context
            )
        if self.prompt:
            self.prompt = self.format_block_parameter_template_from_workflow_run_context(
                self.prompt, workflow_run_context
            )
        if self.sender:
            self.sender = self.format_block_parameter_template_from_workflow_run_context(
                self.sender, workflow_run_context
            )
        if self.subject:
            self.subject = self.format_block_parameter_template_from_workflow_run_context(
                self.subject, workflow_run_context
            )

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
            return await self._failure(
                failure_reason=f"Failed to format jinja template: {str(e)}",
                workflow_run_block_id=workflow_run_block_id,
                organization_id=organization_id,
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
        output_data: dict[str, Any] = {
            "email_client": self.email_client,
            "folder": folder_used,
            "candidate_count": len(candidates),
            "matched_count": len(matched),
            "emails": [message.model_dump(mode="json") for message in matched],
        }
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
