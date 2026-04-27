"""Tests for the copilot narration layer (SKY-9001).

The narrator runs as a background task that consumes tool round-trips from the
agent stream and emits one-sentence user-facing progress lines over SSE. These
tests exercise the state machine, the emit gate, and the fire-and-drop
failure semantics -- the narrator must never be able to crash the agent run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot import narration
from skyvern.forge.sdk.copilot.narration import (
    MIN_NARRATION_GAP_SECONDS,
    NarratorState,
    TransitionKind,
    _build_narrator_prompt,
    _extract_narration_text,
    _NarratorPromptContext,
    _sanitize_narration,
    cancel_in_flight,
    detect_transitions,
    schedule_narration,
    should_emit,
    snapshot_ctx,
)


def _ctx(
    update_workflow_called: bool = False,
    test_after_update_done: bool = False,
    navigate_called: bool = False,
    observation_after_navigate: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        update_workflow_called=update_workflow_called,
        test_after_update_done=test_after_update_done,
        navigate_called=navigate_called,
        observation_after_navigate=observation_after_navigate,
    )


# ---------------------------------------------------------------------------
# detect_transitions
# ---------------------------------------------------------------------------


def test_detect_transitions_workflow_updated_on_false_to_true() -> None:
    before = snapshot_ctx(_ctx(update_workflow_called=False))
    after = snapshot_ctx(_ctx(update_workflow_called=True))
    result = detect_transitions(before, after, tool_name="update_workflow", prior_tool_name="evaluate")
    assert TransitionKind.WORKFLOW_UPDATED in result


def test_detect_transitions_test_completed() -> None:
    before = snapshot_ctx(_ctx(test_after_update_done=False))
    after = snapshot_ctx(_ctx(test_after_update_done=True))
    result = detect_transitions(before, after, tool_name="run_blocks_and_collect_debug", prior_tool_name=None)
    assert TransitionKind.TEST_COMPLETED in result


def test_detect_transitions_navigation_completed() -> None:
    before = snapshot_ctx(_ctx(navigate_called=False))
    after = snapshot_ctx(_ctx(navigate_called=True))
    result = detect_transitions(before, after, tool_name="navigate_browser", prior_tool_name=None)
    assert TransitionKind.NAVIGATION_COMPLETED in result


def test_detect_transitions_new_tool_cluster_only_on_change() -> None:
    before = snapshot_ctx(_ctx())
    after = snapshot_ctx(_ctx())
    assert TransitionKind.NEW_TOOL_CLUSTER in detect_transitions(before, after, "click", prior_tool_name="evaluate")
    assert TransitionKind.NEW_TOOL_CLUSTER not in detect_transitions(before, after, "click", prior_tool_name="click")
    # First tool (prior is None) does not count as a cluster transition -- the
    # agent is just starting up, not changing course.
    assert TransitionKind.NEW_TOOL_CLUSTER not in detect_transitions(before, after, "click", prior_tool_name=None)


def test_detect_transitions_unchanged_ctx_produces_empty() -> None:
    # Same ctx before and after, same tool name: no transitions at all.
    before = snapshot_ctx(_ctx())
    after = snapshot_ctx(_ctx())
    assert detect_transitions(before, after, "click", prior_tool_name="click") == []


# ---------------------------------------------------------------------------
# NarratorState.record_transition priority
# ---------------------------------------------------------------------------


def test_record_transition_first_one_wins_when_same_priority() -> None:
    state = NarratorState()
    state.record_transition(TransitionKind.NEW_TOOL_CLUSTER)
    state.record_transition(TransitionKind.NEW_TOOL_CLUSTER)
    assert state.pending_transition == TransitionKind.NEW_TOOL_CLUSTER


def test_record_transition_higher_priority_overrides_lower() -> None:
    state = NarratorState()
    state.record_transition(TransitionKind.NEW_TOOL_CLUSTER)
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    assert state.pending_transition == TransitionKind.WORKFLOW_UPDATED


def test_record_transition_lower_priority_does_not_override() -> None:
    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    state.record_transition(TransitionKind.NEW_TOOL_CLUSTER)
    assert state.pending_transition == TransitionKind.WORKFLOW_UPDATED


def test_record_tool_truncates_to_buffer_cap() -> None:
    state = NarratorState()
    for i in range(narration.MAX_TOOL_ACTIVITY_BUFFER + 5):
        state.record_tool(tool_name=f"t{i}", summary="s", success=True, iteration=i)
    assert len(state.pending_activity) == narration.MAX_TOOL_ACTIVITY_BUFFER
    # Oldest entries are dropped.
    assert state.pending_activity[0].tool_name == "t5"


# ---------------------------------------------------------------------------
# should_emit gate
# ---------------------------------------------------------------------------


def test_should_emit_false_without_pending_transition() -> None:
    state = NarratorState(last_emitted_at=None)
    assert should_emit(state, now=100.0) is False


def test_should_emit_false_when_in_flight_not_done() -> None:
    async def _pending() -> None:
        await asyncio.sleep(60)

    async def _run() -> bool:
        task = asyncio.create_task(_pending())
        try:
            state = NarratorState(
                last_emitted_at=None, in_flight_task=task, pending_transition=TransitionKind.WORKFLOW_UPDATED
            )
            return should_emit(state, now=100.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert asyncio.run(_run()) is False


def test_should_emit_false_inside_min_gap_window() -> None:
    state = NarratorState(last_emitted_at=100.0, pending_transition=TransitionKind.WORKFLOW_UPDATED)
    assert should_emit(state, now=100.0 + (MIN_NARRATION_GAP_SECONDS - 1)) is False


def test_should_emit_true_after_min_gap_elapsed() -> None:
    state = NarratorState(last_emitted_at=100.0, pending_transition=TransitionKind.WORKFLOW_UPDATED)
    assert should_emit(state, now=100.0 + MIN_NARRATION_GAP_SECONDS + 0.01) is True


def test_should_emit_true_on_first_emission_with_transition() -> None:
    # No prior emission (last_emitted_at is None) + pending transition + no
    # in-flight task = green light.
    state = NarratorState(last_emitted_at=None, pending_transition=TransitionKind.WORKFLOW_UPDATED)
    assert should_emit(state, now=0.0) is True


# ---------------------------------------------------------------------------
# _sanitize_narration
# ---------------------------------------------------------------------------


def test_sanitize_strips_quotes() -> None:
    assert _sanitize_narration('"Checking the login form"') == "Checking the login form"
    assert _sanitize_narration("'Checking the login form'") == "Checking the login form"


def test_sanitize_strips_code_fences() -> None:
    assert _sanitize_narration("```Checking the login form```") == "Checking the login form"


def test_sanitize_collapses_whitespace() -> None:
    assert _sanitize_narration("Checking   the\n  login\tform") == "Checking the login form"


def test_sanitize_truncates_over_cap() -> None:
    out = _sanitize_narration("x" * 500)
    assert out.endswith("...")
    assert len(out) <= narration._MAX_NARRATION_CHARS + len("...")


# ---------------------------------------------------------------------------
# _extract_narration_text
# ---------------------------------------------------------------------------


def test_extract_narration_from_str() -> None:
    assert _extract_narration_text("  hello  ") == "hello"
    assert _extract_narration_text("") is None


def test_extract_narration_from_dict_priority_keys() -> None:
    assert _extract_narration_text({"narration": "a"}) == "a"
    assert _extract_narration_text({"sentence": "b"}) == "b"
    assert _extract_narration_text({"user_response": "c"}) == "c"
    assert _extract_narration_text({"content": "d"}) == "d"
    assert _extract_narration_text({"text": "e"}) == "e"


def test_extract_narration_unknown_shape_returns_none() -> None:
    assert _extract_narration_text({"unrelated": "x"}) is None
    assert _extract_narration_text(42) is None
    assert _extract_narration_text(None) is None


# ---------------------------------------------------------------------------
# _narration_leaks_identifier
# ---------------------------------------------------------------------------


def test_leak_guard_flags_snake_case_tokens() -> None:
    leaks = narration._narration_leaks_identifier
    assert leaks("Running the extract_top_post block.") is True
    assert leaks("Calling update_and_run_blocks on the workflow.") is True
    assert leaks("Extracting via the extract_top_post block.") is True


def test_leak_guard_flags_backtick_identifiers() -> None:
    assert narration._narration_leaks_identifier("Running the `extract_top_post` block.") is True


def test_leak_guard_flags_via_the_phrasing() -> None:
    # "via the ... block" phrasing correlates strongly with the LLM echoing
    # an identifier back even if the identifier itself slipped the regex.
    assert narration._narration_leaks_identifier("Extracting via the top post block.") is True


def test_leak_guard_flags_camel_case_tokens() -> None:
    leaks = narration._narration_leaks_identifier
    assert leaks("Running the extractTopPost block.") is True
    assert leaks("Calling updateAndRunBlocks on the workflow.") is True


def test_leak_guard_flags_kebab_case_tokens() -> None:
    leaks = narration._narration_leaks_identifier
    # 3+ hyphen segments = identifier-shaped. Two-segment compounds like
    # "follow-up" stay legit English.
    assert leaks("Running the extract-top-post step.") is True
    assert leaks("Invoking update-and-run-blocks.") is True


def test_leak_guard_accepts_clean_sentences() -> None:
    leaks = narration._narration_leaks_identifier
    assert leaks("Setting up the workflow.") is False
    assert leaks("Extracting the requested fields.") is False
    assert leaks("Running the workflow to find today's top post.") is False
    # Ordinary English hyphenated compounds are not identifiers.
    assert leaks("Following up on the results.") is False
    assert leaks("Double-checking the output.") is False


# ---------------------------------------------------------------------------
# extract_tool_details — no identifier-looking tokens leave this function
# ---------------------------------------------------------------------------


def test_extract_tool_details_update_workflow_excludes_block_names() -> None:
    parsed = {
        "ok": True,
        "data": {
            "block_count": 2,
            "blocks": [{"label": "open_target_page"}, {"label": "extract_values"}],
            "overall_status": "succeeded",
        },
    }
    details = narration.extract_tool_details("update_and_run_blocks", parsed)
    assert "open_target_page" not in details
    assert "extract_values" not in details
    assert "2 step(s)" in details
    assert "status: succeeded" in details


def test_extract_tool_details_run_blocks_excludes_executed_labels() -> None:
    parsed = {
        "ok": True,
        "data": {
            "executed_block_labels": ["open_hn", "extract_top"],
            "overall_status": "succeeded",
        },
    }
    details = narration.extract_tool_details("run_blocks_and_collect_debug", parsed)
    assert "open_hn" not in details
    assert "extract_top" not in details
    assert "2 step(s)" in details


def test_extract_tool_details_navigate_uses_domain_only() -> None:
    parsed = {"ok": True, "data": {"url": "https://sub.example.com/items?id=12345&token=secret"}}
    details = narration.extract_tool_details("navigate_browser", parsed)
    assert details == "domain: sub.example.com"


def test_extract_tool_details_navigate_strips_userinfo_and_port() -> None:
    parsed = {
        "ok": True,
        "data": {"url": "https://user:secret@host.example.com:8443/private/path?auth=abc"},
    }
    details = narration.extract_tool_details("navigate_browser", parsed)
    assert details == "domain: host.example.com"


def test_extract_tool_details_navigate_ignores_redirect_like_urls() -> None:
    # Decoy authority inside the query string must not become the reported
    # host. This is the exact shape CodeQL's incomplete-url-substring-match
    # rule was pointing at.
    parsed = {
        "ok": True,
        "data": {"url": "https://attacker.example.com/?redirect=https://victim.example.com/path"},
    }
    details = narration.extract_tool_details("navigate_browser", parsed)
    assert details == "domain: attacker.example.com"


def test_extract_tool_details_get_run_results_drops_field_names() -> None:
    parsed = {
        "ok": True,
        "data": {"rank": 1, "title": "x", "url": "y", "points": 10, "author": "a"},
    }
    details = narration.extract_tool_details("get_run_results", parsed)
    # Field names in user's extracted data may include arbitrary strings; hide
    # them behind a count so the narrator can't echo private field names back.
    assert "rank" not in details
    assert "title" not in details
    assert "5 extracted field(s)" in details


def test_extract_tool_details_failure_is_generic() -> None:
    details = narration.extract_tool_details(
        "update_and_run_blocks",
        {"ok": False, "error": "AgentTool failed: secret_key_123_invalid"},
    )
    # Raw error payload may include secrets / internal identifiers -- we keep
    # it vague so it can't reach the narrator prompt.
    assert "secret_key_123" not in details
    assert "failed" in details.lower()


# ---------------------------------------------------------------------------
# _build_narrator_prompt — redacts raw tool identifiers
# ---------------------------------------------------------------------------


def test_prompt_does_not_leak_raw_tool_names() -> None:
    state = NarratorState()
    state.record_tool(tool_name="update_workflow", summary="wrote 3 blocks", success=True, iteration=0)
    state.record_tool(tool_name="run_blocks_and_collect_debug", summary="ran successfully", success=True, iteration=1)
    prompt = _build_narrator_prompt(
        _NarratorPromptContext(
            transition=TransitionKind.WORKFLOW_UPDATED,
            activity=list(state.pending_activity),
        )
    )
    # Raw internal tool names must not appear in the prompt we send to the LLM.
    # Instead their user-facing labels do.
    assert "update_workflow" not in prompt
    assert "run_blocks_and_collect_debug" not in prompt
    assert "revising the workflow draft" in prompt
    assert "running a test of the workflow" in prompt


def test_prompt_handles_unknown_tool_via_generic_label() -> None:
    entry = narration._ToolActivityEntry(
        tool_name="some_future_tool",
        summary="did a thing",
        success=True,
        iteration=0,
    )
    prompt = _build_narrator_prompt(
        _NarratorPromptContext(transition=TransitionKind.NEW_TOOL_CLUSTER, activity=[entry])
    )
    assert "some_future_tool" not in prompt
    assert "running a tool" in prompt


def test_prompt_truncates_long_tool_summaries() -> None:
    entry = narration._ToolActivityEntry(
        tool_name="evaluate",
        summary="x" * 500,
        success=True,
        iteration=0,
        details="x" * 500,
    )
    prompt = _build_narrator_prompt(
        _NarratorPromptContext(transition=TransitionKind.NEW_TOOL_CLUSTER, activity=[entry])
    )
    # Snippet is capped at 200 chars + ellipsis. The prompt length overall is
    # well under the raw details length.
    assert "x" * 300 not in prompt


# ---------------------------------------------------------------------------
# schedule_narration + _narration_task_body — end-to-end
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal EventSourceStream stand-in for narration tests."""

    def __init__(self, send_ok: bool = True) -> None:
        self.send_ok = send_ok
        self.sent: list[Any] = []

    async def send(self, payload: Any) -> bool:
        self.sent.append(payload)
        return self.send_ok

    async def is_disconnected(self) -> bool:
        return False


