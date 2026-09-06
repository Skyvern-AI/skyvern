"""Persisted, typed record of one workflow-copilot turn.

Lives under ``schemas/`` so chat-history schemas can embed it without pulling
in any ``copilot/`` business logic — derivation lives in
``skyvern/forge/sdk/copilot/turn_outcome.py``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResponseKind(StrEnum):
    ANSWER = "answer"
    BUILD = "build"
    CLARIFY = "clarify"
    DIAGNOSE = "diagnose"
    REFUSE = "refuse"
    RECOVER = "recover"


class OutputPolicyReason(StrEnum):
    RAW_SECRET_LEAK = "raw_secret_leak"
    UNAPPROVED_CREDENTIAL_REFERENCE = "unapproved_credential_reference"
    CREDENTIAL_SCOPE_BROADENED = "credential_scope_broadened"
    UNBACKED_WORKFLOW_DELIVERY_CLAIM = "unbacked_workflow_delivery_claim"
    MISSING_PROPOSAL_STATE = "missing_proposal_state"
    PERSISTENCE_STATE_MISMATCH = "persistence_state_mismatch"
    OUTPUT_POLICY_CONTEXT_MISSING = "output_policy_context_missing"
    INTERNAL_BLOCK_TAXONOMY_LEAK = "internal_block_taxonomy_leak"
    INTERNAL_CLASSIFIER_VOCAB_LEAK = "internal_classifier_vocab_leak"
    SELF_PRESCRIPTIVE_PHRASE_LEAK = "self_prescriptive_phrase_leak"
    WORKFLOW_YAML_IN_REPLY = "workflow_yaml_in_reply"


_UNSAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_\- ]")


class UnresolvedRuntimeFailure(BaseModel):
    """A failure a successful turn could not clear, recorded so the outcome is gradeable after the
    fact; the reply text derives independently, and nothing keys success or verification on this."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    workflow_run_id: str
    block_label: str

    @field_validator("workflow_run_id", "block_label")
    @classmethod
    def _bare_identifier(cls, value: str) -> str:
        # Both fields are model-authored and reach the chat reply and the history API, so they are
        # reduced to bounded identifiers here rather than at each surface that renders them.
        return _UNSAFE_IDENTIFIER_RE.sub("", value)[:80].strip()


class ConnectedAccountChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    connection_id: str
    name: str
    state: str
    email_address: str | None = None


class ConnectedAccountChoiceReference(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    connection_id: str


CopilotCancelSource = Literal["escape_key", "stop_button", "api"]
PersistedCopilotComposerMode = Literal["ask", "build", "code"]


class TurnOutcome(BaseModel):
    # extra="ignore" so a rolling deploy that adds a new TurnOutcome field
    # does not make older readers silently treat freshly-written rows as None.
    model_config = ConfigDict(extra="ignore", frozen=True)

    response_kind: ResponseKind
    reason_code: str = ""
    output_policy_reasons: list[OutputPolicyReason] = Field(default_factory=list)
    actuation_obligation_key: str = ""
    normalized_reply_signature: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    terminal_reason: str | None = None
    budget_expired: bool = False
    budget_expiry_source: Literal["deadline", "max_turns"] | None = None
    budget_expiry_report_produced: bool | None = None
    budget_expiry_staged_draft_id: str | None = None
    drain_fingerprint: str | None = None
    # Sits beside terminal_reason so cancel volume groups by gesture in the same
    # query that finds the cancels. None when the request named no source.
    cancel_source: CopilotCancelSource | None = None
    blocked_signatures: list[str] = Field(default_factory=list)
    # Durable discriminator for the post-graduation runtime. Historical legacy
    # turns may carry the same effective mode, so analytics must not infer the
    # runtime from copilot_effective_mode alone.
    copilot_runtime: Literal["agent"] | None = None
    copilot_effective_mode: PersistedCopilotComposerMode | None = None
    copilot_code_available: bool = False
    copilot_last_code_build_failed: bool = False
    copilot_pending_capability: str | None = None
    copilot_turn_id: str | None = None
    idempotency_digest: str | None = None
    unresolved_runtime_failure: UnresolvedRuntimeFailure | None = None
    connected_account_choices: list[ConnectedAccountChoice] | None = None

    @field_validator("output_policy_reasons", mode="before")
    @classmethod
    def _drop_unknown_output_policy_reasons(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        known_reasons: list[OutputPolicyReason] = []
        for raw_reason in value:
            try:
                known_reasons.append(OutputPolicyReason(raw_reason))
            except (TypeError, ValueError):
                continue
        return known_reasons
