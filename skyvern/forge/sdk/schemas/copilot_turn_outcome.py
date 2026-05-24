"""Persisted, typed record of one workflow-copilot turn.

Lives under ``schemas/`` so chat-history schemas can embed it without pulling
in any ``copilot/`` business logic — derivation lives in
``skyvern/forge/sdk/copilot/turn_outcome.py``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResponseKind(StrEnum):
    BUILD = "build"
    CLARIFY = "clarify"
    DIAGNOSE = "diagnose"
    REFUSE = "refuse"
    RECOVER = "recover"


class TurnOutcome(BaseModel):
    # extra="ignore" so a rolling deploy that adds a new TurnOutcome field
    # does not make older readers silently treat freshly-written rows as None.
    model_config = ConfigDict(extra="ignore", frozen=True)

    turn_intent_summary: dict[str, Any] = Field(default_factory=dict)
    response_kind: ResponseKind
    reason_code: str = ""
    normalized_reply_signature: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    terminal_reason: str | None = None
    blocked_signatures: list[str] = Field(default_factory=list)
