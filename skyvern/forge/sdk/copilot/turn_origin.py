from __future__ import annotations

from enum import StrEnum


class TurnOrigin(StrEnum):
    interactive = "interactive"
    runtime_self_heal = "runtime_self_heal"


class HealAdoptionFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


SELF_HEAL_SESSION_PREFIX = "selfheal:"


def make_self_heal_session_id(workflow_run_id: str) -> str:
    return f"{SELF_HEAL_SESSION_PREFIX}{workflow_run_id}"


def is_self_heal_session_id(session_id: str | None) -> bool:
    if not session_id:
        return False
    return session_id.startswith(SELF_HEAL_SESSION_PREFIX)
