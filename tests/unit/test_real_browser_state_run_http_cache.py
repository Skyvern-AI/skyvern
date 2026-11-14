"""Each run bound to a browser context gets its own static response store generation, and a release turns it off."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.webeye.browser_runtime_events import BrowserRuntimeLogContext
from skyvern.webeye.real_browser_state import RealBrowserState


@pytest.fixture
def generations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str | None]]:
    sent: list[tuple[Any, str | None]] = []
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "set_browser_run_http_cache",
        lambda context, generation, **_: sent.append((context, generation)),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_browser_run_released", lambda context: None)
    return sent


def _state(context: Any = None) -> RealBrowserState:
    pw: Any = type("_PW", (), {"stop": AsyncMock()})()
    return RealBrowserState(pw=pw, browser_context=context)


def test_runs_sharing_one_context_never_share_a_generation(generations: list[tuple[Any, str | None]]) -> None:
    context = MagicMock()
    state = _state(context)
    created = generations[-1][1]

    state.bind_runtime_event_context(BrowserRuntimeLogContext.for_run(workflow_run_id="wr_first"))
    first = generations[-1][1]
    state.mark_run_released()
    released = generations[-1][1]
    state.bind_runtime_event_context(BrowserRuntimeLogContext.for_run(workflow_run_id="wr_second"))
    second = generations[-1][1]

    assert all(sent_context is context for sent_context, _ in generations)
    assert released is None
    assert None not in (created, first, second)
    assert len({created, first, second}) == 3


def test_a_context_attached_after_release_stays_off_until_the_next_bind(
    generations: list[tuple[Any, str | None]],
) -> None:
    state = _state()
    state.bind_runtime_event_context(BrowserRuntimeLogContext.for_run(workflow_run_id="wr_lazy"))
    assert generations == []

    context = MagicMock()
    state.browser_context = context
    assert generations[-1][0] is context and generations[-1][1] is not None

    state.mark_run_released()
    state.browser_context = MagicMock()
    assert generations[-1][1] is None


@pytest.mark.parametrize("change", ["same_owner", "workflow_task", "owner", "session", "release", "context"])
def test_reacquisition_only_preserves_the_same_live_authority(
    generations: list[tuple[Any, str | None]], change: str
) -> None:
    state = _state(MagicMock())
    first = BrowserRuntimeLogContext.for_run(workflow_run_id="wr_owner", browser_session_id="pbs_owner")
    state.bind_runtime_event_context(first)
    count = len(generations)
    generation = generations[-1][1]
    if change == "release":
        state.mark_run_released()
    if change == "context":
        state.browser_context = MagicMock()
    state.bind_runtime_event_context(
        BrowserRuntimeLogContext.for_run(
            workflow_run_id="wr_other" if change == "owner" else "wr_owner",
            task_id="task_within_workflow" if change == "workflow_task" else None,
            browser_session_id="pbs_other" if change == "session" else "pbs_owner",
        )
    )
    if change in {"same_owner", "workflow_task"}:
        assert len(generations) == count and generations[-1][1] == generation
    else:
        assert generations[-1][1] not in (None, generation)
