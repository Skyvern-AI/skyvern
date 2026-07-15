from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.failure_tracking import (
    ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES,
    compute_failure_signature,
    compute_frontier_fingerprint,
    compute_repair_root_cause_signature,
    update_repeated_failure_state,
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


def test_signature_stable_for_same_inputs() -> None:
    a = compute_failure_signature("extract", "Timeout", [{"category": "NETWORK_ERROR"}], False)
    b = compute_failure_signature("extract", "Timeout", [{"category": "NETWORK_ERROR"}], False)
    assert a == b and a is not None


def test_signature_differs_on_reason_change() -> None:
    a = compute_failure_signature("extract", "Timeout", None, False)
    b = compute_failure_signature("extract", "Permission denied", None, False)
    assert a != b


def test_signature_uses_root_cause_identity_for_repeat_detection() -> None:
    categories = [{"category": "UNRECOVERABLE_TOOL_ERROR"}]
    signatures = {
        compute_failure_signature(
            "login_v1", 'Browser session not found while waiting for locator("#submit")', categories, False
        ),
        compute_failure_signature(
            "login_v2", 'No browser context while waiting for locator("#submit")', categories, False
        ),
        compute_failure_signature(
            "login_v3", 'No browser context while waiting for locator("#submit")', categories, True
        ),
        compute_failure_signature(
            "login_v4", 'browser_context.mode=none while waiting for locator("#submit")', categories, True
        ),
    }
    parameter_signature = compute_failure_signature("block_a", None, [{"category": "PARAMETER_BINDING_ERROR"}], False)

    assert len(signatures) == 1
    assert next(iter(signatures)) is not None
    assert parameter_signature is not None
    assert parameter_signature == compute_failure_signature(
        "renamed_block", None, [{"category": "PARAMETER_BINDING_ERROR"}], False
    )


def test_signature_collapses_parameter_binding_error() -> None:
    # PARAMETER_BINDING_ERROR embeds offending key names in failure_reason;
    # different keys must still hash to the same signature so repeats count.
    a = compute_failure_signature(
        "block_a", "missing parameter 'x' at path foo.bar", [{"category": "PARAMETER_BINDING_ERROR"}], False
    )
    b = compute_failure_signature(
        "block_a", "missing parameter 'y' at path baz.qux", [{"category": "PARAMETER_BINDING_ERROR"}], False
    )
    assert a == b


def test_category_only_and_code_imposition_root_causes_collapse_noise() -> None:
    category_detected = compute_repair_root_cause_signature(
        failure_categories=[{"category": "ANTI_BOT_DETECTION"}],
        failure_reason="Verify you are human before continuing.",
    )
    context_detected = compute_repair_root_cause_signature(
        failure_categories=[],
        failure_reason="Access page showed different challenge copy this time.",
        detected_challenge=True,
    )
    first = compute_repair_root_cause_signature(
        error_texts=[
            "Unable to impose synthesized code block: dropped scout interaction 0 from `click` (ambiguous_bare_selector)."
        ],
    )
    retried = compute_repair_root_cause_signature(
        error_texts=[
            "Unable to impose synthesized code block: dropped scout interaction 4 from `click` (ambiguous_bare_selector)."
        ],
    )

    assert category_detected.root_cause_signature == context_detected.root_cause_signature
    assert category_detected.primary_category == "ANTI_BOT_CHALLENGE"
    assert first.root_cause_signature == retried.root_cause_signature
    assert first.error_class == "code_block_synthesis_ambiguous_bare_selector"


def test_terminal_challenge_categories_follow_anti_bot_root_cause_aliases() -> None:
    assert TERMINAL_CHALLENGE_FAILURE_CATEGORIES == ANTI_BOT_CHALLENGE_FAILURE_CATEGORIES


def test_update_state_ignores_keyword_only_anti_bot_categories_for_identity() -> None:
    wf = _make_workflow([_Block("open", url="https://a.test")])
    keyword_entry = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.7, "evidence_source": "keyword_only"}

    ctx_a = _make_ctx(failure_reason="Timeout waiting for #search", workflow=wf, executed_labels=["open"])
    update_repeated_failure_state(ctx_a, {"ok": False, "data": {"failure_categories": [dict(keyword_entry)]}})
    ctx_b = _make_ctx(failure_reason="ValueError: missing column 'name'", workflow=wf, executed_labels=["open"])
    update_repeated_failure_state(ctx_b, {"ok": False, "data": {"failure_categories": [dict(keyword_entry)]}})

    assert ctx_a.last_failure_signature is not None
    assert ctx_b.last_failure_signature is not None
    assert ctx_a.last_failure_signature != ctx_b.last_failure_signature


