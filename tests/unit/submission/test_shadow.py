from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.submission import shadow
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.run_enums import RunEngine, RunType
from skyvern.schemas.steps import AgentStepOutput, BrowserMetadata
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.responses import ActionResult
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_state import BrowserState

NOW = datetime(2026, 7, 9, 12, 0, 0)


def _task(task_id: str = "task-1", *, workflow_run_id: str | None = None) -> Task:
    return Task(
        task_id=task_id,
        status=TaskStatus.completed,
        created_at=NOW,
        modified_at=NOW,
        organization_id="org-1",
        workflow_run_id=workflow_run_id,
        url="https://example.com/form",
    )


def _submit_action(*, task_id: str = "task-1", step_id: str = "step-1") -> ClickAction:
    return ClickAction(
        element_id="submit-button",
        task_id=task_id,
        step_id=step_id,
        created_at=NOW + timedelta(seconds=2),
        skyvern_element_data={
            "tagName": "button",
            "attributes": {"type": "submit"},
            "text": "Submit",
        },
    )


def _step(
    step_id: str,
    order: int,
    *,
    task_id: str = "task-1",
    url: str = "https://example.com/form",
    action: ClickAction | None = None,
) -> Step:
    pairs = [] if action is None else [(action, [ActionResult(success=True)])]
    return Step(
        created_at=NOW + timedelta(seconds=order * 10),
        modified_at=NOW + timedelta(seconds=order * 10 + 5),
        task_id=task_id,
        step_id=step_id,
        status=StepStatus.completed,
        output=AgentStepOutput(
            actions_and_results=pairs,
            browser_metadata=BrowserMetadata(website_url=url),
        ),
        order=order,
        is_last=False,
        organization_id="org-1",
    )


def _workflow_run() -> WorkflowRun:
    return WorkflowRun(
        workflow_run_id="workflow-run-1",
        workflow_id="workflow-1",
        workflow_permanent_id="wpid-1",
        organization_id="org-1",
        status=WorkflowRunStatus.completed,
        created_at=NOW,
        modified_at=NOW,
    )


def _browser_state() -> BrowserState:
    return cast(
        BrowserState,
        SimpleNamespace(
            browser_artifacts=BrowserArtifacts(),
        ),
    )


def _har(*, method: str = "POST", status: int = 200, offset_seconds: int = 12) -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "startedDateTime": (NOW + timedelta(seconds=offset_seconds)).replace(tzinfo=UTC).isoformat(),
                        "request": {"method": method, "url": "https://example.com/submit?token=private"},
                        "response": {"status": status, "content": {"mimeType": "application/json"}},
                        "_resourceType": "xhr",
                    }
                ]
            }
        }
    ).encode()


def _artifact(artifact_id: str, step_id: str, artifact_type: ArtifactType) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        uri=f"s3://artifacts/{artifact_id}",
        task_id="task-1",
        step_id=step_id,
        organization_id="org-1",
        created_at=NOW,
        modified_at=NOW,
    )


def _install_task_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    steps: list[Step],
    actions: list[ClickAction],
    artifact_payloads: dict[str, bytes | None],
    include_visible_artifacts: bool = True,
) -> tuple[SimpleNamespace, AsyncMock]:
    visible_by_step = {
        step.step_id: _artifact(f"visible-{step.step_id}", step.step_id, ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT)
        for step in steps
    }
    html_by_step = {
        step.step_id: _artifact(f"html-{step.step_id}", step.step_id, ArtifactType.HTML_SCRAPE) for step in steps
    }

    async def get_artifacts_for_task_step(
        task_id: str,
        step_id: str,
        organization_id: str | None = None,
    ) -> list[Artifact]:
        assert task_id == "task-1"
        assert organization_id == "org-1"
        if include_visible_artifacts:
            return [html_by_step[step_id], visible_by_step[step_id]]
        return [html_by_step[step_id]]

    tasks_repo = SimpleNamespace(
        get_task_steps=AsyncMock(return_value=steps),
        get_task_actions_hydrated=AsyncMock(return_value=actions),
        get_run=AsyncMock(return_value=SimpleNamespace(task_run_type=RunType.task_v1)),
    )
    artifacts_repo = SimpleNamespace(get_artifacts_for_task_step=AsyncMock(side_effect=get_artifacts_for_task_step))
    observer_repo = SimpleNamespace(get_workflow_run_block_by_task_id=AsyncMock())
    database = SimpleNamespace(tasks=tasks_repo, artifacts=artifacts_repo, observer=observer_repo)
    retrieve_artifact = AsyncMock(side_effect=lambda artifact: artifact_payloads.get(artifact.artifact_id))
    monkeypatch.setattr(shadow.app, "DATABASE", database)
    monkeypatch.setattr(shadow.app, "ARTIFACT_MANAGER", SimpleNamespace(retrieve_artifact=retrieve_artifact))
    return database, retrieve_artifact


