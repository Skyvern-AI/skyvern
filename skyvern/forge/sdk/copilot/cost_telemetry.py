from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from skyvern.forge.sdk.api.llm.copilot_model_usage import CopilotModelUsageEvent, copilot_usage_observer


@dataclass
class CopilotCostLedger:
    observed_call_count: int = 0
    priced_call_count: int = 0
    known_cost_usd: float = 0.0
    pricing_sources: set[str] = field(default_factory=set)
    active: bool = True

    def record(self, event: CopilotModelUsageEvent) -> None:
        if not self.active:
            return
        self.observed_call_count += 1
        cost = event.cost
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
            or event.cost_source not in {"provider_reported", "litellm_estimate"}
        ):
            return
        next_cost = self.known_cost_usd + cost
        if not math.isfinite(next_cost):
            return
        self.priced_call_count += 1
        self.known_cost_usd = next_cost
        self.pricing_sources.add(event.cost_source)

    def snapshot(self) -> dict[str, object]:
        return {
            "scope": "copilot_observed_model_calls",
            "currency": "USD",
            "known_cost_usd": self.known_cost_usd if self.priced_call_count else None,
            "status": "partial" if self.priced_call_count else "unavailable",
            "observed_call_count": self.observed_call_count,
            "priced_call_count": self.priced_call_count,
            "unknown_call_count": self.observed_call_count - self.priced_call_count,
            "pricing_sources": sorted(self.pricing_sources),
            "excluded": ["workflow_block_execution", "workflow_replay", "judge", "unreported_or_failed_calls"],
        }


_current_ledger: ContextVar[CopilotCostLedger | None] = ContextVar("copilot_cost_ledger", default=None)


@contextmanager
def copilot_cost_scope() -> Iterator[CopilotCostLedger]:
    ledger = CopilotCostLedger()
    token = _current_ledger.set(ledger)
    try:
        with copilot_usage_observer(ledger.record):
            yield ledger
    finally:
        ledger.active = False
        _current_ledger.reset(token)


def current_copilot_cost() -> dict[str, object] | None:
    ledger = _current_ledger.get()
    return ledger.snapshot() if ledger is not None and ledger.active else None


@contextmanager
def exclude_workflow_block_cost() -> Iterator[None]:
    token = _current_ledger.set(None)
    try:
        with copilot_usage_observer(None):
            yield
    finally:
        _current_ledger.reset(token)
