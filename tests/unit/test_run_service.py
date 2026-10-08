import asyncio
import gc
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from skyvern.constants import SKYVERN_UI_USER_AGENT
from skyvern.exceptions import SkyvernHTTPException, WorkflowNotFoundForWorkflowRun, WorkflowRunNotFound
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.schemas.runs import RunResponse, RunStatus, RunType, WorkflowRunResponse
from skyvern.services import run_service

OWNER_ORG = "o_owner"
OTHER_ORG = "o_other"
RUN_ID = "wr_1"


def _org(organization_id: str) -> Organization:
    now = datetime.now(UTC)
    return Organization(
        organization_id=organization_id, organization_name=organization_id, created_at=now, modified_at=now
    )


class _GatedBuild:
    """Stands in for get_run_response: org-scoped like the DB reads, and parked until released."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, bool]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.status = RunStatus.running
        self.error: Exception | None = None

    async def __call__(
        self, run_id: str, organization_id: str | None = None, cap_output_values: bool = False
    ) -> WorkflowRunResponse | None:
        self.calls.append((organization_id, run_id, cap_output_values))
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        if organization_id != OWNER_ORG:
            return None
        now = datetime.now(UTC)
        return WorkflowRunResponse(
            run_id=run_id, run_type=RunType.workflow_run, status=self.status, created_at=now, modified_at=now
        )


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> _GatedBuild:
    fake = _GatedBuild()
    monkeypatch.setattr(run_service, "get_run_response", fake)
    monkeypatch.setattr(run_service, "_IN_FLIGHT", {})
    return fake


async def _get(run_id: str = RUN_ID, org: str = OWNER_ORG, user_agent: str | None = None) -> RunResponse:
    return await agent_protocol.get_run(run_id=run_id, current_org=_org(org), x_user_agent=user_agent)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_identical_overlapping_polls_share_one_build(build: _GatedBuild) -> None:
    polls = [asyncio.create_task(_get()) for _ in range(20)]
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    await _settle()
    build.release.set()
    results = await asyncio.gather(*polls)

    assert len(build.calls) == 1
    assert all(result == results[0] for result in results)
    assert run_service._IN_FLIGHT == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        {"run_id": "wr_2"},
        {"user_agent": SKYVERN_UI_USER_AGENT},
    ],
)
async def test_different_run_or_cap_builds_separately(build: _GatedBuild, variant: dict[str, str]) -> None:
    first = asyncio.create_task(_get())
    second = asyncio.create_task(_get(**variant))
    await _settle()
    build.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert len(build.calls) == 2
    assert first_result.run_id == RUN_ID
    assert second_result.run_id == variant.get("run_id", RUN_ID)


@pytest.mark.asyncio
async def test_other_org_overlapping_poll_gets_404_not_the_shared_payload(build: _GatedBuild) -> None:
    owner = asyncio.create_task(_get())
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    intruder = asyncio.create_task(_get(org=OTHER_ORG))
    await _settle()
    build.release.set()

    assert (await owner).run_id == RUN_ID
    with pytest.raises(HTTPException) as exc_info:
        await intruder
    assert exc_info.value.status_code == 404
    assert [call[0] for call in build.calls] == [OWNER_ORG, OTHER_ORG]


@pytest.mark.asyncio
async def test_build_error_reaches_every_waiter_and_next_poll_rebuilds(build: _GatedBuild) -> None:
    build.error = RuntimeError("db down")
    polls = [asyncio.create_task(_get()) for _ in range(3)]
    await _settle()
    build.release.set()
    results = await asyncio.gather(*polls, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert run_service._IN_FLIGHT == {}

    build.error = None
    assert (await _get()).status == RunStatus.running
    assert len(build.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_the_shared_build(build: _GatedBuild) -> None:
    leaver = asyncio.create_task(_get())
    stayer = asyncio.create_task(_get())
    await _settle()
    leaver.cancel()
    await _settle()
    build.release.set()

    assert (await stayer).run_id == RUN_ID
    assert leaver.cancelled()
    assert len(build.calls) == 1
    assert run_service._IN_FLIGHT == {}


def test_failed_build_whose_waiters_all_left_is_not_reported_as_unretrieved(build: _GatedBuild) -> None:
    reported: list[str] = []

    async def every_waiter_leaves_then_the_build_fails() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context["message"]))
        build.error = RuntimeError("db down")
        polls = [asyncio.create_task(_get()) for _ in range(3)]
        await _settle()
        for poll in polls:
            poll.cancel()
        await asyncio.gather(*polls, return_exceptions=True)
        build.release.set()
        await _settle()

    # asyncio reports an unretrieved exception when the task is finalized, so the loop must be gone first.
    asyncio.run(every_waiter_leaves_then_the_build_fails())
    gc.collect()

    assert reported == []
    assert run_service._IN_FLIGHT == {}


@pytest.mark.asyncio
async def test_completed_build_is_not_reused_by_the_next_poll(build: _GatedBuild) -> None:
    build.release.set()
    assert (await _get()).status == RunStatus.running
    build.status = RunStatus.completed

    assert (await _get()).status == RunStatus.completed
    assert len(build.calls) == 2


@pytest.mark.asyncio
async def test_poll_after_build_finishes_but_before_cleanup_rebuilds(build: _GatedBuild) -> None:
    async def poll_on_release() -> RunResponse:
        await build.release.wait()
        return await _get()

    first = asyncio.create_task(_get())
    await asyncio.wait_for(build.entered.wait(), timeout=5)
    late = asyncio.create_task(poll_on_release())
    await _settle()
    build.release.set()
    await asyncio.gather(first, late)

    assert len(build.calls) == 2
    assert run_service._IN_FLIGHT == {}


WORKFLOW_ID = "wpid_1"
WORKFLOW_RUN_ROUTES = ["with_workflow_id", "with_workflow", "legacy"]


def _workflow() -> Workflow:
    now = datetime.now(UTC)
    return Workflow(
        workflow_id="w_1",
        organization_id=OWNER_ORG,
        title="Workflow",
        workflow_permanent_id=WORKFLOW_ID,
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
        status=WorkflowStatus.published,
    )


@pytest.fixture
def workflow_build(build: _GatedBuild, monkeypatch: pytest.MonkeyPatch) -> _GatedBuild:
    """Parks the workflow-run routes' status build behind the same gate as get_run's."""

    async def build_status(
        workflow_permanent_id: str,
        workflow_run_id: str,
        organization_id: str | None = None,
        cap_output_values: bool = False,
        **_: object,
    ) -> WorkflowRunResponseBase:
        gated = await build(workflow_run_id, organization_id, cap_output_values)
        if gated is None:
            raise WorkflowRunNotFound(workflow_run_id)
        return WorkflowRunResponseBase(
            workflow_id=workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus(gated.status),
            created_at=gated.created_at,
            modified_at=gated.modified_at,
            parameters={},
        )

    async def build_status_by_run_id(workflow_run_id: str, **kwargs: Any) -> WorkflowRunResponseBase:
        return await build_status(WORKFLOW_ID, workflow_run_id, **kwargs)

    async def get_workflow(workflow_run_id: str, organization_id: str | None = None, **_: object) -> Workflow:
        if organization_id != OWNER_ORG:
            raise WorkflowNotFoundForWorkflowRun(workflow_run_id=workflow_run_id)
        return _workflow()

    service = agent_protocol.app.WORKFLOW_SERVICE
    monkeypatch.setattr(service, "build_workflow_run_status_response", build_status)
    monkeypatch.setattr(service, "build_workflow_run_status_response_by_workflow_id", build_status_by_run_id)
    monkeypatch.setattr(service, "get_workflow_by_workflow_run_id", get_workflow)
    monkeypatch.setattr(
        agent_protocol.app.DATABASE.browser_sessions,
        "get_persistent_browser_session_by_runnable_id",
        AsyncMock(return_value=None),
    )
    return build


async def _poll(
    route: str,
    run_id: str = RUN_ID,
    org: str = OWNER_ORG,
    user_agent: str | None = None,
    workflow_id: str = WORKFLOW_ID,
) -> dict[str, Any]:
    current_org = _org(org)
    if route == "with_workflow_id":
        return await agent_protocol.get_workflow_run_with_workflow_id(
            workflow_id=workflow_id, workflow_run_id=run_id, current_org=current_org, x_user_agent=user_agent
        )
    handler = (
        agent_protocol.get_workflow_and_run_from_workflow_run_id
        if route == "with_workflow"
        else agent_protocol.get_workflow_run
    )
    return (await handler(workflow_run_id=run_id, current_org=current_org, x_user_agent=user_agent)).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", WORKFLOW_RUN_ROUTES)
async def test_workflow_run_route_overlapping_polls_share_one_build(workflow_build: _GatedBuild, route: str) -> None:
    polls = [asyncio.create_task(_poll(route)) for _ in range(20)]
    await asyncio.wait_for(workflow_build.entered.wait(), timeout=5)
    await _settle()
    workflow_build.release.set()
    results = await asyncio.gather(*polls)

    assert len(workflow_build.calls) == 1
    assert results[0]["workflow_run_id"] == RUN_ID
    assert all(result == results[0] for result in results)
    assert run_service._IN_FLIGHT == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "variant"),
    [
        *[
            (route, variant)
            for route in WORKFLOW_RUN_ROUTES
            for variant in ({"run_id": "wr_2"}, {"user_agent": SKYVERN_UI_USER_AGENT})
        ],
        ("with_workflow_id", {"workflow_id": "wpid_2"}),
    ],
)
async def test_workflow_run_route_builds_separately_when_an_argument_differs(
    workflow_build: _GatedBuild, route: str, variant: dict[str, str]
) -> None:
    first = asyncio.create_task(_poll(route))
    second = asyncio.create_task(_poll(route, **variant))
    await _settle()
    workflow_build.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert len(workflow_build.calls) == 2
    assert (first_result["workflow_id"], first_result["workflow_run_id"]) == (WORKFLOW_ID, RUN_ID)
    assert second_result["workflow_id"] == variant.get("workflow_id", WORKFLOW_ID)
    assert second_result["workflow_run_id"] == variant.get("run_id", RUN_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", WORKFLOW_RUN_ROUTES)
async def test_workflow_run_route_other_org_overlapping_poll_gets_404_not_the_shared_payload(
    workflow_build: _GatedBuild, route: str
) -> None:
    owner = asyncio.create_task(_poll(route))
    await asyncio.wait_for(workflow_build.entered.wait(), timeout=5)
    intruder = asyncio.create_task(_poll(route, org=OTHER_ORG))
    await _settle()
    workflow_build.release.set()

    assert (await owner)["workflow_run_id"] == RUN_ID
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await intruder
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_different_routes_polling_one_run_never_share_a_build(workflow_build: _GatedBuild) -> None:
    polls = [asyncio.create_task(_poll(route)) for route in WORKFLOW_RUN_ROUTES]
    run_poll = asyncio.create_task(_get())
    await _settle()
    workflow_build.release.set()
    _, with_workflow, legacy = await asyncio.gather(*polls)

    assert len(workflow_build.calls) == 4
    assert with_workflow["workflow"]["workflow_permanent_id"] == WORKFLOW_ID
    assert "workflow" not in legacy
    assert isinstance(await run_poll, WorkflowRunResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", WORKFLOW_RUN_ROUTES)
async def test_workflow_run_route_sees_a_finished_run_on_the_next_poll(workflow_build: _GatedBuild, route: str) -> None:
    workflow_build.release.set()
    assert (await _poll(route))["status"] == WorkflowRunStatus.running
    workflow_build.status = RunStatus.completed

    assert (await _poll(route))["status"] == WorkflowRunStatus.completed
    assert len(workflow_build.calls) == 2