@pytest.fixture(autouse=True)
def _clear_pending_tasks() -> None:
    shadow._PENDING_SUBMISSION_SHADOW_TASKS.clear()
    yield
    shadow._PENDING_SUBMISSION_SHADOW_TASKS.clear()


def test_flag_off_returns_before_pruning_or_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow.settings, "SKYVERN_SUBMISSION_SIGNAL_SHADOW", False)
    monkeypatch.setattr(shadow, "_prune_pending", Mock(side_effect=AssertionError("must not prune")))
    task = _task()
    last_step = _step("step-1", 1)

    with capture_logs() as logs:
        scheduled = shadow.schedule_submission_signal_shadow(
            har_data=b"",
            browser_state=_browser_state(),
            last_step=last_step,
            task=task,
        )

    assert scheduled is None
    assert shadow._PENDING_SUBMISSION_SHADOW_TASKS == set()
    assert logs == []


def test_pending_task_cap_drops_work_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    class _PendingMarker:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(shadow.settings, "SKYVERN_SUBMISSION_SIGNAL_SHADOW", True)
    assert shadow._MAX_PENDING == 20
    pending: set[Any] = {_PendingMarker() for _ in range(shadow._MAX_PENDING)}
    monkeypatch.setattr(shadow, "_PENDING_SUBMISSION_SHADOW_TASKS", pending)

    with capture_logs() as logs:
        scheduled = shadow.schedule_submission_signal_shadow(
            har_data=b"",
            browser_state=_browser_state(),
            last_step=_step("step-1", 1),
            task=_task(),
        )

    assert scheduled is None
    assert len(shadow._PENDING_SUBMISSION_SHADOW_TASKS) == shadow._MAX_PENDING
    assert logs == [
        {
            "event": "submission_shadow_task_cap_reached",
            "log_level": "warning",
            "pending": shadow._MAX_PENDING,
        },
        {
            "event": "submission_signal_shadow",
            "log_level": "info",
            "reason": "pending_cap",
            "status": "skipped",
        },
    ]


def test_scheduler_swallows_setup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    log = SimpleNamespace(debug=Mock())
    monkeypatch.setattr(shadow.settings, "SKYVERN_SUBMISSION_SIGNAL_SHADOW", True)
    monkeypatch.setattr(shadow, "_prune_pending", Mock(side_effect=RuntimeError("private scheduler detail")))
    monkeypatch.setattr(shadow, "LOG", log)

    scheduled = shadow.schedule_submission_signal_shadow(
        har_data=b"",
        browser_state=_browser_state(),
        last_step=_step("step-1", 1),
        task=_task(),
    )

    assert scheduled is None
    log.debug.assert_called_once_with("submission_shadow_schedule_failed", error_type="RuntimeError")


