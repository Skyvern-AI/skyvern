"""Typed state shared by Copilot budget enforcement, persistence, and tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from skyvern.forge.sdk.schemas.copilot_turn_outcome import TurnOutcome

BudgetExpirySource = Literal["deadline", "max_turns"]


@dataclass
class BudgetExpiryState:
    source: BudgetExpirySource | None = None
    drain_active: bool = False
    drain_attempted: bool = False
    drain_fingerprint: str | None = None
    report_produced: bool | None = None
    staged_draft_id: str | None = None
    hard_backstop_reached: bool = False

    def begin_drain(self) -> None:
        self.drain_attempted = True
        self.drain_active = True
        self.drain_fingerprint = uuid4().hex


class BudgetExpiryObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    budget_expired: Literal[True] = True
    source: BudgetExpirySource
    headroom: int
    staged_draft: str | None
    message: str = (
        "The normal turn budget has expired. headroom is the maximum number of model calls available to finish "
        "this turn, including this call; the hard time limit may stop it sooner. Your session history and any "
        "staged draft remain available. Choose how to use these calls. New build-test runs cannot be dispatched."
    )


class BudgetRunDenialData(TypedDict):
    budget_expired: Literal[True]
    run_dispatched: Literal[False]
    source: BudgetExpirySource | None


class BudgetRunDenial(TypedDict):
    ok: Literal[False]
    data: BudgetRunDenialData


def serialize_budget_expiry_observation(*, source: BudgetExpirySource, headroom: int, staged_draft: str | None) -> str:
    return BudgetExpiryObservation(
        source=source,
        headroom=headroom,
        staged_draft=staged_draft,
    ).model_dump_json()


def serialize_prior_budget_expiry(outcome: TurnOutcome | None) -> str | None:
    if outcome is None or not outcome.budget_expired or outcome.budget_expiry_source is None:
        return None
    return json.dumps(
        {
            "budget_expired": True,
            "source": outcome.budget_expiry_source,
            "report_produced": outcome.budget_expiry_report_produced,
            "staged_draft": outcome.budget_expiry_staged_draft_id,
            "drain_fingerprint": outcome.drain_fingerprint,
        },
        separators=(",", ":"),
    )


def budget_run_denial(state: BudgetExpiryState) -> BudgetRunDenial:
    return {
        "ok": False,
        "data": {
            "budget_expired": True,
            "run_dispatched": False,
            "source": state.source,
        },
    }
