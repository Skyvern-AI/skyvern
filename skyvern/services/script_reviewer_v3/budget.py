"""Budget tracking for the v3 agentic reviewer.

Two budgets:

- :class:`Budget` — per-review triple budget (LLM cycles, total tokens, total
  cost). Constructed fresh for each mid-run or post-run review. Exhaustion on
  any of the three returns ``budget_exhausted`` and ends the loop.
- :class:`RunBudget` — per-workflow-run cumulative budget. Lives on
  :class:`SkyvernContext`. Accumulates across all mid-run v3 invocations in a
  single workflow run. Its :meth:`try_acquire_invocation` method atomically
  reserves a slot (both invocation count AND cost) under an asyncio.Lock,
  handing out an :class:`InvocationHandle` that reconciles actual cost on
  completion.

See ``docs/plans/code-v3-agentic-reviewer/architecture.md`` ("RunBudget —
Atomic Check+Reserve") for design rationale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import structlog

LOG = structlog.get_logger()


# Per-review budget defaults. Tuned via PostHog SCRIPT_REVIEWER_V3_BUDGET payload.
DEFAULT_MIDRUN_MAX_CYCLES = 15
DEFAULT_MIDRUN_MAX_TOKENS = 200_000
DEFAULT_MIDRUN_MAX_COST_USD = 0.50
DEFAULT_MIDRUN_MAX_WALL_SECONDS = 90.0

# Post-run reviews the whole workflow run, so bigger ceilings.
DEFAULT_POSTRUN_MAX_CYCLES = 30
DEFAULT_POSTRUN_MAX_TOKENS = 500_000
DEFAULT_POSTRUN_MAX_COST_USD = 3.00
DEFAULT_POSTRUN_MAX_WALL_SECONDS = 300.0

# Per-run cumulative defaults (mid-run only).
DEFAULT_MIDRUN_MAX_INVOCATIONS_PER_RUN = 5
DEFAULT_MIDRUN_MAX_COST_PER_RUN_USD = 2.50  # = max_invocations * per_review_cost_ceiling


@dataclasses.dataclass
class Budget:
    """Per-review triple-cap budget. Whichever cap hits first ends the loop."""

    max_cycles: int = DEFAULT_MIDRUN_MAX_CYCLES
    max_tokens: int = DEFAULT_MIDRUN_MAX_TOKENS
    max_cost_usd: float = DEFAULT_MIDRUN_MAX_COST_USD

    cycles_used: int = 0
    tokens_used: int = 0
    cost_usd_used: float = 0.0
    started_at: float = dataclasses.field(default_factory=time.monotonic)

    def charge_cycle(self) -> None:
        self.cycles_used += 1

    def charge_tokens(self, n: int) -> None:
        self.tokens_used += n

    def charge_cost(self, usd: float) -> None:
        self.cost_usd_used += usd

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> tuple[bool, str | None]:
        """Return (True, reason) if any cap is hit; (False, None) otherwise."""
        if self.cycles_used >= self.max_cycles:
            return True, f"cycles_cap ({self.cycles_used}/{self.max_cycles})"
        if self.tokens_used >= self.max_tokens:
            return True, f"tokens_cap ({self.tokens_used}/{self.max_tokens})"
        if self.cost_usd_used >= self.max_cost_usd:
            return True, f"cost_cap (${self.cost_usd_used:.4f}/${self.max_cost_usd})"
        return False, None

    def to_metrics(self) -> dict[str, float | int]:
        return {
            "cycles_used": self.cycles_used,
            "tokens_used": self.tokens_used,
            "cost_usd_used": round(self.cost_usd_used, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "max_cycles": self.max_cycles,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
        }


@dataclasses.dataclass
class RunBudget:
    """Per-workflow-run cumulative cap across mid-run v3 invocations.

    Lives on :class:`SkyvernContext` so it persists across hook site invocations
    within one workflow run. Not used by post-run v3 (post-run runs once per run,
    has no "cumulative across invocations" semantics).

    Atomicity story: :meth:`try_acquire_invocation` combines the slot check and
    the reservation into a single critical section under ``_lock``. No
    check-then-charge race possible — contrast with v2's separate cap helpers
    which can overshoot under concurrency (documented in
    ``skyvern/forge/sdk/workflow/service.py::_increment_script_review_counter``).
    """

    max_invocations_per_run: int = DEFAULT_MIDRUN_MAX_INVOCATIONS_PER_RUN
    max_cost_per_run_usd: float = DEFAULT_MIDRUN_MAX_COST_PER_RUN_USD
    per_review_cost_ceiling_usd: float = DEFAULT_MIDRUN_MAX_COST_USD

    _invocations_used: int = 0
    _cost_reserved_usd: float = 0.0
    _lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        # Invariant: max_cost_per_run_usd must be >= max_invocations * per_review_ceiling,
        # otherwise the nominal invocation cap is unreachable (the cost cap binds first).
        # Log a warning and the caller can fall back to defaults if needed.
        required_budget = self.max_invocations_per_run * self.per_review_cost_ceiling_usd
        if self.max_cost_per_run_usd < required_budget:
            LOG.warning(
                "RunBudget violates invariant: max_cost_per_run_usd < max_invocations * per_review_ceiling",
                max_cost_per_run_usd=self.max_cost_per_run_usd,
                max_invocations_per_run=self.max_invocations_per_run,
                per_review_cost_ceiling_usd=self.per_review_cost_ceiling_usd,
                required_budget_usd=required_budget,
                note="Effective invocation cap will be lower than max_invocations_per_run",
            )

    async def try_acquire_invocation(self) -> InvocationHandle | None:
        """Atomically check caps and reserve an invocation slot.

        Returns an :class:`InvocationHandle` on success (slot reserved, cost
        pre-charged at the per-review ceiling). Returns ``None`` if either cap
        would be exceeded — caller should skip v3 and fall through to agent.

        Reservation (not actual usage) is held until :meth:`InvocationHandle.finalize_cost`
        reconciles with the true cost spent during the review.
        """
        async with self._lock:
            if self._invocations_used >= self.max_invocations_per_run:
                return None
            new_reservation = self._cost_reserved_usd + self.per_review_cost_ceiling_usd
            if new_reservation > self.max_cost_per_run_usd:
                return None
            self._invocations_used += 1
            self._cost_reserved_usd = new_reservation
            return InvocationHandle(budget=self, reserved_usd=self.per_review_cost_ceiling_usd)

    def to_metrics(self) -> dict[str, float | int]:
        return {
            "invocations_used": self._invocations_used,
            "cost_reserved_usd": round(self._cost_reserved_usd, 6),
            "max_invocations_per_run": self.max_invocations_per_run,
            "max_cost_per_run_usd": self.max_cost_per_run_usd,
            "per_review_cost_ceiling_usd": self.per_review_cost_ceiling_usd,
        }


@dataclasses.dataclass
class InvocationHandle:
    """Handle to an acquired invocation slot. Call :meth:`finalize_cost` to
    reconcile the pre-charged reservation against actual spend.

    If ``actual_usd`` is less than the reservation, the unused amount is released
    back to the run budget so subsequent invocations can use it. If actual exceeds
    the reservation, the overshoot is logged but the slot is not refused — the
    invocation already completed.
    """

    budget: RunBudget
    reserved_usd: float

    async def finalize_cost(self, actual_usd: float) -> None:
        async with self.budget._lock:
            # Adjust reserved -> actual:
            # - remove the reserved amount, add back the actual amount
            # - clamp at 0 to handle transient arithmetic edge cases
            self.budget._cost_reserved_usd = max(
                0.0,
                self.budget._cost_reserved_usd - self.reserved_usd + actual_usd,
            )
            if actual_usd > self.reserved_usd:
                LOG.warning(
                    "v3 review exceeded cost reservation",
                    reserved_usd=self.reserved_usd,
                    actual_usd=actual_usd,
                    overshoot_usd=actual_usd - self.reserved_usd,
                )
