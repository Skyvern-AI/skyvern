from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import add_log_context
from skyvern.webeye import real_browser_manager
from skyvern.webeye.browser_runtime_events import (
    BrowserRuntimeLogContext,
    browser_runtime_log_context,
    log_browser_acquisition_failure,
)
from skyvern.webeye.real_browser_state import RealBrowserState
from tests.unit.forge_log_capture import capture_runtime_logs


@pytest.fixture
def runtime_logs() -> Iterator[list[dict]]:
    with capture_runtime_logs() as logs:
        yield logs


@pytest.mark.parametrize("reconfigure", [False, True])
@pytest.mark.parametrize("exceptional_exit", [False, True])
def test_runtime_logs_restore_complete_configuration(reconfigure: bool, exceptional_exit: bool) -> None:
    previous = structlog.get_config()
    processors = previous["processors"]
    contents = processors.copy()
    capture = contextmanager(runtime_logs.__wrapped__)
    try:
        for _ in range(3):
            owner = SkyvernContext(task_id="capture-owner")
            try:
                with skyvern_context.scoped(owner), capture() as logs:
                    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
                    state._on_browser_context_closed(state.browser_context)
                    state._on_browser_context_closed(state.browser_context)
                    if reconfigure:
                        structlog.configure(
                            processors=list(structlog.get_config()["processors"]),
                            cache_logger_on_first_use=not previous["cache_logger_on_first_use"],
                        )
                    if exceptional_exit:
                        raise ValueError("capture interrupted")
            except ValueError as exc:
                assert exceptional_exit and str(exc) == "capture interrupted"
            assert len(logs) == len(owner.log) == 1
            assert logs[0]["browser_runtime_event"] == "runtime_ended"
            assert logs[0]["task_id"] == "capture-owner"
            current = structlog.get_config()
            assert current["processors"] is processors
            assert current["processors"] == contents
            assert current == previous
    finally:
        processors[:] = contents
        structlog.configure(**previous)


@pytest.mark.parametrize("emission_scope", ["owner", "nested", "after_owner"])
@pytest.mark.parametrize("workflow_owned", [True, False])
def test_browser_runtime_event_identity_survives_production_context_processor(
    runtime_logs: list[dict], emission_scope: str, workflow_owned: bool
) -> None:
    browser_context = MagicMock()
    owner = SkyvernContext(
        workflow_run_id="workflow-owner" if workflow_owned else None,
        task_id="task-owner",
        browser_session_id="session-owner",
    )
    unrelated = SkyvernContext(workflow_run_id="other-workflow", browser_session_id="session-owner", run_id="parent")
    listener_context, listener = MagicMock(), MagicMock()
    owner.download_popup_context_listeners["task-owner"] = [(listener_context, listener)]
    unrelated_listener_context, unrelated_listener = MagicMock(), MagicMock()
    unrelated.download_popup_context_listeners["other-task"] = [(unrelated_listener_context, unrelated_listener)]

    def emit() -> None:
        ambient = skyvern_context.current()
        state.record_browser_acquisition("reuse")
        state.record_browser_acquisition("reuse")
        state._on_browser_context_closed(browser_context)
        state._on_browser_context_closed(browser_context)
        assert skyvern_context.current() is ambient
        assert unrelated.download_popup_context_listeners["other-task"] == [
            (unrelated_listener_context, unrelated_listener)
        ]
        unrelated_listener_context.remove_listener.assert_not_called()

    with skyvern_context.scoped(owner):
        state = RealBrowserState(pw=MagicMock(), browser_context=browser_context)
        if emission_scope == "owner":
            emit()
        elif emission_scope == "nested":
            with skyvern_context.scoped(unrelated):
                emit()
        assert owner.download_popup_context_listeners["task-owner"] == [(listener_context, listener)]
        listener_context.remove_listener.assert_not_called()
    if emission_scope == "after_owner":
        owner.workflow_run_id = "mutated-workflow"
        owner.task_id = "mutated-task"
        with skyvern_context.scoped(unrelated):
            emit()
    events = [entry for entry in runtime_logs if entry.get("browser_runtime_event")]
    assert [entry["browser_runtime_event"] for entry in events] == ["acquire_result", "runtime_ended"]
    assert owner.log == [{key: value for key, value in entry.items() if key != "log_level"} for entry in events]
    assert unrelated.log == []
    for event in events:
        assert event["workflow_run_id"] == ("workflow-owner" if workflow_owned else None)
        assert event["task_id"] == "task-owner"
        assert event["browser_session_id"] == "session-owner"
        assert not event.get("run_id")
        assert "other-workflow" not in event["msg"]
        assert "mutated" not in str(event)


