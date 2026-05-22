from __future__ import annotations

from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    build_transcript_context,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.turn_intent import RequiredContextKey, TurnIntent, TurnIntentMode
from skyvern.forge.sdk.copilot.workflow_change_summary import WorkflowChangeKind, summarize_user_workflow_change
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage, WorkflowCopilotChatSender

LOG = structlog.get_logger()

_WORKFLOW_MODES = {
    TurnIntentMode.BUILD,
    TurnIntentMode.EDIT,
    TurnIntentMode.DIAGNOSE,
    TurnIntentMode.DRAFT_ONLY,
    TurnIntentMode.CLARIFY,
}
_RUN_MODES = {TurnIntentMode.DIAGNOSE}  # Additional run-evidence modes can join here.


class TurnContextOmission(BaseModel):
    context_key: RequiredContextKey
    reason: Literal["unavailable", "truncated_to_budget", "not_implemented"]
    required: bool = True
    detail: str = ""


class WorkflowContext(BaseModel):
    yaml: str
    source: Literal["current", "proposed"] = "current"
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


class DocsContext(BaseModel):
    # v1 placeholder; reserves a typed slot for future docs retrieval output.
    status: Literal["empty_hook"] = "empty_hook"


class TurnContextPacket(BaseModel):
    turn_intent_summary: dict[str, Any]
    workflow_context: WorkflowContext | None = None
    proposal_context: ProposalContext | None = None
    workflow_change_context: WorkflowChangeContext | None = None
    transcript_context: TranscriptContext
    run_context: RunContext | None = None
    credential_context: CredentialContext | None = None
    docs_context: DocsContext | None = None
    omissions: list[TurnContextOmission] = Field(default_factory=list)

    def to_trace_data(self) -> dict[str, Any]:
        section_fields = (
            "workflow_context",
            "proposal_context",
            "workflow_change_context",
            "run_context",
            "credential_context",
            "docs_context",
        )
        return {
            "mode": self.turn_intent_summary.get("mode"),
            "sections": [field for field in section_fields if getattr(self, field) is not None],
            "omissions": [omission.context_key.value for omission in self.omissions],
            "omission_reasons": [omission.reason for omission in self.omissions],
            "workflow_truncated": bool(self.workflow_context and self.workflow_context.truncated),
            "proposal_truncated": bool(self.proposal_context and self.proposal_context.truncated),
            "run_truncated": bool(self.run_context and self.run_context.truncated),
            "workflow_change_kind": self.workflow_change_context.kind if self.workflow_change_context else None,
        }


class TurnContextInputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    turn_intent: TurnIntent
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
        required = set(inputs.turn_intent.required_context)
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
        credential_context: CredentialContext | None = None
        docs_context: DocsContext | None = None

        if self._should_include_workflow(inputs.turn_intent, required):
            workflow_key = (
                RequiredContextKey.PROPOSED_WORKFLOW
                if RequiredContextKey.PROPOSED_WORKFLOW in required
                else RequiredContextKey.CURRENT_WORKFLOW
            )
            if inputs.workflow_yaml.strip():
                yaml_text, original_chars, truncated = _bounded_text(inputs.workflow_yaml, self.workflow_char_budget)
                # The caller controls whether this is current or proposed workflow YAML.
                workflow_context = WorkflowContext(
                    yaml=yaml_text,
                    source="proposed" if workflow_key == RequiredContextKey.PROPOSED_WORKFLOW else "current",
                    original_chars=original_chars,
                    truncated=truncated,
                )
                if truncated:
                    omissions.append(
                        TurnContextOmission(
                            context_key=workflow_key,
                            reason="truncated_to_budget",
                            detail=f"workflow_yaml exceeded {self.workflow_char_budget} chars",
                        )
                    )
            elif workflow_key in required:
                omissions.append(TurnContextOmission(context_key=workflow_key, reason="unavailable"))

        if RequiredContextKey.LATEST_ASSISTANT_PROPOSAL in required:
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
                            context_key=RequiredContextKey.LATEST_ASSISTANT_PROPOSAL,
                            reason="truncated_to_budget",
                            detail=f"latest assistant proposal exceeded {self.proposal_char_budget} chars",
                        )
                    )
            else:
                omissions.append(
                    TurnContextOmission(
                        context_key=RequiredContextKey.LATEST_ASSISTANT_PROPOSAL,
                        reason="unavailable",
                    )
                )

        if RequiredContextKey.WORKFLOW_CHANGE in required and inputs.prior_workflow_yaml.strip():
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

        if self._should_include_run_context(inputs.turn_intent, required):
            if inputs.debug_run_info_text.strip():
                summary, original_chars, truncated = _bounded_text(inputs.debug_run_info_text, self.run_char_budget)
                run_context = RunContext(summary=summary, original_chars=original_chars, truncated=truncated)
                if truncated:
                    omissions.append(
                        TurnContextOmission(
                            context_key=RequiredContextKey.LATEST_RUN_RESULT,
                            reason="truncated_to_budget",
                            detail=f"run context exceeded {self.run_char_budget} chars",
                        )
                    )
            elif RequiredContextKey.LATEST_RUN_RESULT in required:
                omissions.append(
                    TurnContextOmission(context_key=RequiredContextKey.LATEST_RUN_RESULT, reason="unavailable")
                )

        if RequiredContextKey.CREDENTIAL_METADATA in required:
            credential_context, credential_omissions = self._credential_context(inputs.request_policy)
            omissions.extend(credential_omissions)

        if RequiredContextKey.DOCS_CONTEXT in required:
            docs_context = DocsContext()

        if RequiredContextKey.BROWSER_STATE in required:
            omissions.append(
                TurnContextOmission(
                    context_key=RequiredContextKey.BROWSER_STATE,
                    reason="not_implemented",
                    detail="browser state context packet section is reserved for a future assembler revision",
                )
            )

        packet = TurnContextPacket(
            turn_intent_summary=inputs.turn_intent.to_trace_data(),
            workflow_context=workflow_context,
            proposal_context=proposal_context,
            workflow_change_context=workflow_change_context,
            transcript_context=transcript_context,
            run_context=run_context,
            credential_context=credential_context,
            docs_context=docs_context,
            omissions=omissions,
        )

        LOG.info(
            "assembled copilot turn context packet",
            **{f"turn_context_{key}": value for key, value in packet.to_trace_data().items()},
        )
        return packet

    def _should_include_workflow(self, intent: TurnIntent, required: set[RequiredContextKey]) -> bool:
        if intent.mode == TurnIntentMode.DOCS_ANSWER:
            return False
        # Edit-capable turns still need workflow context even when the shadow classifier
        # did not include a specific workflow key yet.
        return bool(required & {RequiredContextKey.CURRENT_WORKFLOW, RequiredContextKey.PROPOSED_WORKFLOW}) or (
            intent.mode in _WORKFLOW_MODES and intent.authority.may_update_workflow
        )

    def _should_include_run_context(self, intent: TurnIntent, required: set[RequiredContextKey]) -> bool:
        if intent.mode == TurnIntentMode.DOCS_ANSWER:
            return False
        return RequiredContextKey.LATEST_RUN_RESULT in required or intent.mode in _RUN_MODES

    def _credential_context(self, request_policy: RequestPolicy) -> tuple[CredentialContext, list[TurnContextOmission]]:
        omissions: list[TurnContextOmission] = []
        credentials = request_policy.resolved_credentials[: self.credential_count_budget]
        omitted_count = max(len(request_policy.resolved_credentials) - len(credentials), 0)
        if omitted_count:
            omissions.append(
                TurnContextOmission(
                    context_key=RequiredContextKey.CREDENTIAL_METADATA,
                    reason="truncated_to_budget",
                    detail=f"{omitted_count} credential metadata entries omitted",
                )
            )
        if not request_policy.resolved_credentials and not request_policy.credential_refs:
            omissions.append(
                TurnContextOmission(context_key=RequiredContextKey.CREDENTIAL_METADATA, reason="unavailable")
            )
        return CredentialContext(
            requested_refs=_dedupe_nonempty(request_policy.credential_refs),
            invalid_credential_ids=_dedupe_nonempty(request_policy.invalid_credential_ids),
            credentials=[_safe_credential_metadata(credential) for credential in credentials],
            omitted_credential_count=omitted_count,
        ), omissions
