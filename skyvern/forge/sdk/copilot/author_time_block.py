from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ADR-0011: unsafe code in a persisted block outlives the turn and runs with the org's browser.
CODE_SAFETY_BLOCK_ID = "code_safety"
# ADR-0002: a raw or broadened credential in a persisted draft is an irreversible disclosure.
CREDENTIAL_SCOUT_BLOCK_ID = "credential_scout"
# ADR-0010: a persisted block outside the code-native ladder escapes the migration it forces.
BANNED_BLOCKS_BLOCK_ID = "banned_blocks"

AUTHOR_TIME_HARD_BLOCKS: frozenset[str] = frozenset(
    {
        CODE_SAFETY_BLOCK_ID,
        CREDENTIAL_SCOUT_BLOCK_ID,
        BANNED_BLOCKS_BLOCK_ID,
    }
)


@dataclass(frozen=True)
class AuthorTimeBlock:
    """The only value an author-time refusal sink accepts. Constructing one with an
    identity outside ``AUTHOR_TIME_HARD_BLOCKS`` raises, so a new validator cannot
    become a wall without editing that constant."""

    block_id: str
    error: str
    user_facing_summary: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.block_id not in AUTHOR_TIME_HARD_BLOCKS:
            raise ValueError(
                f"{self.block_id!r} is not an author-time hard block; "
                f"validators return findings. Allowed: {sorted(AUTHOR_TIME_HARD_BLOCKS)}"
            )
