from __future__ import annotations

from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    build_transcript_context,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.workflow_change_summary import WorkflowChangeKind, summarize_user_workflow_change
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage, WorkflowCopilotChatSender
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()

TurnContextSection = Literal[
    "current_workflow",
    "latest_assistant_proposal",
    "latest_run_result",
    "credential_metadata",
]


class TurnContextOmission(BaseModel):
    context_key: TurnContextSection
    reason: Literal["unavailable", "truncated_to_budget"]
    detail: str = ""


class WorkflowContext(BaseModel):
    yaml: str
    original_chars: int
    truncated: bool = False


class ProposalContext(BaseModel):
    latest_assistant_proposal: str
    original_chars: int
    truncated: bool = False


class WorkflowChangeContext(BaseModel):
    kind: str
    rendered_summary: str
    structural_diff_unavailable: bool = False


class RunnableDraftContext(BaseModel):
    rendered_summary: str
    block_labels: list[str] = Field(default_factory=list)


class TranscriptContext(BaseModel):
    earliest_user_turn: str
    latest_prior_user_turn: str
    latest_assistant_turn: str
    retained_history: str
    omitted_any: bool


class RunContext(BaseModel):
    summary: str
    original_chars: int
    truncated: bool = False


class CredentialMetadata(BaseModel):
    credential_id: str
    name: str
    credential_type: str
    vault_type: str | None = None
    tested_url: str | None = None
    browser_profile_id: str | None = None


class CredentialContext(BaseModel):
    requested_refs: list[str] = Field(default_factory=list)
    invalid_credential_ids: list[str] = Field(default_factory=list)
    credentials: list[CredentialMetadata] = Field(default_factory=list)
    omitted_credential_count: int = 0


class TurnContextPacket(BaseModel):
    workflow_context: WorkflowContext | None = None
    proposal_context: ProposalContext | None = None
    workflow_change_context: WorkflowChangeContext | None = None
    runnable_draft_context: RunnableDraftContext | None = None
    transcript_context: TranscriptContext
    run_context: RunContext | None = None
    credential_context: CredentialContext | None = None
    omissions: list[TurnContextOmission] = Field(default_factory=list)

    def to_trace_data(self) -> dict[str, Any]:
        section_fields = (
            "workflow_context",
            "proposal_context",
            "workflow_change_context",
            "runnable_draft_context",
            "run_context",
            "credential_context",
        )
        return {
            "sections": [field for field in section_fields if getattr(self, field) is not None],
            "omissions": [omission.context_key for omission in self.omissions],
            "omission_reasons": [omission.reason for omission in self.omissions],
            "workflow_truncated": bool(self.workflow_context and self.workflow_context.truncated),
            "proposal_truncated": bool(self.proposal_context and self.proposal_context.truncated),
            "run_truncated": bool(self.run_context and self.run_context.truncated),
            "workflow_change_kind": self.workflow_change_context.kind if self.workflow_change_context else None,
        }


class TurnContextInputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    request_policy: RequestPolicy
    user_message: str = ""
    workflow_yaml: str = ""
    prior_workflow_yaml: str = ""
    chat_history: list[WorkflowCopilotChatHistoryMessage] = Field(default_factory=list)
    debug_run_info_text: str = ""


def _dedupe_nonempty(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _bounded_text(value: str, char_budget: int) -> tuple[str, int, bool]:
    redacted = redact_raw_secrets_for_prompt(value or "")
    original_chars = len(redacted)
    if original_chars <= char_budget:
        return redacted, original_chars, False
    suffix = "...<truncated>"
    if char_budget <= len(suffix):
        return redacted[:char_budget], original_chars, True
    return redacted[: char_budget - len(suffix)].rstrip() + suffix, original_chars, True


def _top_level_block_labels(workflow_yaml: str) -> list[str]:
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict):
        return []
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return []
    blocks = definition.get("blocks")
    if not isinstance(blocks, list):
        return []
    labels: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            label = block.get("label")
            if isinstance(label, str) and label:
                labels.append(label)
    return labels


def _latest_assistant_turn(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> str:
    for message in reversed(chat_history):
        if message.sender == WorkflowCopilotChatSender.AI and (message.content or "").strip():
            return message.content
    return ""


def _safe_credential_metadata(credential: Credential) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=credential.credential_id,
        name=credential.name,
        credential_type=str(credential.credential_type),
        vault_type=str(credential.vault_type) if credential.vault_type else None,
        tested_url=credential.tested_url,
        browser_profile_id=credential.browser_profile_id,
    )