def test_update_state_keeps_carrier_backed_anti_bot_category_identity() -> None:
    wf = _make_workflow([_Block("open", url="https://a.test")])
    carrier_entry = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "evidence_source": "challenge_state"}

    ctx_a = _make_ctx(failure_reason="Challenge page A blocked the run", workflow=wf, executed_labels=["open"])
    update_repeated_failure_state(ctx_a, {"ok": False, "data": {"failure_categories": [dict(carrier_entry)]}})
    ctx_b = _make_ctx(failure_reason="Different challenge prose on retry", workflow=wf, executed_labels=["open"])
    update_repeated_failure_state(ctx_b, {"ok": False, "data": {"failure_categories": [dict(carrier_entry)]}})

    assert ctx_a.last_failure_signature is not None
    assert ctx_a.last_failure_signature == ctx_b.last_failure_signature


def test_fingerprint_empty_without_workflow_definition() -> None:
    assert compute_frontier_fingerprint(["open"], None) == ""


def test_fingerprint_changes_when_block_config_changes() -> None:
    wf_a = _make_workflow([_Block("open", url="https://a.test")])
    wf_b = _make_workflow([_Block("open", url="https://b.test")])

    assert compute_frontier_fingerprint(["open"], wf_a.workflow_definition) != compute_frontier_fingerprint(
        ["open"], wf_b.workflow_definition
    )


def test_streak_increments_when_signature_and_fingerprint_repeat() -> None:
    wf = _make_workflow([_Block("open", url="x"), _Block("extract", schema="s")])
    fingerprint = compute_frontier_fingerprint(["open", "extract"], wf.workflow_definition)
    signature = compute_failure_signature("extract", "Timeout on element", None, False)

    ctx = _make_ctx(
        workflow=wf,
        last_signature=signature,
        last_fingerprint=fingerprint,
        streak=1,
    )
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})

    assert ctx.repeated_failure_streak_count == 2
    assert ctx.last_failure_signature == signature
    assert ctx.last_frontier_fingerprint == fingerprint


def test_streak_increments_across_challenge_prose_and_frontier_drift() -> None:
    wf = _make_workflow([_Block("open", url="x"), _Block("extract", schema="s")])
    fingerprint = compute_frontier_fingerprint(["open", "extract"], wf.workflow_definition)
    signature = compute_failure_signature(
        "extract_v1",
        "Run completed, but extracted data reported a blocker: Verify you are human",
        None,
        True,
        detected_challenge=True,
    )

    ctx = _make_ctx(
        suspicious=True,
        anti_bot="Extracted data reported anti-bot blocker: Verify you are human",
        failure_reason="Run completed, but extracted data reported a blocker: Cloudflare challenge still present",
        frontier_label="extract_v2",
        workflow=wf,
        last_signature=signature,
        last_fingerprint=fingerprint,
        streak=5,
    )
    update_repeated_failure_state(ctx, {"ok": True, "data": {}})

    assert ctx.repeated_failure_streak_count == 6
    assert ctx.last_failure_signature == signature


def test_streak_resets_on_meaningful_success() -> None:
    wf = _make_workflow([_Block("open", url="x")])
    ctx = _make_ctx(
        workflow=wf,
        executed_labels=["open"],
        last_signature="prior",
        last_fingerprint="prior",
        streak=4,
        nudge_streak=3,
    )
    update_repeated_failure_state(ctx, {"ok": True, "data": {}})

    assert ctx.repeated_failure_streak_count == 0
    assert ctx.last_failure_signature is None
    assert ctx.repeated_failure_nudge_emitted_at_streak == 0


