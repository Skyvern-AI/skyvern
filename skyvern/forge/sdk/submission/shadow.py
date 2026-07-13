from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.submission.models import BrowserPath, CandidateWindow, SubmissionVerdict, TierAEvaluation
from skyvern.forge.sdk.submission.verifier import (
    CandidateEvaluation,
    build_candidate_windows,
    classify_browser_path,
    combine,
    detect_submit_candidates,
    evaluate_tier_a,
    evaluate_tier_b,
    find_candidate_step_pairs,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.services import service_utils
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionResult
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()

_SHADOW_EVENT = "submission_signal_shadow"
_ZIP_HEADER = b"PK\x03\x04"
_PAGE_ARTIFACT_TYPES = (
    ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT,
    ArtifactType.HTML_SCRAPE,
)

_PENDING_SUBMISSION_SHADOW_TASKS: set[asyncio.Task[None]] = set()
_MAX_PENDING = 20
_MAX_HAR_BYTES = 25_000_000


@dataclass(frozen=True)
class _LoadedContext:
    tasks: list[Task]
    steps: list[Step]
    actions: list[Action]
    organization_id: str
    workflow_run_id: str | None
    run_status: str
    output_is_none: bool


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _prune_pending() -> None:
    _PENDING_SUBMISSION_SHADOW_TASKS.difference_update(
        {task for task in _PENDING_SUBMISSION_SHADOW_TASKS if task.done()}
    )


def _track(task: asyncio.Task[None]) -> asyncio.Task[None]:
    _PENDING_SUBMISSION_SHADOW_TASKS.add(task)
    task.add_done_callback(_PENDING_SUBMISSION_SHADOW_TASKS.discard)
    return task


def _merge_last_step(steps: list[Step], last_step: Step) -> list[Step]:
    merged = [last_step if step.step_id == last_step.step_id else step for step in steps]
    if all(step.step_id != last_step.step_id for step in steps):
        merged.append(last_step)
    return sorted(merged, key=lambda step: (step.created_at, step.order, step.retry_index))


def _flatten_action_results(steps: list[Step]) -> list[ActionResult]:
    results: list[ActionResult] = []
    for step in steps:
        if step.output is None:
            continue
        if step.output.actions_and_results:
            results.extend(result for _, action_results in step.output.actions_and_results for result in action_results)
        elif step.output.action_results:
            results.extend(step.output.action_results)
    return results


async def _load_context(
    *,
    task: Task | None,
    workflow_run: WorkflowRun | None,
    last_step: Step,
) -> _LoadedContext:
    if task is None and workflow_run is None:
        raise ValueError("task or workflow_run is required")
    if task is not None and workflow_run is not None:
        raise ValueError("task and workflow_run are mutually exclusive")

    if task is not None:
        tasks = [task]
        organization_id = task.organization_id
        workflow_run_id = task.workflow_run_id
        run_status = str(task.status)
        output_is_none = task.extracted_information is None
    else:
        assert workflow_run is not None
        tasks = await app.DATABASE.tasks.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run.workflow_run_id)
        organization_id = workflow_run.organization_id
        workflow_run_id = workflow_run.workflow_run_id
        run_status = str(workflow_run.status)
        workflow_outputs = await app.DATABASE.workflow_runs.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run.workflow_run_id
        )
        output_is_none = not workflow_outputs

    steps: list[Step] = []
    actions: list[Action] = []
    for item in tasks:
        task_steps = await app.DATABASE.tasks.get_task_steps(item.task_id, item.organization_id)
        steps.extend(task_steps)
        actions.extend(
            await app.DATABASE.tasks.get_task_actions_hydrated(
                item.task_id,
                organization_id=item.organization_id,
            )
        )

    if any(item.task_id == last_step.task_id for item in tasks):
        steps = _merge_last_step(steps, last_step)
    else:
        steps.sort(key=lambda step: (step.created_at, step.order, step.retry_index))

    return _LoadedContext(
        tasks=tasks,
        steps=steps,
        actions=actions,
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        run_status=run_status,
        output_is_none=output_is_none,
    )


def _preferred_page_artifact(artifacts: list[Artifact]) -> Artifact | None:
    for artifact_type in _PAGE_ARTIFACT_TYPES:
        matching = [artifact for artifact in artifacts if artifact.artifact_type == artifact_type]
        if matching:
            return max(matching, key=lambda artifact: artifact.created_at)
    return None


