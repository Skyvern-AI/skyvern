"""task_v2 emits a best-effort deliverable from gathered data when it fails on budget exhaustion (SKY-11238)."""

from __future__ import annotations

import pytest

from skyvern.forge.prompts import prompt_engine
from skyvern.services import task_v2_service


class _FakeTaskV2:
    observer_cruise_id = "tsk_v2_test"
    organization_id = "o_test"
    workflow_run_id = "wr_test"
    workflow_id = "w_test"
    workflow_permanent_id = "wpid_test"
    url = "https://example.com"
    prompt = "do the thing"
    extracted_information_schema = None
    workflow_system_prompt = None


@pytest.mark.asyncio
async def test_best_effort_deliverable_skips_when_no_history_gathered() -> None:
    # No data gathered => nothing to synthesize, and no LLM call should be attempted.
    summary, output = await task_v2_service._best_effort_failure_deliverable(
        _FakeTaskV2(), task_history=[], context=object()
    )
    assert summary is None
    assert output is None


@pytest.mark.asyncio
async def test_best_effort_deliverable_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(**kwargs: object) -> tuple[str | None, dict | None]:
        raise RuntimeError("llm down")

    monkeypatch.setattr(task_v2_service, "_generate_task_v2_deliverable", _boom)
    # Terminal failure handling must complete even if deliverable synthesis blows up.
    summary, output = await task_v2_service._best_effort_failure_deliverable(
        _FakeTaskV2(),
        task_history=[{"type": "extract", "status": "completed"}],
        context=object(),
    )
    assert summary is None
    assert output is None


@pytest.mark.asyncio
async def test_best_effort_deliverable_requests_partial_and_returns_output_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _capture(
        *, task_v2: object, task_history: list, context: object, screenshots: object, is_partial: bool
    ) -> tuple[str | None, object]:
        captured["is_partial"] = is_partial
        captured["history_len"] = len(task_history)
        captured["screenshots"] = screenshots
        return "best effort memo", {"rows": 7}

    monkeypatch.setattr(task_v2_service, "_generate_task_v2_deliverable", _capture)
    history = [{"type": "extract", "status": "completed", "extracted_data": {"a": 1}}]
    summary, output = await task_v2_service._best_effort_failure_deliverable(
        _FakeTaskV2(), task_history=history, context=object()
    )
    assert summary == "best effort memo"
    # Output is returned exactly as the model produced it -- no metadata injected into the user's schema.
    assert output == {"rows": 7}
    assert captured["is_partial"] is True
    assert captured["history_len"] == 1
    # Failure path skips screenshots (synthesizes from history only) to avoid a redundant capture.
    assert captured["screenshots"] is None


def test_summary_prompt_enforces_structure_and_partial_modes() -> None:
    common = dict(
        user_goal="Memo with sections A, B, C.",
        task_history=[{"type": "extract", "status": "completed"}],
        extracted_information_schema=None,
        local_datetime="2026-06-19T12:00:00",
    )
    full = prompt_engine.load_prompt("task_v2_summary", is_partial=False, **common)
    partial = prompt_engine.load_prompt("task_v2_summary", is_partial=True, **common)
    # Structure-fidelity guidance is always present (helps merged-section / wrong-count near-misses).
    assert "do not merge, drop, or renumber" in full
    assert "do not merge, drop, or renumber" in partial
    # Partial guidance only when the run stopped early.
    assert "could not be completed" not in full
    assert "could not be completed" in partial
    # Default opening is byte-for-byte unchanged for the happy path.
    assert full.startswith("The AI assistant has helped the user achieve the user goal in the web.")


@pytest.mark.asyncio
async def test_mark_task_v2_as_failed_threads_summary_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_update(task_v2_id: str, **kwargs: object) -> _FakeTaskV2:
        captured["task_v2_id"] = task_v2_id
        captured.update(kwargs)
        return _FakeTaskV2()

    async def _noop_webhook(task_v2: object) -> None:
        return None

    monkeypatch.setattr(task_v2_service, "_update_task_v2_status", _fake_update)
    monkeypatch.setattr(task_v2_service, "send_task_v2_webhook", _noop_webhook)
    # No workflow_run_id => the workflow-run failure side effect is skipped; this exercises the
    # persistence wiring that carries the best-effort deliverable onto a failed run.
    result = await task_v2_service.mark_task_v2_as_failed(
        task_v2_id="tsk_v2_x",
        organization_id="o_x",
        failure_reason="ran out of steps",
        summary="best-effort memo",
        output={"a": 1},
    )
    assert isinstance(result, _FakeTaskV2)
    assert captured["status"] == task_v2_service.TaskV2Status.failed
    assert captured["summary"] == "best-effort memo"
    assert captured["output"] == {"a": 1}