@pytest.mark.parametrize(
    "case_kwargs",
    [
        pytest.param(
            {
                "failure_reason": "Totally new error",
                "last_signature": "extract|old-reason|",
                "last_fingerprint_matches": True,
            },
            id="new_signature",
        ),
        pytest.param(
            {
                "failure_reason": "Timeout on element",
                "last_signature_matches": True,
                "last_fingerprint": "different-fingerprint-from-prior-run",
            },
            id="new_fingerprint",
        ),
    ],
)
def test_streak_and_nudge_reset_on_change(case_kwargs: dict[str, Any]) -> None:
    wf = _make_workflow([_Block("open", url="x"), _Block("extract", schema="s")])
    fingerprint = compute_frontier_fingerprint(["open", "extract"], wf.workflow_definition)
    signature = compute_failure_signature("extract", "Timeout on element", None, False)

    last_signature = signature if case_kwargs.pop("last_signature_matches", False) else case_kwargs["last_signature"]
    last_fingerprint = (
        fingerprint if case_kwargs.pop("last_fingerprint_matches", False) else case_kwargs["last_fingerprint"]
    )

    ctx = _make_ctx(
        workflow=wf,
        failure_reason=case_kwargs["failure_reason"],
        last_signature=last_signature,
        last_fingerprint=last_fingerprint,
        streak=3,
        nudge_streak=2,
    )
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})

    assert ctx.repeated_failure_streak_count == 1
    assert ctx.repeated_failure_nudge_emitted_at_streak == 0


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


def test_action_fingerprint_streak_increments_on_repeat() -> None:
    ctx = _make_action_ctx(pending_fingerprint="fp_1")
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 1
    assert ctx.last_action_sequence_fingerprint == "fp_1"

    ctx.pending_action_sequence_fingerprint = "fp_1"
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 2

    ctx.pending_action_sequence_fingerprint = "fp_1"
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 3


def test_action_fingerprint_streak_resets_when_fingerprint_changes() -> None:
    ctx = _make_action_ctx(pending_fingerprint="fp_1", last_fingerprint="fp_1", streak=2)
    # Different action sequence this run — streak resets to 1.
    ctx.pending_action_sequence_fingerprint = "fp_2"
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 1
    assert ctx.last_action_sequence_fingerprint == "fp_2"


def test_action_fingerprint_streak_preserved_on_transient_empty_trace() -> None:
    """A single run with no action trace (e.g. no failed blocks had a task_id)
    between two fingerprint-matching runs shouldn't erase an in-progress
    streak. Otherwise a single empty trace would mask a real loop.
    """
    ctx = _make_action_ctx(pending_fingerprint=None, last_fingerprint="fp_1", streak=1)
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 1
    # Prior fingerprint is cleared because pending is None — the next run's fp
    # compares against None, which is the correct "no match" result.
    assert ctx.last_action_sequence_fingerprint is None


def test_action_fingerprint_streak_resets_on_meaningful_success() -> None:
    ctx = _make_action_ctx(pending_fingerprint="fp_1", last_fingerprint="fp_1", streak=2)
    ctx.last_test_failure_reason = None
    ctx.last_test_suspicious_success = False
    update_repeated_failure_state(ctx, {"ok": True, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 0
    # Success promotes pending → last so the next failure can compare against it.
    assert ctx.last_action_sequence_fingerprint == "fp_1"
    assert ctx.pending_action_sequence_fingerprint is None


def test_action_fingerprint_streak_independent_of_frontier_streak() -> None:
    """The action-sequence streak ticks up even when the failure reason text
    changes between runs. The frontier-based streak requires the signature
    (derived from failure reason / categories) to match, so it would reset;
    the action-sequence streak depends only on the action shape.
    """
    wf = _make_workflow([_Block("open", url="x")])
    fingerprint = compute_frontier_fingerprint(["open"], wf.workflow_definition)

    ctx = SimpleNamespace(
        last_test_suspicious_success=False,
        last_test_anti_bot=None,
        last_test_failure_reason="Validation failed: name is required",
        last_frontier_start_label="open",
        last_executed_block_labels=["open"],
        last_workflow=wf,
        last_failure_signature=None,
        last_frontier_fingerprint=fingerprint,
        repeated_failure_streak_count=0,
        repeated_failure_nudge_emitted_at_streak=0,
        pending_action_sequence_fingerprint="fp_same",
        last_action_sequence_fingerprint=None,
        repeated_action_fingerprint_streak_count=0,
    )
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 1

    # New failure-reason text — frontier streak resets, action streak continues.
    ctx.last_test_failure_reason = "Validation failed: email is required"
    ctx.pending_action_sequence_fingerprint = "fp_same"
    update_repeated_failure_state(ctx, {"ok": False, "data": {}})
    assert ctx.repeated_action_fingerprint_streak_count == 2
    assert ctx.repeated_failure_streak_count == 1