async def _retrieve_page_text(step: Step) -> str | None:
    try:
        artifacts = await app.DATABASE.artifacts.get_artifacts_for_task_step(
            task_id=step.task_id,
            step_id=step.step_id,
            organization_id=step.organization_id,
        )
        artifact = _preferred_page_artifact(artifacts)
        if artifact is None:
            return None
        content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if not content or content.startswith(_ZIP_HEADER):
            return None
        return content.decode("utf-8")
    except Exception:  # noqa: BLE001 - page confirmation is optional shadow evidence
        return None


def _step_url(step: Step | None) -> str | None:
    if step is None or step.output is None or step.output.browser_metadata is None:
        return None
    return step.output.browser_metadata.website_url


def _evaluate_submission_signal(
    *,
    har_data: bytes,
    candidate_windows: list[CandidateWindow],
    candidate_step_pairs: list[tuple[Step, Step | None]],
    detected_candidate_step_ids: list[str],
    latest_candidate_step: Step | None,
    pre_page_text: str | None,
    post_page_text: str | None,
    submit_intent_detected: bool,
    browser_path: BrowserPath,
    cua_run: bool,
    coordinate_click: bool,
) -> SubmissionVerdict:
    har_too_large = len(har_data) > _MAX_HAR_BYTES
    tier_a = (
        TierAEvaluation(evidence=[], har_present=True, har_parsed=False, har_entry_count=0)
        if har_too_large
        else evaluate_tier_a(har_data, candidate_windows)
    )
    candidate_evaluations = [
        CandidateEvaluation(
            step_id=candidate_step.step_id,
            tier_a=[evidence for evidence in tier_a.evidence if evidence.correlated_step_id == candidate_step.step_id],
            tier_b=evaluate_tier_b(
                pre_url=_step_url(candidate_step),
                post_url=_step_url(candidate_post_step),
                action_results=_flatten_action_results([candidate_step]),
                pre_page_text=pre_page_text if candidate_step == latest_candidate_step else None,
                post_page_text=post_page_text if candidate_step == latest_candidate_step else None,
            ),
            is_latest=candidate_step == latest_candidate_step,
        )
        for candidate_step, candidate_post_step in candidate_step_pairs
    ]
    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=candidate_evaluations,
        detected_candidate_step_ids=detected_candidate_step_ids,
        submit_intent_detected=submit_intent_detected,
        browser_path=browser_path,
        cua_run=cua_run,
        coordinate_click=coordinate_click,
    )
    if har_too_large:
        verdict.notes.append("har_too_large")
    return verdict


def _task_for_candidate_step(tasks: list[Task], candidate_step: Step | None) -> Task:
    if candidate_step is not None:
        for task in tasks:
            if task.task_id == candidate_step.task_id:
                return task
    if not tasks:
        raise ValueError("no tasks available for submission shadow")
    return tasks[-1]


