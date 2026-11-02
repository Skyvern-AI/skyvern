from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from skyvern.webeye.utils.captcha_solver import ChallengeOutcome

# What a solve helper saw for one route: a matched challenge, its own solved verdict, its own verdict that the
# challenge stayed unsolved, an explicit "no challenge here", or a matched challenge it deliberately did not attempt.
ChallengeVerdict = Literal["detected", "solved", "unsolved", "absent", "skipped"]


@dataclass(frozen=True)
class ChallengeObservation:
    route: str
    verdict: ChallengeVerdict


@dataclass
class ChallengeSolveRecord:
    """What one challenge-solve call observed, filled in by the call site for a deployment's solve ledger.

    Nested solve calls share the outermost call's record. The OSS build discards it."""

    challenge: ChallengeOutcome | None = None
    route: str | None = None
    route_results: list[Any] | None = None
    image_text_read: bool | None = None
    image_text_delivered: bool | None = None
    extension_solved: bool | None = None
    unsolved: bool = False
    observations: list[ChallengeObservation] = field(default_factory=list)
    # (producer, variant): the reCAPTCHA variant a solver route found on the widget it targeted.
    recaptcha_target: tuple[str, str] | None = None


@dataclass(frozen=True)
class ChallengeSolveReceipt:
    """A finished solve call's token-free outcome, kept on the run's context for its failure reporting. ``service`` is
    the challenge the solve positively identified (never the solver it ran or a model-chosen captcha type), None when
    it identified no presented challenge."""

    run_id: str
    ended_at: datetime
    service: str | None
    outcome: str
    page_state: str
    # The task whose solve this was; the run id alone cannot tell apart the tasks of one workflow run.
    owner_id: str | None = None


MAX_SOLVE_RECEIPTS = 20


# Set by a caller that reaches another client's solve boundary, so the recorded row names the caller instead.
calling_solve_client: ContextVar[str | None] = ContextVar("calling_solve_client", default=None)
# The record of the solve call in progress, scoped by the deployment's solve ledger.
active_solve_record: ContextVar[ChallengeSolveRecord | None] = ContextVar("challenge_solve_record", default=None)


def note_challenge(route: str, verdict: ChallengeVerdict) -> None:
    """Report what a solve helper observed to the solve call in progress; a no-op outside one."""
    record = active_solve_record.get()
    if record is not None:
        record.observations.append(ChallengeObservation(route, verdict))
