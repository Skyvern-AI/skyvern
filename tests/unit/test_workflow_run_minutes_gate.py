"""Run minutes count compute, so a workflow run that never reached ``running``
must contribute no minutes (SKY-14608) -- but the exclusion itself is exported,
tagged ``excluded_reason="never_started"``, so the removed cohort stays
observable instead of silently vanishing.

Both terminal writers derive ``duration_seconds`` from
``COALESCE(started_at, created_at)``. On a run finalized straight out of the
queue that fallback measures queue age, and the run held no pod at all -- which
is why the emission, not the duration, is what carries the exclusion. The two
writers carry independent copies of the logic, so both are covered here.

The same rule governs the task_v1 emitter in ``Agent.update_task``, which reads
the task once on entry and finalizes it later: it is covered here too, because
only the post-claim row can say whether the task started.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunEngine


def _make_row(*, started: bool) -> MagicMock:
    now = datetime.now(UTC)
    row = MagicMock()
    row.workflow_run_id = "wr_gate"
    row.workflow_id = "wf_gate"
    row.organization_id = "org_gate"
    row.parent_workflow_run_id = None
    row.created_at = now - timedelta(minutes=30)
    row.started_at = (now - timedelta(minutes=20)) if started else None
    row.status = WorkflowRunStatus.canceled
    row.run_with = None
    row.ai_fallback = False
    row.trigger_type = None
    row.workflow_schedule_id = None
    row.failure_category = None
    return row


@pytest.fixture
def record_run_duration(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    emitter = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "record_run_duration", emitter)
    monkeypatch.setattr(WorkflowService, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(WorkflowService, "_schedule_workflow_run_terminal_hooks", MagicMock())
    monkeypatch.setattr(WorkflowService, "_sync_task_run_from_workflow_run", AsyncMock())
    return emitter


def _assert_emission(record_run_duration: AsyncMock, *, started: bool) -> None:
    assert record_run_duration.await_count == 1
    kwargs = record_run_duration.await_args.kwargs
    if started:
        assert kwargs["excluded_reason"] is None
        assert kwargs["duration_seconds"] == pytest.approx(20 * 60, abs=5)
    else:
        # Excluded, not silent: the recorder turns this into a zero-minute sample
        # tagged excluded=never_started, so sums stay compute-only while the
        # exclusion stays countable.
        assert kwargs["excluded_reason"] == "never_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [True, False])
async def test_terminal_write_emits_minutes_only_for_runs_that_started(
    record_run_duration: AsyncMock,
    started: bool,
) -> None:
    await WorkflowService()._after_workflow_run_status_write(_make_row(started=started), WorkflowRunStatus.canceled)

    _assert_emission(record_run_duration, started=started)


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [True, False])
async def test_conditional_cancel_emits_minutes_only_for_runs_that_started(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
    started: bool,
) -> None:
    row = _make_row(started=started)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=row),
    )

    await WorkflowService().mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_gate")

    _assert_emission(record_run_duration, started=started)


def _make_task(*, status: TaskStatus, started_at: datetime | None, finished_at: datetime | None = None) -> Task:
    now = datetime.now(UTC)
    return Task(
        task_id="tsk_gate",
        organization_id="org_gate",
        url="https://example.com",
        status=status,
        created_at=now - timedelta(minutes=30),
        modified_at=now,
        started_at=started_at,
        finished_at=finished_at,
        workflow_run_id=None,
    )


@pytest.mark.asyncio
async def test_task_v1_emission_reads_started_at_from_the_claim_not_the_entry_read(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    """A worker can stamp ``started_at`` between ``update_task``'s entry read and its
    finished_at claim. Deciding the exclusion off the entry read would bill that
    task's real compute as ``never_started`` and emit zero minutes for it.
    """
    now = datetime.now(UTC)
    entry_task = _make_task(status=TaskStatus.queued, started_at=None)
    claimed_task = _make_task(
        status=TaskStatus.canceled,
        started_at=now - timedelta(minutes=9),
        finished_at=now,
    )
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=entry_task))
    monkeypatch.setattr(
        app.DATABASE.tasks,
        "update_task_and_claim_finish",
        AsyncMock(return_value=(claimed_task, True)),
    )
    monkeypatch.setattr(agent_module, "save_task_logs", AsyncMock())

    await ForgeAgent().update_task(entry_task, status=TaskStatus.canceled)

    assert record_run_duration.await_count == 1
    kwargs = record_run_duration.await_args.kwargs
    assert kwargs.get("excluded_reason") is None
    assert kwargs["duration_seconds"] == pytest.approx(9 * 60, abs=5)


FINALLY_BLOCK_SECONDS = 5 * 60
BODY_SECONDS = 20 * 60


class _Clock:
    """The service reads wall clock through ``service_module.datetime``. Driving it by hand is
    what makes the body's minutes and the finally block's minutes two distinct intervals
    rather than two reads of the same instant."""

    def __init__(self, start: datetime) -> None:
        self.now_value = start

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)

    def now(self, tz: tzinfo | None = None) -> datetime:
        return self.now_value if tz is None else self.now_value.astimezone(tz)


class _FakeWorkflowRunStore:
    """The two writers ``_update_workflow_run_status`` picks between, over one mutable row: a
    conditional claim that refuses an already-terminal row, and the unconditional overwrite.
    A terminal write stamps ``finished_at`` at the clock's current instant, as the real row does.
    """

    def __init__(self, row: SimpleNamespace, clock: _Clock) -> None:
        self.row = row
        self.clock = clock

    def snapshot(self) -> SimpleNamespace:
        return copy.copy(self.row)

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> SimpleNamespace:
        return self.snapshot()

    async def update_workflow_run_if_not_final(
        self, workflow_run_id: str, status: WorkflowRunStatus, **_: object
    ) -> SimpleNamespace | None:
        if self.row.status.is_final():
            return None
        self.row.status = status
        if status.is_final():
            self.row.finished_at = self.clock.now_value
        return self.snapshot()

    async def update_workflow_run(
        self, workflow_run_id: str, status: WorkflowRunStatus | None = None, **_: object
    ) -> SimpleNamespace:
        if status is not None:
            self.row.status = status
        return self.snapshot()


@pytest.mark.asyncio
async def test_finally_block_re_finalization_records_only_the_minutes_it_added(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    """A run whose body terminalized it and whose workflow declares a finally block is written
    back to ``running`` so the block can execute, then terminalized again. Both writes flip a
    non-terminal row to a terminal one, so recording each one's full ``now - started_at``
    bills the body twice (SKY-14606). The second write owes only the compute the first did
    not measure -- and it does owe that, because the finally block is real work on the pod.
    """
    clock = _Clock(datetime.now(UTC))
    started_at = clock.now_value
    store = _FakeWorkflowRunStore(
        SimpleNamespace(
            workflow_run_id="wr_finally",
            workflow_id="wf_finally",
            workflow_permanent_id="wpid_finally",
            organization_id="org_finally",
            parent_workflow_run_id=None,
            status=WorkflowRunStatus.running,
            failure_reason=None,
            failure_category=None,
            created_at=started_at,
            started_at=started_at,
            finished_at=None,
            run_with="agent",
            ai_fallback=False,
            trigger_type=None,
            workflow_schedule_id=None,
            browser_session_id=None,
            browser_profile_id=None,
            browser_address=None,
            start_fresh_browser=None,
            reuse_browser_session=None,
            ignore_inherited_workflow_system_prompt=False,
            proxy_location=None,
            max_elapsed_time_minutes=None,
            code_gen=False,
        ),
        clock,
    )
    workflow = SimpleNamespace(
        workflow_id="wf_finally",
        workflow_permanent_id="wpid_finally",
        organization_id="org_finally",
        title="Finally workflow",
        persist_browser_session=False,
        reuse_browser_session=False,
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[SimpleNamespace(block_type=BlockType.TASK)],
        ),
    )
    organization = SimpleNamespace(organization_id="org_finally")

    monkeypatch.setattr(service_module, "datetime", SimpleNamespace(now=clock.now))
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(
            initialize_workflow_run_context=AsyncMock(),
            get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
        ),
    )
    monkeypatch.setattr(service_module.app, "DATABASE", SimpleNamespace(workflow_runs=store))
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(service_module, "_get_workflow_run_max_elapsed_timeout_seconds", lambda _workflow_run: 10.0)

    svc = WorkflowService()

    async def terminalize_inside_body(**_: object) -> tuple[SimpleNamespace, set[str]]:
        clock.advance(BODY_SECONDS)
        await svc.mark_workflow_run_as_terminated(
            workflow_run_id="wr_finally",
            failure_reason="terminate criterion matched",
        )
        return store.snapshot(), set()

    statuses_seen_by_finally_block: list[WorkflowRunStatus] = []

    async def observe_finally_block(**_: object) -> None:
        statuses_seen_by_finally_block.append(store.row.status)
        clock.advance(FINALLY_BLOCK_SECONDS)
        return None

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(side_effect=lambda **_: store.snapshot()))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "bind_browser_action_policy", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(side_effect=lambda **_: store.snapshot()))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(side_effect=terminalize_inside_body))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", AsyncMock(side_effect=observe_finally_block))
    monkeypatch.setattr(svc, "clean_up_workflow", AsyncMock())

    await svc.execute_workflow(workflow_run_id="wr_finally", api_key=None, organization=organization)

    # The row really was re-opened and re-finalized. Without both flips there is nothing to
    # double-count and the durations below would pass for the wrong reason.
    assert statuses_seen_by_finally_block == [WorkflowRunStatus.running]
    assert store.row.status == WorkflowRunStatus.terminated

    # Two terminal writes, two samples: dropping the second would erase the finally block's
    # own compute, which is as wrong as counting the body twice.
    assert record_run_duration.await_count == 2
    body_call, re_finalize_call = record_run_duration.await_args_list
    assert [call.kwargs["status"] for call in (body_call, re_finalize_call)] == [str(WorkflowRunStatus.terminated)] * 2
    assert body_call.kwargs["excluded_reason"] is None
    assert body_call.kwargs["duration_seconds"] == pytest.approx(BODY_SECONDS)
    assert re_finalize_call.kwargs["duration_seconds"] == pytest.approx(FINALLY_BLOCK_SECONDS)

    # The invariant the delta form exists to hold: the samples partition the run's wall clock
    # rather than overlapping on the body.
    wall_clock_seconds = (clock.now_value - started_at).total_seconds()
    assert wall_clock_seconds == pytest.approx(BODY_SECONDS + FINALLY_BLOCK_SECONDS)
    assert sum(call.kwargs["duration_seconds"] for call in record_run_duration.await_args_list) == pytest.approx(
        wall_clock_seconds
    )


@pytest.mark.asyncio
async def test_duration_metrics_log_carries_task_run_type_for_a_bare_task(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    # The v1-vs-v3 wall-time dashboard needs an engine discriminator on "Task duration metrics";
    # a bare task resolves it from its own task_runs row (SKY-15499).
    from structlog.testing import capture_logs

    from skyvern.schemas.run_enums import RunType

    now = datetime.now(UTC)
    entry_task = _make_task(status=TaskStatus.running, started_at=now - timedelta(minutes=5))
    claimed_task = _make_task(status=TaskStatus.completed, started_at=now - timedelta(minutes=5), finished_at=now)
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=entry_task))
    monkeypatch.setattr(
        app.DATABASE.tasks, "update_task_and_claim_finish", AsyncMock(return_value=(claimed_task, True))
    )
    monkeypatch.setattr(app.DATABASE.tasks, "get_run", AsyncMock(return_value=MagicMock(task_run_type=RunType.task_v3)))
    monkeypatch.setattr(agent_module, "save_task_logs", AsyncMock())

    with capture_logs() as logs:
        await ForgeAgent().update_task(entry_task, status=TaskStatus.completed)

    duration_logs = [e for e in logs if e.get("event") == "Task duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_run_type"] == "task_v3"


@pytest.mark.asyncio
async def test_duration_metrics_log_resolves_engine_from_the_block_for_a_workflow_task(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    # Workflow-block tasks have no task_runs row; the block row's RESOLVED engine is the
    # discriminator (task_runs cover bare tasks only).
    from structlog.testing import capture_logs

    from skyvern.schemas.run_enums import RunEngine

    now = datetime.now(UTC)
    entry_task = _make_task(status=TaskStatus.running, started_at=now - timedelta(minutes=5))
    entry_task.workflow_run_id = "wr_gate"
    claimed_task = _make_task(status=TaskStatus.terminated, started_at=now - timedelta(minutes=5), finished_at=now)
    claimed_task.workflow_run_id = "wr_gate"
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=entry_task))
    monkeypatch.setattr(
        app.DATABASE.tasks, "update_task_and_claim_finish", AsyncMock(return_value=(claimed_task, True))
    )
    monkeypatch.setattr(
        app.DATABASE.observer,
        "get_workflow_run_block_engine_by_task_id",
        AsyncMock(return_value=RunEngine.skyvern_v1),
    )
    monkeypatch.setattr(agent_module, "save_task_logs", AsyncMock())

    with capture_logs() as logs:
        await ForgeAgent().update_task(entry_task, status=TaskStatus.terminated, failure_reason="blocked")

    duration_logs = [e for e in logs if e.get("event") == "Task duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_run_type"] == "task_v1"


@pytest.mark.asyncio
async def test_duration_metrics_log_survives_a_failed_run_type_resolution(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    # The discriminator is best-effort telemetry: a DB error must neither drop the log nor fail the
    # terminal update.
    from structlog.testing import capture_logs

    now = datetime.now(UTC)
    entry_task = _make_task(status=TaskStatus.running, started_at=now - timedelta(minutes=5))
    claimed_task = _make_task(status=TaskStatus.completed, started_at=now - timedelta(minutes=5), finished_at=now)
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=entry_task))
    monkeypatch.setattr(
        app.DATABASE.tasks, "update_task_and_claim_finish", AsyncMock(return_value=(claimed_task, True))
    )
    monkeypatch.setattr(app.DATABASE.tasks, "get_run", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(agent_module, "save_task_logs", AsyncMock())

    with capture_logs() as logs:
        await ForgeAgent().update_task(entry_task, status=TaskStatus.completed)

    duration_logs = [e for e in logs if e.get("event") == "Task duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_run_type"] is None


def test_run_type_by_engine_mapping_is_exhaustive() -> None:
    # A new RunEngine member silently maps to None otherwise -- pin the dict to the enum.
    from skyvern.forge.agent import _RUN_TYPE_BY_ENGINE
    from skyvern.schemas.run_enums import RunEngine, RunType

    assert set(_RUN_TYPE_BY_ENGINE) == set(RunEngine)
    assert set(_RUN_TYPE_BY_ENGINE.values()) <= {t.value for t in RunType}


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


def _pin_workflow_block_engine_arm(context: SkyvernContext, *, workflow_run_id: str, engine: RunEngine | None) -> None:
    # workflow_run_id itself is what marks the context as belonging to THIS run -- an
    # out-of-band finalizer's context (if any) belongs to whatever request triggered it, not
    # to this run, so it never has this set.
    context.workflow_run_id = workflow_run_id
    context.workflow_block_engine_resolved_run_id = workflow_run_id
    context.workflow_block_engine_override = engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,expected_arm",
    [
        (RunEngine.skyvern_v3, "treatment"),
        (None, "control"),
        # Finding 2: only an explicit v3 override reads as treatment -- a future non-v3
        # override on this field must not silently pass a truthiness check.
        (RunEngine.openai_cua, "control"),
    ],
)
async def test_after_status_write_duration_log_carries_the_pinned_arm(
    scoped_context: SkyvernContext,
    record_run_duration: AsyncMock,
    engine: RunEngine | None,
    expected_arm: str,
) -> None:
    # SKY-15561: the arm resolved once at execution start (resolve_workflow_block_engine_arm)
    # is pinned on the run's own context, so the terminal writer reads it back from there --
    # no new DB lookup needed at finalize time.
    from structlog.testing import capture_logs

    _pin_workflow_block_engine_arm(scoped_context, workflow_run_id="wr_gate", engine=engine)

    with capture_logs() as logs:
        await WorkflowService()._after_workflow_run_status_write(_make_row(started=True), WorkflowRunStatus.canceled)

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] == expected_arm


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,expected_arm", [(RunEngine.skyvern_v3, "treatment"), (None, "control")])
async def test_conditional_cancel_duration_log_carries_the_pinned_arm(
    monkeypatch: pytest.MonkeyPatch,
    scoped_context: SkyvernContext,
    record_run_duration: AsyncMock,
    engine: RunEngine | None,
    expected_arm: str,
) -> None:
    # Same field, the other emission site (SKY-15561).
    from structlog.testing import capture_logs

    row = _make_row(started=True)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=row),
    )
    _pin_workflow_block_engine_arm(scoped_context, workflow_run_id="wr_gate", engine=engine)

    with capture_logs() as logs:
        await WorkflowService().mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_gate")

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] == expected_arm


@pytest.mark.asyncio
async def test_duration_log_reads_unknown_when_no_context_is_current(
    record_run_duration: AsyncMock,
) -> None:
    # No context at all is current (e.g. a worker process finalizing with nothing bound):
    # attribution is lost, not "confirmed control" -- the field must read "unknown".
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await WorkflowService()._after_workflow_run_status_write(_make_row(started=True), WorkflowRunStatus.canceled)

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] == "unknown"


@pytest.mark.asyncio
async def test_duration_log_reads_none_when_this_runs_own_context_never_resolved_an_arm(
    scoped_context: SkyvernContext,
    record_run_duration: AsyncMock,
) -> None:
    # This run's own context is current (e.g. task_v2 / cached-script helper paths, which never
    # call resolve_workflow_block_engine_arm) but never resolved an arm: genuinely never
    # entered the A/B, distinct from the out-of-band "unknown" case below.
    from structlog.testing import capture_logs

    scoped_context.workflow_run_id = "wr_gate"

    with capture_logs() as logs:
        await WorkflowService()._after_workflow_run_status_write(_make_row(started=True), WorkflowRunStatus.canceled)

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] is None


@pytest.mark.asyncio
async def test_duration_log_reads_unknown_for_a_different_runs_context(
    scoped_context: SkyvernContext,
    record_run_duration: AsyncMock,
) -> None:
    # The exact API-cancel/stuck-run-sweep shape (SKY-15561 finding 1): the finalizer for
    # wr_gate runs in a request/task whose current context belongs to a different run
    # (wr_other) entirely. Reading that as "control" would silently bias per-arm duration
    # reads against exactly the canceled/timed-out population; it must read "unknown".
    from structlog.testing import capture_logs

    _pin_workflow_block_engine_arm(scoped_context, workflow_run_id="wr_other", engine=RunEngine.skyvern_v3)

    with capture_logs() as logs:
        await WorkflowService()._after_workflow_run_status_write(_make_row(started=True), WorkflowRunStatus.canceled)

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] == "unknown"


@pytest.mark.asyncio
async def test_duration_log_survives_a_failed_arm_lookup(
    monkeypatch: pytest.MonkeyPatch,
    record_run_duration: AsyncMock,
) -> None:
    # The arm read is best-effort telemetry: a lookup failure must neither drop the log nor
    # break run finalization (SKY-15561, mirrors SKY-15499's task_run_type discipline).
    from structlog.testing import capture_logs

    monkeypatch.setattr(
        service_module,
        "resolved_workflow_block_engine_arm_label",
        MagicMock(side_effect=RuntimeError("context lookup blew up")),
    )

    with capture_logs() as logs:
        await WorkflowService()._after_workflow_run_status_write(_make_row(started=True), WorkflowRunStatus.canceled)

    duration_logs = [e for e in logs if e.get("event") == "Workflow run duration metrics"]
    assert len(duration_logs) == 1
    assert duration_logs[0]["task_v3_ab_arm"] == "unknown"
    assert record_run_duration.await_count == 1