@pytest.mark.parametrize("workflow_owned", [True, False])
@pytest.mark.parametrize("matching_owner", [True, False])
def test_browser_runtime_acquisition_failure_uses_only_bound_owner_log(
    runtime_logs: list[dict], workflow_owned: bool, matching_owner: bool
) -> None:
    owner = SkyvernContext(
        workflow_run_id="workflow-owner" if workflow_owned else None,
        task_id="task-owner",
        browser_session_id="session-shared",
    )
    unrelated = SkyvernContext(workflow_run_id="other-workflow", browser_session_id="session-shared")
    with skyvern_context.scoped(owner):
        bound = BrowserRuntimeLogContext.for_run(
            workflow_run_id=owner.workflow_run_id,
            task_id=owner.task_id,
            browser_session_id=owner.browser_session_id,
        )
    with skyvern_context.scoped(unrelated), browser_runtime_log_context(bound):
        context = BrowserRuntimeLogContext.for_run(
            task_id="task-owner" if matching_owner else "different-task", browser_session_id="session-shared"
        )
        with (
            pytest.raises(ValueError, match="private detail"),
            log_browser_acquisition_failure(structlog.get_logger(), context, "attach"),
        ):
            raise ValueError("private detail")
        assert skyvern_context.current() is unrelated
    assert len(runtime_logs) == 1
    event = runtime_logs[0]
    assert event["outcome"] == "failure"
    assert event["workflow_run_id"] == ("workflow-owner" if workflow_owned and matching_owner else None)
    assert event["task_id"] == ("task-owner" if matching_owner else "different-task")
    assert owner.log == ([{key: value for key, value in event.items() if key != "log_level"}] if matching_owner else [])
    assert unrelated.log == []
    assert "private" not in str(event) and "other-workflow" not in str(event)


@pytest.mark.asyncio
async def test_browser_runtime_concurrent_owners_keep_separate_log_buffers(runtime_logs: list[dict]) -> None:
    owners = [SkyvernContext(workflow_run_id=f"workflow-{index}") for index in range(2)]
    unrelated = SkyvernContext(workflow_run_id="unrelated")
    entered = 0
    ready = asyncio.Event()

    async def run(owner: SkyvernContext) -> None:
        nonlocal entered
        with skyvern_context.scoped(owner):
            state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
            bound = BrowserRuntimeLogContext.for_run(workflow_run_id=owner.workflow_run_id)
            entered += 1
            if entered == len(owners):
                ready.set()
            await ready.wait()
            with skyvern_context.scoped(unrelated), browser_runtime_log_context(bound):
                state.bind_runtime_event_context(BrowserRuntimeLogContext.for_run(browser_session_id="session-shared"))
                state.record_browser_acquisition("reuse")
                await asyncio.sleep(0)
                state._on_browser_context_closed(state.browser_context)

    await asyncio.gather(*(run(owner) for owner in owners))
    assert len(runtime_logs) == 4
    assert unrelated.log == []
    for owner in owners:
        assert [entry["browser_runtime_event"] for entry in owner.log] == ["acquire_result", "runtime_ended"]
        assert {entry["workflow_run_id"] for entry in owner.log} == {owner.workflow_run_id}


def test_browser_runtime_adoption_moves_log_destination_to_new_owner(runtime_logs: list[dict]) -> None:
    previous = SkyvernContext(workflow_run_id="workflow-previous", browser_session_id="session-shared")
    owner = SkyvernContext(workflow_run_id="workflow-owner", browser_session_id="session-shared")
    with skyvern_context.scoped(previous):
        state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
        state.record_browser_acquisition("attach")
    with skyvern_context.scoped(owner):
        state.bind_runtime_event_context(
            BrowserRuntimeLogContext.for_run(workflow_run_id=owner.workflow_run_id, task_id="task-in-workflow")
        )
        state.record_browser_acquisition("reuse")
    with skyvern_context.scoped(previous):
        state._on_browser_context_closed(state.browser_context)
    assert len(runtime_logs) == 3
    assert [entry["browser_runtime_event"] for entry in previous.log] == ["acquire_result"]
    assert [entry["browser_runtime_event"] for entry in owner.log] == ["acquire_result", "runtime_ended"]
    assert {entry["workflow_run_id"] for entry in owner.log} == {"workflow-owner"}


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_owned", [True, False])
async def test_browser_runtime_task_pbs_boundary_preserves_owner_log(
    runtime_logs: list[dict], workflow_owned: bool
) -> None:
    owner = SkyvernContext(workflow_run_id="workflow-owner" if workflow_owned else None, task_id="task-owner")
    task = MagicMock(task_id=owner.task_id, workflow_run_id=owner.workflow_run_id, organization_id="org-owner")

    async def acquire(session_id: str, **kwargs: object) -> None:
        with log_browser_acquisition_failure(
            structlog.get_logger(),
            BrowserRuntimeLogContext.for_run(task_id="task-owner", browser_session_id=session_id),
            "attach",
        ):
            raise ValueError("acquisition failed")

    with skyvern_context.scoped(owner), patch.object(real_browser_manager, "app") as app:
        app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(return_value="generation-owner")
        app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=acquire)
        with pytest.raises(ValueError, match="acquisition failed"):
            await real_browser_manager.RealBrowserManager().get_or_create_for_task(task, browser_session_id="session")
    events = [entry for entry in runtime_logs if entry.get("browser_runtime_event")]
    persisted = [entry for entry in owner.log if entry.get("browser_runtime_event")]
    assert len(events) == len(persisted) == 1
    assert persisted[0] == {key: value for key, value in events[0].items() if key != "log_level"}
    assert persisted[0]["workflow_run_id"] == owner.workflow_run_id
    assert persisted[0]["task_id"] == owner.task_id