@pytest.mark.asyncio
async def test_scheduler_prunes_done_tasks_before_applying_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DoneMarker:
        def done(self) -> bool:
            return True

    async def fake_runner(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(shadow.settings, "SKYVERN_SUBMISSION_SIGNAL_SHADOW", True)
    monkeypatch.setattr(shadow, "run_submission_signal_shadow", fake_runner)
    pending: set[Any] = {_DoneMarker() for _ in range(shadow._MAX_PENDING)}
    monkeypatch.setattr(shadow, "_PENDING_SUBMISSION_SHADOW_TASKS", pending)

    with capture_logs() as logs:
        scheduled = shadow.schedule_submission_signal_shadow(
            har_data=b"",
            browser_state=_browser_state(),
            last_step=_step("step-1", 1),
            task=_task(),
        )

    assert scheduled is not None
    await scheduled
    await asyncio.sleep(0)
    assert shadow._PENDING_SUBMISSION_SHADOW_TASKS == set()
    assert logs == []


@pytest.mark.asyncio
async def test_scheduler_returns_and_tracks_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_runner(**_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(shadow.settings, "SKYVERN_SUBMISSION_SIGNAL_SHADOW", True)
    monkeypatch.setattr(shadow, "run_submission_signal_shadow", fake_runner)

    scheduled = shadow.schedule_submission_signal_shadow(
        har_data=b"",
        browser_state=_browser_state(),
        last_step=_step("step-1", 1),
        task=_task(),
    )

    assert scheduled is not None
    await started.wait()
    assert scheduled in shadow._PENDING_SUBMISSION_SHADOW_TASKS
    release.set()
    await scheduled
    await asyncio.sleep(0)
    assert scheduled not in shadow._PENDING_SUBMISSION_SHADOW_TASKS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_visible_artifacts", "artifact_prefix"),
    [(True, "visible"), (False, "html")],
)
async def test_runner_uses_hydrated_actions_next_step_text_and_emits_one_aggregate_event(
    monkeypatch: pytest.MonkeyPatch,
    include_visible_artifacts: bool,
    artifact_prefix: str,
) -> None:
    action = _submit_action()
    pre_step = _step("step-1", 1, action=action)
    post_step = _step("step-2", 2)
    database, retrieve_artifact = _install_task_dependencies(
        monkeypatch,
        steps=[pre_step, post_step],
        actions=[action],
        artifact_payloads={
            "visible-step-1": b"Review the details before sending.",
            "visible-step-2": b"Thank you. Your request is complete.",
            "html-step-1": b"Review the details before sending.",
            "html-step-2": b"Thank you. Your request is complete.",
        },
        include_visible_artifacts=include_visible_artifacts,
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")
    to_thread = AsyncMock(side_effect=lambda function, *args, **kwargs: function(*args, **kwargs))
    monkeypatch.setattr(shadow.asyncio, "to_thread", to_thread)

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(),
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    events = [record for record in logs if record["event"] == "submission_signal_shadow"]
    assert len(events) == 1
    assert events[0]["status"] == "ok"
    assert events[0]["signal"] == "submitted_verified"
    assert events[0]["tier_a_hit"] is True
    assert events[0]["tier_b_hits"] >= 1
    assert "evidence" not in events[0]
    to_thread.assert_awaited_once()
    database.tasks.get_task_actions_hydrated.assert_awaited_once_with("task-1", organization_id="org-1")
    database.tasks.get_run.assert_awaited_once_with(run_id="task-1", organization_id="org-1")
    assert [call.args[0].artifact_id for call in retrieve_artifact.await_args_list] == [
        f"{artifact_prefix}-step-1",
        f"{artifact_prefix}-step-2",
    ]


@pytest.mark.asyncio
async def test_runner_skips_oversized_har_without_allowing_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    action = _submit_action()
    pre_step = _step("step-1", 1, action=action)
    post_step = _step("step-2", 2)
    _install_task_dependencies(
        monkeypatch,
        steps=[pre_step, post_step],
        actions=[action],
        artifact_payloads={
            "visible-step-1": b"Review the details before sending.",
            "visible-step-2": b"The form remains ready.",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")
    monkeypatch.setattr(shadow, "_MAX_HAR_BYTES", 10)

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=b"x" * 11,
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["signal"] == "not_evaluated"
    assert event["har_present"] is True
    assert event["har_parsed"] is False
    assert "har_too_large" in event["notes"]


@pytest.mark.asyncio
async def test_unmapped_candidate_step_cannot_produce_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    mapped_action = _submit_action()
    ghost_action = _submit_action(step_id="step-ghost")
    pre_step = _step("step-1", 1, action=mapped_action)
    post_step = _step("step-2", 2)
    _install_task_dependencies(
        monkeypatch,
        steps=[pre_step, post_step],
        actions=[mapped_action, ghost_action],
        artifact_payloads={
            "visible-step-1": b"Review the details before sending.",
            "visible-step-2": b"The form remains ready.",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(method="GET"),
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["har_present"] is True
    assert event["har_parsed"] is True
    assert event["signal"] == "not_evaluated"


@pytest.mark.asyncio
async def test_zip_page_artifacts_cannot_produce_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    action = _submit_action()
    pre_step = _step("step-1", 1, action=action)
    post_step = _step("step-2", 2)
    _, retrieve_artifact = _install_task_dependencies(
        monkeypatch,
        steps=[pre_step, post_step],
        actions=[action],
        artifact_payloads={
            "visible-step-1": b"PK\x03\x04pre-archive",
            "visible-step-2": b"PK\x03\x04post-archive",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(method="GET"),
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "not_evaluated"
    assert event["signal"] != "not_submitted"
    assert retrieve_artifact.await_count == 2


@pytest.mark.asyncio
async def test_unrelated_step_download_does_not_count_as_candidate_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    unrelated_step = _step("step-0", 0)
    assert unrelated_step.output is not None
    unrelated_step.output.action_results = [ActionResult(success=True, download_triggered=True)]
    action = _submit_action()
    pre_step = _step("step-1", 1, action=action)
    post_step = _step("step-2", 2)
    _install_task_dependencies(
        monkeypatch,
        steps=[unrelated_step, pre_step, post_step],
        actions=[action],
        artifact_payloads={
            "visible-step-1": b"Review the details before sending.",
            "visible-step-2": b"The form remains ready.",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(method="GET"),
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "not_submitted"
    assert event["tier_b_hits"] == 0


@pytest.mark.asyncio
async def test_latest_unpaired_candidate_cannot_reuse_older_page_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_action = _submit_action(step_id="step-1")
    latest_action = _submit_action(step_id="step-3")
    older_step = _step("step-1", 1, action=older_action)
    older_post_step = _step("step-2", 2)
    latest_step = _step("step-3", 3, action=latest_action)
    _, retrieve_artifact = _install_task_dependencies(
        monkeypatch,
        steps=[older_step, older_post_step, latest_step],
        actions=[older_action, latest_action],
        artifact_payloads={
            "visible-step-1": b"Review the details.",
            "visible-step-2": b"The form remains ready.",
            "visible-step-3": b"Review the latest details.",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(method="GET"),
            browser_state=_browser_state(),
            last_step=latest_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "not_evaluated"
    assert [call.args[0].step_id for call in retrieve_artifact.await_args_list] == ["step-3"]


@pytest.mark.asyncio
async def test_runner_does_not_cross_pair_older_network_with_latest_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_action = _submit_action(step_id="step-1")
    latest_action = _submit_action(step_id="step-3")
    older_step = _step("step-1", 1, action=older_action)
    older_post_step = _step("step-2", 2)
    latest_step = _step("step-3", 3, action=latest_action)
    latest_post_step = _step("step-4", 4, url="https://example.com/complete")
    _install_task_dependencies(
        monkeypatch,
        steps=[older_step, older_post_step, latest_step, latest_post_step],
        actions=[older_action, latest_action],
        artifact_payloads={
            "visible-step-3": b"Review the latest details.",
            "visible-step-4": b"The form remains ready.",
        },
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(offset_seconds=12),
            browser_state=_browser_state(),
            last_step=latest_post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "submitted_likely"
    assert event["tier_a_hit"] is False
    assert event["tier_b_hits"] == 1


@pytest.mark.asyncio
async def test_runner_can_select_an_older_verified_candidate_without_extra_page_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_action = _submit_action(step_id="step-1")
    latest_action = _submit_action(step_id="step-3")
    older_step = _step("step-1", 1, action=older_action)
    older_post_step = _step("step-2", 2, url="https://example.com/complete")
    latest_step = _step("step-3", 3, action=latest_action)
    _, retrieve_artifact = _install_task_dependencies(
        monkeypatch,
        steps=[older_step, older_post_step, latest_step],
        actions=[older_action, latest_action],
        artifact_payloads={"visible-step-3": b"Review the latest details."},
    )
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(offset_seconds=12),
            browser_state=_browser_state(),
            last_step=latest_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "submitted_verified"
    assert event["tier_a_hit"] is True
    assert event["tier_b_hits"] == 1
    assert [call.args[0].step_id for call in retrieve_artifact.await_args_list] == ["step-3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_type", "include_coordinate_click"),
    [(RunType.openai_cua, False), (RunType.task_v1, True)],
)
async def test_cua_run_and_coordinate_click_overrides_are_applied_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    run_type: RunType,
    include_coordinate_click: bool,
) -> None:
    action = _submit_action()
    actions = [action]
    if include_coordinate_click:
        actions.append(ClickAction(element_id="", step_id="step-1", x=10, y=20))
    pre_step = _step("step-1", 1, action=action)
    post_step = _step("step-2", 2)
    database, _ = _install_task_dependencies(
        monkeypatch,
        steps=[pre_step, post_step],
        actions=actions,
        artifact_payloads={
            "visible-step-1": b"Review the details.",
            "visible-step-2": b"Thank you. Your request is complete.",
        },
    )
    database.tasks.get_run.return_value = SimpleNamespace(task_run_type=run_type)
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(),
            browser_state=_browser_state(),
            last_step=post_step,
            task=_task(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "not_evaluated"
    database.tasks.get_run.assert_awaited_once_with(run_id="task-1", organization_id="org-1")


@pytest.mark.asyncio
async def test_workflow_uses_all_tasks_and_block_engine_for_cua_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    first_task = _task("task-a", workflow_run_id="workflow-run-1")
    candidate_task = _task("task-1", workflow_run_id="workflow-run-1")
    action = _submit_action()
    first_step = _step("step-a", 1, task_id="task-a")
    pre_step = _step("step-1", 2, action=action)
    post_step = _step("step-2", 3)

    async def get_steps(task_id: str, organization_id: str) -> list[Step]:
        assert organization_id == "org-1"
        return [first_step] if task_id == "task-a" else [pre_step, post_step]

    async def get_actions(task_id: str, organization_id: str | None = None) -> list[ClickAction]:
        assert organization_id == "org-1"
        return [] if task_id == "task-a" else [action]

    tasks_repo = SimpleNamespace(
        get_tasks_by_workflow_run_id=AsyncMock(return_value=[first_task, candidate_task]),
        get_task_steps=AsyncMock(side_effect=get_steps),
        get_task_actions_hydrated=AsyncMock(side_effect=get_actions),
        get_run=AsyncMock(return_value=SimpleNamespace(task_run_type=RunType.task_v1)),
    )
    artifacts_repo = SimpleNamespace(get_artifacts_for_task_step=AsyncMock(return_value=[]))
    observer_repo = SimpleNamespace(
        get_workflow_run_block_by_task_id=AsyncMock(return_value=SimpleNamespace(engine=RunEngine.openai_cua))
    )
    workflow_runs_repo = SimpleNamespace(
        get_workflow_run_output_parameters=AsyncMock(return_value=[SimpleNamespace(value="workflow-output")])
    )
    monkeypatch.setattr(
        shadow.app,
        "DATABASE",
        SimpleNamespace(
            tasks=tasks_repo,
            artifacts=artifacts_repo,
            observer=observer_repo,
            workflow_runs=workflow_runs_repo,
        ),
    )
    monkeypatch.setattr(shadow.app, "ARTIFACT_MANAGER", SimpleNamespace(retrieve_artifact=AsyncMock()))
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(),
            browser_state=_browser_state(),
            last_step=post_step,
            workflow_run=_workflow_run(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["status"] == "ok"
    assert event["signal"] == "not_evaluated"
    assert event["task_id"] == "task-1"
    assert event["workflow_run_id"] == "workflow-run-1"
    assert event["output_is_none"] is False
    tasks_repo.get_tasks_by_workflow_run_id.assert_awaited_once_with(workflow_run_id="workflow-run-1")
    assert tasks_repo.get_task_steps.await_count == 2
    assert tasks_repo.get_task_actions_hydrated.await_count == 2
    observer_repo.get_workflow_run_block_by_task_id.assert_awaited_once_with(
        task_id="task-1",
        organization_id="org-1",
    )
    workflow_runs_repo.get_workflow_run_output_parameters.assert_awaited_once_with(workflow_run_id="workflow-run-1")


@pytest.mark.asyncio
async def test_workflow_event_task_follows_the_winning_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    older_task = _task("task-a", workflow_run_id="workflow-run-1")
    latest_task = _task("task-b", workflow_run_id="workflow-run-1")
    older_action = _submit_action(task_id="task-a", step_id="step-a1")
    latest_action = _submit_action(task_id="task-b", step_id="step-b1")
    older_step = _step("step-a1", 1, task_id="task-a", action=older_action)
    older_post_step = _step("step-a2", 2, task_id="task-a", url="https://example.com/complete")
    latest_step = _step("step-b1", 3, task_id="task-b", action=latest_action)

    async def get_steps(task_id: str, organization_id: str) -> list[Step]:
        assert organization_id == "org-1"
        return [older_step, older_post_step] if task_id == "task-a" else [latest_step]

    async def get_actions(task_id: str, organization_id: str | None = None) -> list[ClickAction]:
        assert organization_id == "org-1"
        return [older_action] if task_id == "task-a" else [latest_action]

    tasks_repo = SimpleNamespace(
        get_tasks_by_workflow_run_id=AsyncMock(return_value=[older_task, latest_task]),
        get_task_steps=AsyncMock(side_effect=get_steps),
        get_task_actions_hydrated=AsyncMock(side_effect=get_actions),
        get_run=AsyncMock(return_value=SimpleNamespace(task_run_type=RunType.task_v1)),
    )
    monkeypatch.setattr(
        shadow.app,
        "DATABASE",
        SimpleNamespace(
            tasks=tasks_repo,
            artifacts=SimpleNamespace(get_artifacts_for_task_step=AsyncMock(return_value=[])),
            observer=SimpleNamespace(get_workflow_run_block_by_task_id=AsyncMock(return_value=None)),
            workflow_runs=SimpleNamespace(get_workflow_run_output_parameters=AsyncMock(return_value=[])),
        ),
    )
    monkeypatch.setattr(shadow.app, "ARTIFACT_MANAGER", SimpleNamespace(retrieve_artifact=AsyncMock()))
    monkeypatch.setattr(shadow.settings, "BROWSER_TYPE", "chromium-headless")

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=_har(offset_seconds=12),
            browser_state=_browser_state(),
            last_step=latest_step,
            workflow_run=_workflow_run(),
        )

    event = next(record for record in logs if record["event"] == "submission_signal_shadow")
    assert event["signal"] == "submitted_verified"
    assert event["task_id"] == "task-a"
    tasks_repo.get_run.assert_awaited_once_with(run_id="task-b", organization_id="org-1")


@pytest.mark.asyncio
async def test_runner_swallows_errors_without_logging_exception_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "raw-value?token=do-not-log"
    database = SimpleNamespace(
        tasks=SimpleNamespace(get_task_steps=AsyncMock(side_effect=ValueError(secret))),
        artifacts=SimpleNamespace(),
        observer=SimpleNamespace(),
    )
    monkeypatch.setattr(shadow.app, "DATABASE", database)

    with capture_logs() as logs:
        await shadow.run_submission_signal_shadow(
            har_data=b"",
            browser_state=_browser_state(),
            last_step=_step("step-1", 1),
            task=_task(),
        )

    events = [record for record in logs if record["event"] == "submission_signal_shadow"]
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error_stage"] == "load_tasks"
    assert events[0]["error_type"] == "ValueError"
    assert secret not in str(events[0])
    assert "error" not in events[0] or events[0]["error"] != secret