async def run_submission_signal_shadow(
    *,
    har_data: bytes,
    browser_state: BrowserState,
    last_step: Step,
    task: Task | None = None,
    workflow_run: WorkflowRun | None = None,
    browser_session_id: str | None = None,
) -> None:
    started_at = time.monotonic()
    stage = "load_tasks"
    if task is not None:
        organization_id = task.organization_id
        event_task_id = task.task_id
        event_workflow_run_id = task.workflow_run_id
    elif workflow_run is not None:
        organization_id = workflow_run.organization_id
        event_task_id = last_step.task_id
        event_workflow_run_id = workflow_run.workflow_run_id
    else:
        organization_id = None
        event_task_id = last_step.task_id
        event_workflow_run_id = None
    try:
        context = await _load_context(task=task, workflow_run=workflow_run, last_step=last_step)
        stage = "detect_candidates"
        detection = detect_submit_candidates(context.actions)
        candidate_step_pairs = find_candidate_step_pairs(detection.candidates, context.steps)
        pre_step, post_step = candidate_step_pairs[-1] if candidate_step_pairs else (None, None)
        context_task = _task_for_candidate_step(context.tasks, pre_step)

        stage = "load_run"
        cua_run = await service_utils.is_cua_task(task=context_task)
        stage = "classify_browser_path"
        artifacts = browser_state.browser_artifacts
        browser_path = classify_browser_path(
            browser_session_id=browser_session_id or (workflow_run.browser_session_id if workflow_run else None),
            task_browser_session_id=context_task.browser_session_id,
            remote_browser_session_id=artifacts.remote_browser_session_id,
            task_browser_address=context_task.browser_address
            or (workflow_run.browser_address if workflow_run is not None else None),
            needs_cdp_frame_publisher=artifacts.needs_cdp_frame_publisher,
            browser_type=settings.BROWSER_TYPE,
        )

        candidate_windows = build_candidate_windows(detection.candidates, context.steps)

        stage = "retrieve_page_text"
        pre_page_text = await _retrieve_page_text(pre_step) if pre_step is not None else None
        post_page_text = await _retrieve_page_text(post_step) if post_step is not None else None

        stage = "evaluate"
        verdict = await asyncio.to_thread(
            _evaluate_submission_signal,
            har_data=har_data,
            candidate_windows=candidate_windows,
            candidate_step_pairs=candidate_step_pairs,
            detected_candidate_step_ids=sorted({candidate.step_id for candidate in detection.candidates}),
            latest_candidate_step=pre_step,
            pre_page_text=pre_page_text,
            post_page_text=post_page_text,
            submit_intent_detected=detection.submit_intent_detected,
            browser_path=browser_path,
            cua_run=cua_run,
            coordinate_click=detection.coordinate_click,
        )
        winning_step = next(
            (
                candidate_step
                for candidate_step, _ in candidate_step_pairs
                if candidate_step.step_id == verdict.winning_step_id
            ),
            None,
        )
        event_task = _task_for_candidate_step(context.tasks, winning_step or pre_step)

        LOG.info(
            _SHADOW_EVENT,
            status="ok",
            signal=verdict.signal.value,
            tier_a_hit=bool(verdict.tier_a),
            tier_b_hits=len(verdict.tier_b),
            submit_intent_detected=verdict.submit_intent_detected,
            run_status=context.run_status,
            output_is_none=context.output_is_none,
            har_present=verdict.har_present,
            har_parsed=verdict.har_parsed,
            har_entry_count=verdict.har_entry_count,
            browser_path=verdict.browser_path.value,
            capped=verdict.capped,
            notes=verdict.notes,
            duration_ms=_elapsed_ms(started_at),
            task_id=event_task.task_id,
            workflow_run_id=context.workflow_run_id,
            organization_id=context.organization_id,
        )
    except Exception as exc:  # noqa: BLE001 - shadow observability must never affect cleanup
        LOG.warning(
            _SHADOW_EVENT,
            status="error",
            error_stage=stage,
            error_type=type(exc).__name__,
            duration_ms=_elapsed_ms(started_at),
            task_id=event_task_id,
            workflow_run_id=event_workflow_run_id,
            organization_id=organization_id,
        )


def schedule_submission_signal_shadow(
    *,
    har_data: bytes,
    browser_state: BrowserState,
    last_step: Step,
    task: Task | None = None,
    workflow_run: WorkflowRun | None = None,
    browser_session_id: str | None = None,
) -> asyncio.Task[None] | None:
    if not settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW:
        return None

    try:
        _prune_pending()
        if len(_PENDING_SUBMISSION_SHADOW_TASKS) >= _MAX_PENDING:
            LOG.warning("submission_shadow_task_cap_reached", pending=len(_PENDING_SUBMISSION_SHADOW_TASKS))
            LOG.info(_SHADOW_EVENT, status="skipped", reason="pending_cap")
            return None

        runner = run_submission_signal_shadow(
            har_data=har_data,
            browser_state=browser_state,
            last_step=last_step,
            task=task,
            workflow_run=workflow_run,
            browser_session_id=browser_session_id,
        )
        try:
            scheduled = asyncio.create_task(runner)
        except Exception:
            runner.close()
            raise
        try:
            return _track(scheduled)
        except Exception:
            _PENDING_SUBMISSION_SHADOW_TASKS.discard(scheduled)
            scheduled.cancel()
            raise
    except Exception as exc:  # noqa: BLE001 - scheduler must never affect artifact persistence
        try:
            LOG.debug("submission_shadow_schedule_failed", error_type=type(exc).__name__)
        except Exception:  # noqa: BLE001 - logging must not break the no-throw boundary
            pass
        return None
