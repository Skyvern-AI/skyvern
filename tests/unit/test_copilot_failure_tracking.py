from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.failure_tracking import (
    ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES,
    block_shape_hashes_by_label,
)
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_FAILURE_CATEGORIES


class _Block:
    def __init__(self, label: str, **config: Any) -> None:
        self.label = label
        self._config = config

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"label": self.label, **{k: v for k, v in self._config.items() if v is not None}}


def _make_workflow(blocks: list[_Block]) -> Any:
    definition = SimpleNamespace(blocks=blocks)
    return SimpleNamespace(workflow_definition=definition)


def _make_ctx(
    *,
    suspicious: bool = False,
    failure_reason: str | None = "Timeout on element",
    anti_bot: str | None = None,
    frontier_label: str = "extract",
    executed_labels: list[str] | None = None,
    workflow: Any = None,
    last_signature: str | None = None,
    last_fingerprint: str = "",
    streak: int = 0,
    nudge_streak: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        last_test_suspicious_success=suspicious,
        last_test_anti_bot=anti_bot,
        last_test_failure_reason=failure_reason,
        last_frontier_start_label=frontier_label,
        last_executed_block_labels=executed_labels or ["open", "extract"],
        last_workflow=workflow,
        last_failure_signature=last_signature,
        last_frontier_fingerprint=last_fingerprint,
        repeated_failure_streak_count=streak,
        repeated_failure_nudge_emitted_at_streak=nudge_streak,
    )


def test_terminal_challenge_categories_follow_anti_bot_root_cause_aliases() -> None:
    assert TERMINAL_CHALLENGE_FAILURE_CATEGORIES == ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES


# --------------------------------------------------------------------------- #
# Action-sequence fingerprint streak                                          #
# --------------------------------------------------------------------------- #


def _make_action_ctx(
    *,
    pending_fingerprint: str | None,
    last_fingerprint: str | None = None,
    streak: int = 0,
    failure_reason: str | None = "fail",
    last_signature: str | None = "sig_prior",
    last_frontier_fingerprint: str = "fp_prior",
) -> SimpleNamespace:
    """CtX builder specific to action-sequence tests — mirrors what
    ``_run_blocks_and_collect_debug`` sets on CopilotContext before the
    ``update_repeated_failure_state`` call.
    """
    wf = _make_workflow([_Block("open", url="x")])
    ctx = _make_ctx(
        workflow=wf,
        failure_reason=failure_reason,
        executed_labels=["open"],
        last_signature=last_signature,
        last_fingerprint=last_frontier_fingerprint,
    )
    ctx.pending_action_sequence_fingerprint = pending_fingerprint
    ctx.last_action_sequence_fingerprint = last_fingerprint
    ctx.repeated_action_fingerprint_streak_count = streak
    return ctx


def test_block_shape_hashes_by_label_binds_every_label_that_resolves() -> None:
    workflow = _make_workflow([_Block("open_job", code="a"), _Block("read_receipt", code="b")])

    hashes = block_shape_hashes_by_label(["open_job", "confirm_job", "read_receipt"], workflow.workflow_definition)

    assert sorted(hashes) == ["open_job", "read_receipt"]
    assert hashes["open_job"] != hashes["read_receipt"]
    assert block_shape_hashes_by_label(["open_job"], None) == {}
