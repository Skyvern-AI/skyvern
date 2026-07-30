"""Persisted, typed record of one workflow-copilot turn.

Lives under ``schemas/`` so chat-history schemas can embed it without pulling
in any ``copilot/`` business logic — derivation lives in
``skyvern/forge/sdk/copilot/turn_outcome.py``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResponseKind(StrEnum):
    BUILD = "build"
    CLARIFY = "clarify"
    DIAGNOSE = "diagnose"
    REFUSE = "refuse"
    RECOVER = "recover"


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


class TurnOutcome(BaseModel):
    # extra="ignore" so a rolling deploy that adds a new TurnOutcome field
    # does not make older readers silently treat freshly-written rows as None.
    model_config = ConfigDict(extra="ignore", frozen=True)

    turn_intent_summary: dict[str, Any] = Field(default_factory=dict)
    response_kind: ResponseKind
    reason_code: str = ""
    actuation_obligation_key: str = ""
    normalized_reply_signature: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    terminal_reason: str | None = None
    blocked_signatures: list[str] = Field(default_factory=list)
    copilot_effective_mode: Literal["ask", "build", "code"] | None = None
    copilot_code_available: bool = False
    copilot_last_code_build_failed: bool = False
    copilot_pending_capability: str | None = None
    copilot_turn_id: str | None = None
    unresolved_runtime_failure: UnresolvedRuntimeFailure | None = None
