"""Anti-bot evidence a failed run's error reporting reads without a model: the challenge-solve receipts its context
kept. The closed HAR joins them only at cleanup, after every earlier error call has run."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.utils.challenge_solve_record import ChallengeSolveReceipt

_MAX_PROMPT_RECEIPTS = 10


def _utc(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def run_solve_receipts(run_ids: Iterable[str | None], since: datetime | None) -> list[ChallengeSolveReceipt]:
    """This context's receipts for these runs, oldest first, from solves that ended at or after ``since``."""
    context = skyvern_context.current()
    if context is None:
        return []
    ids = {run_id for run_id in run_ids if run_id}
    return [
        receipt
        for receipt in context.challenge_solve_receipts
        if receipt.run_id in ids and (since is None or _utc(receipt.ended_at) >= _utc(since))
    ]


def _key(receipt: ChallengeSolveReceipt) -> tuple[str, str | None]:
    return receipt.owner_id or receipt.run_id, receipt.service


def unresolved_challenges(receipts: list[ChallengeSolveReceipt]) -> list[ChallengeSolveReceipt]:
    """Unsolved solves of a presented challenge that no later solve of the same owner and known service cleared. A
    solve that identified no service clears nothing."""
    unresolved: list[ChallengeSolveReceipt] = []
    for receipt in receipts:
        if receipt.outcome == "solved" and receipt.service is not None:
            unresolved = [pending for pending in unresolved if _key(pending) != _key(receipt)]
        elif receipt.outcome == "unsolved" and receipt.service is not None and receipt.page_state != "challenge_gone":
            unresolved.append(receipt)
    return unresolved


def prompt_evidence_str(task: Task) -> str | None:
    """The bounded, token-free solve facts an existing error call can read for this task so far, or None."""
    receipts = [
        receipt
        for receipt in run_solve_receipts((task.task_id, task.workflow_run_id), task.started_at or task.created_at)
        if (receipt.owner_id or receipt.run_id) == task.task_id
    ]
    return _projection(receipts)


def run_prompt_evidence_str(run_id: str | None, since: datetime | None) -> str | None:
    """The same facts for every task of one run, for a call that speaks for the whole run."""
    return _projection(run_solve_receipts((run_id,), since))


def _projection(receipts: list[ChallengeSolveReceipt]) -> str | None:
    if not receipts:
        return None
    unresolved = {id(receipt) for receipt in unresolved_challenges(receipts)}
    kept = receipts[-_MAX_PROMPT_RECEIPTS:]
    return json.dumps(
        {
            "challenge_solves": [
                {
                    "ref": f"solve:{index}",
                    "service": receipt.service or "unknown",
                    "outcome": receipt.outcome,
                    "page_state": receipt.page_state,
                    "unresolved": id(receipt) in unresolved,
                }
                for index, receipt in enumerate(kept, 1)
            ],
            "truncated": len(kept) < len(receipts),
            "network_recording": "not_available_before_the_run_ends",
        }
    )