class TurnContextAssembler:
    def __init__(
        self,
        *,
        workflow_char_budget: int = 12_000,
        proposal_char_budget: int = 2_000,
        run_char_budget: int = 4_000,
        credential_count_budget: int = 20,
    ) -> None:
        self.workflow_char_budget = workflow_char_budget
        self.proposal_char_budget = proposal_char_budget
        self.run_char_budget = run_char_budget
        self.credential_count_budget = credential_count_budget

    def assemble(self, inputs: TurnContextInputs) -> TurnContextPacket:
        omissions: list[TurnContextOmission] = []
        transcript = build_transcript_context(inputs.chat_history, inputs.user_message)
        transcript_context = TranscriptContext(
            earliest_user_turn=transcript.earliest_user_turn,
            latest_prior_user_turn=transcript.latest_prior_user_turn,
            latest_assistant_turn=transcript.latest_assistant_turn,
            retained_history=transcript.retained_history,
            omitted_any=transcript.omitted_any,
        )

        workflow_context: WorkflowContext | None = None
        proposal_context: ProposalContext | None = None
        workflow_change_context: WorkflowChangeContext | None = None
        run_context: RunContext | None = None

        if inputs.workflow_yaml.strip():
            yaml_text, original_chars, truncated = _bounded_text(inputs.workflow_yaml, self.workflow_char_budget)
            workflow_context = WorkflowContext(
                yaml=yaml_text,
                original_chars=original_chars,
                truncated=truncated,
            )
            if truncated:
                omissions.append(
                    TurnContextOmission(
                        context_key="current_workflow",
                        reason="truncated_to_budget",
                        detail=f"workflow_yaml exceeded {self.workflow_char_budget} chars",
                    )
                )
        else:
            omissions.append(TurnContextOmission(context_key="current_workflow", reason="unavailable"))

        latest_proposal = _latest_assistant_turn(inputs.chat_history)
        if latest_proposal:
            proposal, original_chars, truncated = _bounded_text(latest_proposal, self.proposal_char_budget)
            proposal_context = ProposalContext(
                latest_assistant_proposal=proposal,
                original_chars=original_chars,
                truncated=truncated,
            )
            if truncated:
                omissions.append(
                    TurnContextOmission(
                        context_key="latest_assistant_proposal",
                        reason="truncated_to_budget",
                        detail=f"latest assistant proposal exceeded {self.proposal_char_budget} chars",
                    )
                )

        if inputs.prior_workflow_yaml.strip():
            change_summary = summarize_user_workflow_change(
                prior_yaml=inputs.prior_workflow_yaml,
                current_yaml=inputs.workflow_yaml,
            )
            # Only surface the section when the user actually edited the workflow.
            # An unchanged or first-turn baseline carries no signal the agent acts on.
            if change_summary.kind is WorkflowChangeKind.USER_MODIFIED_SINCE_LAST_TURN:
                workflow_change_context = WorkflowChangeContext(
                    kind=change_summary.kind.value,
                    rendered_summary=change_summary.render_prompt_block(),
                    structural_diff_unavailable=change_summary.structural_diff_unavailable,
                )

        runnable_draft_context = self._runnable_draft_context(inputs)

        if inputs.debug_run_info_text.strip():
            summary, original_chars, truncated = _bounded_text(inputs.debug_run_info_text, self.run_char_budget)
            run_context = RunContext(summary=summary, original_chars=original_chars, truncated=truncated)
            if truncated:
                omissions.append(
                    TurnContextOmission(
                        context_key="latest_run_result",
                        reason="truncated_to_budget",
                        detail=f"run context exceeded {self.run_char_budget} chars",
                    )
                )
        else:
            omissions.append(TurnContextOmission(context_key="latest_run_result", reason="unavailable"))

        credential_context, credential_omissions = self._credential_context(inputs.request_policy)
        omissions.extend(credential_omissions)

        packet = TurnContextPacket(
            workflow_context=workflow_context,
            proposal_context=proposal_context,
            workflow_change_context=workflow_change_context,
            runnable_draft_context=runnable_draft_context,
            transcript_context=transcript_context,
            run_context=run_context,
            credential_context=credential_context,
            omissions=omissions,
        )

        LOG.info(
            "assembled copilot turn context packet",
            **{f"turn_context_{key}": value for key, value in packet.to_trace_data().items()},
        )
        return packet

    def _runnable_draft_context(self, inputs: TurnContextInputs) -> RunnableDraftContext | None:
        if not inputs.request_policy.allow_run_blocks:
            return None
        if _top_level_block_labels(inputs.workflow_yaml):
            return None
        labels = _top_level_block_labels(inputs.prior_workflow_yaml)
        if not labels:
            return None
        labels_csv = ", ".join(labels)
        summary = (
            "A prior turn proposed a workflow draft that was never committed to the canvas, so the"
            " CURRENT WORKFLOW YAML above is empty. The user is asking to run/re-test that draft. To"
            " run it, call run_blocks_and_collect_debug with these block labels: "
            f"{labels_csv}. Do not call update_and_run_blocks and do not ask the user to rebuild it."
        )
        return RunnableDraftContext(rendered_summary=summary, block_labels=labels)

    def _credential_context(self, request_policy: RequestPolicy) -> tuple[CredentialContext, list[TurnContextOmission]]:
        omissions: list[TurnContextOmission] = []
        credentials = request_policy.resolved_credentials[: self.credential_count_budget]
        omitted_count = max(len(request_policy.resolved_credentials) - len(credentials), 0)
        if omitted_count:
            omissions.append(
                TurnContextOmission(
                    context_key="credential_metadata",
                    reason="truncated_to_budget",
                    detail=f"{omitted_count} credential metadata entries omitted",
                )
            )
        if not request_policy.resolved_credentials and not request_policy.credential_refs:
            omissions.append(TurnContextOmission(context_key="credential_metadata", reason="unavailable"))
        return CredentialContext(
            requested_refs=_dedupe_nonempty(request_policy.credential_refs),
            invalid_credential_ids=_dedupe_nonempty(request_policy.invalid_credential_ids),
            credentials=[_safe_credential_metadata(credential) for credential in credentials],
            omitted_credential_count=omitted_count,
        ), omissions