async def _install_handler(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(narration, "_get_narrator_handler", lambda: handler)


@pytest.mark.asyncio
async def test_schedule_narration_no_op_when_no_transition() -> None:
    state = NarratorState()
    stream = _FakeStream()
    schedule_narration(state, stream, iteration=0)  # type: ignore[arg-type]
    assert state.in_flight_task is None
    assert stream.sent == []


@pytest.mark.asyncio
async def test_schedule_narration_emits_on_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        assert prompt_name == "workflow-copilot-narration"
        return "Revising the workflow draft."

    await _install_handler(monkeypatch, _handler)

    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    stream = _FakeStream()

    assert state.last_emitted_at is None
    schedule_narration(state, stream, iteration=3)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    await state.in_flight_task

    assert state.in_flight_task is None
    assert len(stream.sent) == 1
    payload = stream.sent[0]
    assert payload.narration == "Revising the workflow draft."
    assert payload.iteration == 3
    # Transition is consumed once scheduled.
    assert state.pending_transition is None
    # Clock advanced only after the SSE frame was delivered.
    assert state.last_emitted_at is not None


@pytest.mark.asyncio
async def test_schedule_narration_keeps_clock_frozen_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed narration must not advance ``last_emitted_at``. Advancing would
    silence the next ``MIN_NARRATION_GAP_SECONDS`` of valid transitions even
    though no user-visible narration was actually delivered."""

    async def _raising_handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    await _install_handler(monkeypatch, _raising_handler)

    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=1)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    await state.in_flight_task

    assert state.last_emitted_at is None, "clock must stay frozen when no narration was delivered"
    # The slot is released so the next transition can schedule immediately.
    assert state.in_flight_task is None


@pytest.mark.asyncio
async def test_schedule_narration_swallows_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _raising_handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    await _install_handler(monkeypatch, _raising_handler)

    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=1)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    # Awaiting the task should not raise -- errors are swallowed inside.
    await state.in_flight_task

    assert state.in_flight_task is None
    assert stream.sent == []


@pytest.mark.asyncio
async def test_schedule_narration_drops_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narration, "NARRATOR_TIMEOUT_SECONDS", 0.05)

    async def _slow_handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        await asyncio.sleep(1.0)
        return "too late"

    await _install_handler(monkeypatch, _slow_handler)

    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=2)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    await state.in_flight_task

    assert state.in_flight_task is None
    assert stream.sent == []


@pytest.mark.asyncio
async def test_schedule_narration_drops_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blank_handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        return "   "

    await _install_handler(monkeypatch, _blank_handler)

    state = NarratorState()
    state.record_transition(TransitionKind.TEST_COMPLETED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=4)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    await state.in_flight_task

    assert stream.sent == []
    assert state.in_flight_task is None


@pytest.mark.asyncio
async def test_schedule_narration_no_handler_available(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the AppHolder-not-initialized case (unit tests, pre-startup).
    await _install_handler(monkeypatch, None)

    state = NarratorState()
    state.record_transition(TransitionKind.NAVIGATION_COMPLETED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=0)  # type: ignore[arg-type]
    assert state.in_flight_task is not None
    await state.in_flight_task

    assert stream.sent == []
    assert state.in_flight_task is None


@pytest.mark.asyncio
async def test_schedule_narration_skips_when_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second transition arriving while the first narration is in flight
    must not spawn a concurrent task. At most one narration runs at a time."""
    gate = asyncio.Event()

    async def _gated_handler(prompt: str, prompt_name: str, **kwargs: object) -> str:
        await gate.wait()
        return "first narration"

    await _install_handler(monkeypatch, _gated_handler)

    state = NarratorState()
    state.record_transition(TransitionKind.WORKFLOW_UPDATED)
    stream = _FakeStream()

    schedule_narration(state, stream, iteration=0)  # type: ignore[arg-type]
    first_task = state.in_flight_task
    assert first_task is not None

    # Second transition arrives while first is running.
    state.record_transition(TransitionKind.ENFORCEMENT_RETRY)
    schedule_narration(state, stream, iteration=1)  # type: ignore[arg-type]
    # Same task -- no new task spawned.
    assert state.in_flight_task is first_task

    gate.set()
    await first_task
    assert len(stream.sent) == 1


# ---------------------------------------------------------------------------
# cancel_in_flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_in_flight_noop_when_no_task() -> None:
    state = NarratorState()
    await cancel_in_flight(state)  # must not raise


@pytest.mark.asyncio
async def test_cancel_in_flight_noop_when_task_done() -> None:
    async def _immediate() -> None:
        return None

    task = asyncio.create_task(_immediate())
    await task
    state = NarratorState(in_flight_task=task)
    await cancel_in_flight(state)  # must not raise


@pytest.mark.asyncio
async def test_cancel_in_flight_hard_cancels_running_task() -> None:
    """A narration LLM call runs ~2-3s; waiting it out before the final
    response would regress completion latency. Cancel immediately so the
    route can ship the final assistant message without delay."""

    async def _pending() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_pending())
    state = NarratorState(in_flight_task=task)
    await cancel_in_flight(state)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_cancel_in_flight_returns_fast() -> None:
    """Cancellation must not add meaningful latency to stream teardown.
    Budget: well under 100ms for a hard-cancel."""
    started = asyncio.Event()

    async def _pending() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_pending())
    await started.wait()
    state = NarratorState(in_flight_task=task)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await cancel_in_flight(state)
    elapsed = loop.time() - t0
    assert elapsed < 0.1, f"cancel_in_flight took {elapsed:.3f}s, expected <0.1s"
