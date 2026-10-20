from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skyvern.webeye.utils.captcha_solver import ChallengeOutcome


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


# Set by a caller that reaches another client's solve boundary, so the recorded row names the caller instead.
calling_solve_client: ContextVar[str | None] = ContextVar("calling_solve_client", default=None)