def test_add_log_context_tolerates_partial_context() -> None:
    # A partial context (e.g. a SimpleNamespace test double) exposes only some of the
    # fields add_log_context reads. It must fail-open on the missing ones rather than raise.
    context = SimpleNamespace(organization_id="org_1", task_id="task_1", log=[])
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "warning", {"msg": "hi"})

    assert event_dict["organization_id"] == "org_1"
    assert event_dict["task_id"] == "task_1"
    assert "request_id" not in event_dict


def test_correlation_ids_are_searchable_in_msg() -> None:
    # Datadog free-text search only scans the message content; pasting an id like
    # pbs_x/wr_x must keep matching, so the ids are appended to msg (SKY-13848 kept
    # the arbitrary-kwarg copy out — only these bounded ids go in).
    context = SkyvernContext(
        organization_id="o_1",
        workflow_run_id="wr_1",
        browser_session_id="pbs_1",
    )
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "info", {"msg": "Closing browser", "payload": "x" * 500})

    assert event_dict["msg"] == "Closing browser | organization_id=o_1, workflow_run_id=wr_1, browser_session_id=pbs_1"
    assert event_dict["browser_session_id"] == "pbs_1"


def test_kwarg_correlation_id_is_searchable_without_context() -> None:
    # Worker code paths log ids as kwargs before any skyvern_context exists; those must
    # be free-text searchable too.
    with patch.object(skyvern_context, "current", return_value=None):
        event_dict = add_log_context(None, "info", {"msg": "Begin browser session", "browser_session_id": "pbs_2"})

    assert event_dict["msg"] == "Begin browser session | browser_session_id=pbs_2"


def test_codeblock_execution_path_is_a_field_not_a_msg_suffix() -> None:
    # The arm is a grouping facet for the secure-vs-legacy monitors, not a correlation id,
    # so it must stay out of the searchable-id suffix (SKY-13848 bounds what goes into msg).
    context = SkyvernContext(workflow_run_id="wr_1", codeblock_execution_path="secure_runner")
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "warning", {"msg": "Block failed"})

    assert event_dict["codeblock_execution_path"] == "secure_runner"
    assert event_dict["msg"] == "Block failed | workflow_run_id=wr_1"


def test_org_age_bucket_is_a_field_not_a_msg_suffix() -> None:
    # The org-age bucket is a low-cardinality grouping facet for new-org diagnostics, not a
    # correlation id, so like codeblock_execution_path it rides existing run-lifecycle log lines
    # as a structured field and stays out of the searchable-id msg suffix.
    context = SkyvernContext(workflow_run_id="wr_1", org_age_bucket="first_day")
    with patch.object(skyvern_context, "current", return_value=context):
        event_dict = add_log_context(None, "info", {"msg": "Run task activity started"})

    assert event_dict["org_age_bucket"] == "first_day"
    assert event_dict["msg"] == "Run task activity started | workflow_run_id=wr_1"


def test_a_dropped_coroutine_warning_names_its_call_site() -> None:
    """CPython emits "coroutine ... was never awaited" from the coroutine's __del__, so the
    file:line it carries is wherever the collector ran, never the code that dropped it. Origin
    tracking is what puts the creating frame in the warning (SKY-15069).

    Run out of process: setup_logger() replaces the root handlers, the structlog configuration and
    several logger levels, and a subprocess also puts the warning on the same stderr stream the log
    collector reads in production.
    """
    program = textwrap.dedent(
        """
        import gc

        from skyvern.forge.sdk.forge_log import setup_logger

        setup_logger()

        async def dropped_coroutine() -> None:
            return None

        def sig_handler() -> None:
            dropped_coroutine()

        sig_handler()
        gc.collect()
        """
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True)

    assert "was never awaited" in result.stderr
    assert "Coroutine created at" in result.stderr
    assert "in sig_handler" in result.stderr
