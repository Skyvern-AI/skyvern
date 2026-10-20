import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from skyvern.config import settings
from skyvern.exceptions import (
    GroupAccountsRanSinceReview,
    SkyvernHTTPException,
    WorkflowChangedSinceReview,
    WorkflowPinnedByRunGroup,
)
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.permissions.permission_checkers import PermissionChecker
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.db.models import (
    WorkflowModel,
    WorkflowRunGroupItemModel,
    WorkflowRunGroupModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.repositories import workflow_run_groups as workflow_run_groups_repository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.runs import Run
from skyvern.forge.sdk.workflow.models.block import (
    ForLoopBlock,
    HttpRequestBlock,
    WorkflowTriggerBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import (
    COPILOT_TEST_WORKFLOW_CREATOR,
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflow_run_groups import (
    WorkflowRunGroupCreateRequest,
    WorkflowRunGroupItemOutcome,
    WorkflowRunGroupItemState,
    WorkflowRunGroupStatus,
)
from skyvern.services import workflow_run_group_service as group_service
from tests.unit.conftest import RUN_GROUP_ORG as ORG
from tests.unit.conftest import RUN_GROUP_OTHER_ORG as OTHER_ORG
from tests.unit.conftest import RUN_GROUP_WPID as WPID
from tests.unit.conftest import (
    GroupEnv,
    count_rows,
    make_block_output_parameter,
    run_group_definition,
    run_group_task_block,
)


def _looped_trigger(browser_session_id: str | None = None, wait_for_completion: bool = True) -> ForLoopBlock:
    trigger = WorkflowTriggerBlock(
        label="trigger",
        workflow_permanent_id="wpid_child",
        browser_session_id=browser_session_id,
        wait_for_completion=wait_for_completion,
        output_parameter=make_block_output_parameter("trigger"),
    )
    return ForLoopBlock(label="loop", loop_blocks=[trigger], output_parameter=make_block_output_parameter("loop"))


def _http_block() -> HttpRequestBlock:
    return HttpRequestBlock(
        label="post", url="https://example.com", method="POST", output_parameter=make_block_output_parameter("post")
    )


@pytest.fixture
def env(run_group_env: GroupEnv) -> GroupEnv:
    return run_group_env


def _request(
    key: str = "sub-1", count: int = 3, items: Sequence[Mapping[str, object]] | None = None
) -> WorkflowRunGroupCreateRequest:
    if items is None:
        items = [{"key": f"acct-{i}", "parameters": {"login": "cred_1" if i % 2 else "cred_2"}} for i in range(count)]
    return WorkflowRunGroupCreateRequest.model_validate({"workflow_id": WPID, "submission_key": key, "items": items})


async def _set_child_status(env: GroupEnv, workflow_run_id: str, status: WorkflowRunStatus) -> None:
    await env.database.workflow_runs.update_workflow_run(workflow_run_id, status=status)


async def _start_nested_run(env: GroupEnv, parent_workflow_run_id: str) -> str:
    nested = await env.database.workflow_runs.create_workflow_run(
        workflow_permanent_id=WPID,
        workflow_id="wf_1",
        organization_id=ORG,
        parent_workflow_run_id=parent_workflow_run_id,
    )
    await _set_child_status(env, nested.workflow_run_id, WorkflowRunStatus.running)
    return nested.workflow_run_id


async def _status(env: GroupEnv, workflow_run_id: str) -> WorkflowRunStatus | None:
    run = await env.database.workflow_runs.get_workflow_run(workflow_run_id)
    return run.status if run else None


async def _age_claims(env: GroupEnv) -> None:
    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunGroupItemModel).values(claimed_at=naive_utc_now() - timedelta(minutes=30))
        )
        await session.commit()


async def _submit(env: GroupEnv, request: WorkflowRunGroupCreateRequest | None = None) -> str:
    response = await group_service.submit_workflow_run_group(env.organization, request or _request())
    return response.workflow_run_group_id


@pytest.mark.asyncio
async def test_submit_preallocates_ids_without_child_rows_then_dispatches_one(env: GroupEnv) -> None:
    items = [{"key": f"acct-{i}", "parameters": {"login": "cred_1"}} for i in range(25)]
    response = await group_service.submit_workflow_run_group(env.organization, _request(items=items))

    run_ids = [item.workflow_run_id for item in response.items]
    assert len(set(run_ids)) == 25
    assert response.workflow_id == "wf_1"
    assert await count_rows(env, WorkflowRunModel) == 0

    await group_service.advance_workflow_run_group(response.workflow_run_group_id)

    assert env.executor.executed == [run_ids[0]]
    child = await env.database.workflow_runs.get_workflow_run(run_ids[0])
    assert child is not None and child.workflow_id == "wf_1" and child.start_fresh_browser is True
    assert await count_rows(env, WorkflowRunModel) == 1
    group = await group_service.get_workflow_run_group(response.workflow_run_group_id, ORG)
    assert [item.workflow_run_id for item in group.items] == run_ids


async def _run_ids(env: GroupEnv, group_id: str) -> list[str]:
    return [item.workflow_run_id for item in await env.database.workflow_run_groups.get_items(group_id)]


async def _states(env: GroupEnv, group_id: str) -> list[WorkflowRunGroupItemState]:
    return [item.state for item in await env.database.workflow_run_groups.get_items(group_id)]


@pytest.mark.asyncio
async def test_next_child_waits_until_the_running_one_is_final(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)

    await asyncio.gather(*(group_service.advance_workflow_run_group(group_id) for _ in range(3)))
    await group_service.recover_workflow_run_groups()

    assert env.executor.executed == [run_ids[0]]
    assert (await _states(env, group_id))[1:] == [WorkflowRunGroupItemState.pending] * 2

    await _set_child_status(env, run_ids[0], WorkflowRunStatus.completed)
    await group_service.advance_workflow_run_group(group_id)
    assert env.executor.executed == run_ids[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize("outside_status", [WorkflowRunStatus.canceled, WorkflowRunStatus.timed_out])
async def test_an_outside_cancel_or_timeout_holds_the_next_child_until_the_worker_can_stop(
    env: GroupEnv, outside_status: WorkflowRunStatus
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    await env.database.workflow_runs.update_workflow_run_if_not_final(run_ids[0], outside_status)

    await group_service.advance_workflow_run_group(group_id)
    assert env.executor.executed == run_ids[:1]

    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.workflow_run_id == run_ids[0])
            .values(finished_at=naive_utc_now() - group_service.CHILD_STOP_SETTLE - timedelta(seconds=1))
        )
        await session.commit()
    await group_service.advance_workflow_run_group(group_id)
    assert env.executor.executed == run_ids[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_credential", ["cred_foreign", "cred_missing"])
async def test_one_bad_credential_rejects_the_whole_group(env: GroupEnv, bad_credential: str) -> None:
    items = [
        {"key": "a", "parameters": {"login": "cred_1"}},
        {"key": "b", "parameters": {"login": bad_credential}},
        {"key": "c", "parameters": {"login": "cred_2"}},
    ]
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await group_service.submit_workflow_run_group(env.organization, _request(items=items))

    assert 400 <= exc_info.value.status_code < 500
    assert await count_rows(env, WorkflowRunGroupModel) == 0
    assert await count_rows(env, WorkflowRunGroupItemModel) == 0
    assert await count_rows(env, WorkflowRunModel) == 0
    assert env.spawned == [] and env.executor.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["3", "abc"])
async def test_a_mistyped_value_in_any_item_rejects_the_whole_group(env: GroupEnv, count: str) -> None:
    await env.database.workflow_params.create_workflow_parameter(
        workflow_id="wf_1", workflow_parameter_type=WorkflowParameterType.INTEGER, key="count", default_value=None
    )
    items = [
        {"key": "a", "parameters": {"login": "cred_1", "count": "1"}},
        {"key": "b", "parameters": {"login": "cred_2", "count": count}},
    ]

    if count.isdigit():
        await group_service.submit_workflow_run_group(env.organization, _request(items=items))
        assert await count_rows(env, WorkflowRunGroupItemModel) == 2
        return
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await group_service.submit_workflow_run_group(env.organization, _request(items=items))
    assert exc_info.value.status_code == 400
    assert await count_rows(env, WorkflowRunGroupModel) == 0
    assert await count_rows(env, WorkflowRunGroupItemModel) == 0


@pytest.mark.asyncio
async def test_identical_retry_after_workflow_saves_returns_the_same_group(env: GroupEnv) -> None:
    async with env.database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_copilot_test",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                title="Workflow",
                version=2,
                created_by=COPILOT_TEST_WORKFLOW_CREATOR,
                workflow_definition=run_group_definition(run_group_task_block()),
            )
        )
        await session.commit()
    first = await group_service.submit_workflow_run_group(env.organization, _request())
    assert first.workflow_id == "wf_1"

    async with env.database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_3",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                title="Workflow",
                version=3,
                workflow_definition=run_group_definition(),
            )
        )
        await session.commit()
    retry = await group_service.submit_workflow_run_group(env.organization, _request())

    assert retry.workflow_run_group_id == first.workflow_run_group_id
    assert retry.workflow_id == "wf_1"
    assert [item.workflow_run_id for item in retry.items] == [item.workflow_run_id for item in first.items]
    assert await count_rows(env, WorkflowRunGroupModel) == 1


@pytest.mark.asyncio
async def test_identical_retry_with_submission_disabled_returns_the_same_group(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await group_service.submit_workflow_run_group(env.organization, _request())
    monkeypatch.setattr(settings, "WORKFLOW_RUN_GROUPS_SUBMIT_ENABLED", False)

    retry = await group_service.submit_workflow_run_group(env.organization, _request())
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await group_service.submit_workflow_run_group(env.organization, _request(key="sub-2"))

    assert retry.workflow_run_group_id == first.workflow_run_group_id
    assert exc_info.value.status_code == 503
    assert await count_rows(env, WorkflowRunGroupModel) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        _request(items=[{"key": "acct-0", "parameters": {"login": "cred_2"}}]),
        _request().model_copy(update={"version": 1}),
    ],
    ids=["items", "version"],
)
async def test_conflicting_input_for_a_submission_key_is_rejected(
    env: GroupEnv, changed: WorkflowRunGroupCreateRequest
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)

    with pytest.raises(SkyvernHTTPException) as exc_info:
        await group_service.submit_workflow_run_group(env.organization, changed)

    assert exc_info.value.status_code == 409
    assert await count_rows(env, WorkflowRunGroupModel) == 1
    assert await _run_ids(env, group_id) == run_ids
    assert len(env.spawned) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_submissions_share_one_group(env: GroupEnv) -> None:
    responses = await asyncio.gather(
        *(group_service.submit_workflow_run_group(env.organization, _request()) for _ in range(3))
    )

    assert len({response.workflow_run_group_id for response in responses}) == 1
    assert len({tuple(item.workflow_run_id for item in response.items) for response in responses}) == 1
    assert await count_rows(env, WorkflowRunGroupModel) == 1
    assert await count_rows(env, WorkflowRunGroupItemModel) == 3


@pytest.mark.asyncio
async def test_concurrent_recoveries_of_a_stale_claim_execute_once(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    assert await env.database.workflow_run_groups.claim_next_item(group_id, "dead-owner") is not None
    await _age_claims(env)

    await asyncio.gather(group_service.recover_workflow_run_groups(), group_service.recover_workflow_run_groups())

    assert env.executor.executed == [run_ids[0]]
    assert await count_rows(env, WorkflowRunModel) == 1


@pytest.mark.asyncio
async def test_slow_owner_cannot_execute_after_recovery_reclaims_its_item(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    group = await env.database.workflow_run_groups.get_group(group_id)
    slow_item = await env.database.workflow_run_groups.claim_next_item(group_id, "slow-owner")
    assert group is not None and slow_item is not None
    await _age_claims(env)
    reclaimed = await env.database.workflow_run_groups.reclaim_stale_item(
        group_id, 0, dispatch_token="new-owner", stale_before=naive_utc_now() - group_service.DISPATCH_GRACE
    )
    assert reclaimed is not None

    assert await group_service._dispatch_item(group, slow_item, "slow-owner") is True
    assert env.executor.executed == []

    assert await group_service._dispatch_item(group, reclaimed, "new-owner") is True
    assert env.executor.executed == [run_ids[0]]
    assert await count_rows(env, WorkflowRunModel) == 1


@pytest.mark.asyncio
async def test_stale_owner_interrupted_prep_leaves_the_new_owners_child_alone(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    group = await env.database.workflow_run_groups.get_group(group_id)
    stale_item = await env.database.workflow_run_groups.claim_next_item(group_id, "stale-owner")
    assert group is not None and stale_item is not None
    await _age_claims(env)
    reclaimed = await env.database.workflow_run_groups.reclaim_stale_item(
        group_id, 0, dispatch_token="new-owner", stale_before=naive_utc_now() - group_service.DISPATCH_GRACE
    )
    assert reclaimed is not None
    create_task_run = env.database.tasks.create_task_run
    status_after_stale_owner: list[WorkflowRunStatus | None] = []

    async def stale_owner_runs_mid_preparation(**kwargs: Any) -> Run:
        if not status_after_stale_owner:
            assert await group_service._dispatch_item(group, stale_item, "stale-owner") is False
            status_after_stale_owner.append(await _status(env, run_ids[0]))
        return await create_task_run(**kwargs)

    monkeypatch.setattr(env.database.tasks, "create_task_run", stale_owner_runs_mid_preparation)

    assert await group_service._dispatch_item(group, reclaimed, "new-owner") is True

    assert status_after_stale_owner == [WorkflowRunStatus.created]
    assert env.executor.executed == [run_ids[0]]
    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.dispatched
    assert await count_rows(env, WorkflowRunModel) == 1


@pytest.mark.asyncio
async def test_crash_between_child_row_and_task_run_fails_that_item_and_runs_the_next(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await _leave_item_zero_unstarted(env, group_id, run_ids[0], "dispatching")

    await group_service.recover_workflow_run_groups()

    assert env.executor.executed == [run_ids[1]]
    child = await env.database.workflow_runs.get_workflow_run(run_ids[0])
    assert child is not None and child.status == WorkflowRunStatus.failed
    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert group.items[0].state == WorkflowRunGroupItemState.failed_to_start
    assert group.items[0].outcome == WorkflowRunGroupItemOutcome.failed
    assert await count_rows(env, WorkflowRunModel) == 2


class RefusingPermissionChecker(PermissionChecker):
    async def check(self, organization: Organization, browser_session_id: str | None = None) -> None:
        raise HTTPException(status_code=402, detail="Marketplace subscription is not active")


@pytest.mark.asyncio
async def test_org_refused_after_submit_starts_no_remaining_children(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_id = await _submit(env)
    monkeypatch.setattr(group_service.PermissionCheckerFactory, "get_instance", RefusingPermissionChecker)

    await group_service.advance_workflow_run_group(group_id)

    assert env.executor.executed == []
    assert await _states(env, group_id) == [WorkflowRunGroupItemState.failed_to_start] * 3
    assert await count_rows(env, WorkflowRunModel) == 0


@pytest.mark.asyncio
async def test_failing_an_item_whose_child_was_just_canceled_holds_the_next_item_until_it_settles(
    env: GroupEnv,
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    claimed = await env.database.workflow_run_groups.claim_next_item(group_id, "owner")
    group = await env.database.workflow_run_groups.get_group(group_id)
    assert claimed is not None and group is not None
    async with env.database.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=run_ids[0],
                workflow_id="wf_1",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                status=WorkflowRunStatus.canceled.value,
                finished_at=naive_utc_now(),
                start_fresh_browser=True,
            )
        )
        await session.commit()

    assert await group_service._dispatch_item(group, claimed, "owner") is False
    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.dispatched
    await group_service.advance_workflow_run_group(group_id)
    assert env.executor.executed == []

    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.workflow_run_id == run_ids[0])
            .values(finished_at=naive_utc_now() - group_service.CHILD_STOP_SETTLE - timedelta(seconds=1))
        )
        await session.commit()
    await group_service.advance_workflow_run_group(group_id)

    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.done
    assert env.executor.executed == run_ids[1:2]


@pytest.mark.asyncio
async def test_an_item_held_before_its_child_existed_is_released_by_the_sweep(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_id = await _submit(env)
    monkeypatch.setattr(group_service.PermissionCheckerFactory, "get_instance", RefusingPermissionChecker)
    finish_item = env.database.workflow_run_groups.finish_item
    crashed: list[bool] = []

    async def crash_once_before_the_release(*args: Any, **kwargs: Any) -> bool:
        if kwargs["state"] == WorkflowRunGroupItemState.failed_to_start and not crashed:
            crashed.append(True)
            raise RuntimeError("process died")
        return await finish_item(*args, **kwargs)

    monkeypatch.setattr(env.database.workflow_run_groups, "finish_item", crash_once_before_the_release)
    with pytest.raises(RuntimeError):
        await group_service.advance_workflow_run_group(group_id)
    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.dispatched

    await _age_claims(env)
    await group_service.recover_workflow_run_groups()

    assert await _states(env, group_id) == [WorkflowRunGroupItemState.failed_to_start] * 3
    group = await env.database.workflow_run_groups.get_group(group_id)
    assert group is not None and group.status == WorkflowRunGroupStatus.finished


@pytest.mark.asyncio
async def test_child_cancel_between_flip_and_queued_write(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)

    async def cancel_group(_: str) -> None:
        await group_service.cancel_workflow_run_group(group_id, ORG)

    env.executor.before_queue = cancel_group
    await group_service.advance_workflow_run_group(group_id)

    assert env.executor.executed == [run_ids[0]]
    assert env.executor.submitted == []
    assert await _status(env, run_ids[0]) == WorkflowRunStatus.canceled
    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert group.status == WorkflowRunGroupStatus.finished
    assert [item.outcome for item in group.items] == [WorkflowRunGroupItemOutcome.canceled] * 3
    assert await count_rows(env, WorkflowRunModel) == 1


@pytest.mark.asyncio
async def test_cancel_between_claim_and_dispatch_never_executes(env: GroupEnv) -> None:
    group_id = await _submit(env)
    group = await env.database.workflow_run_groups.get_group(group_id)
    claimed = await env.database.workflow_run_groups.claim_next_item(group_id, "owner")
    assert group is not None and claimed is not None

    await group_service.cancel_workflow_run_group(group_id, ORG)
    still_in_flight = await group_service._dispatch_item(group, claimed, "owner")

    assert still_in_flight is False
    assert env.executor.executed == []
    child = await env.database.workflow_runs.get_workflow_run(claimed.workflow_run_id)
    assert child is not None and child.status == WorkflowRunStatus.canceled
    assert await _states(env, group_id) == [WorkflowRunGroupItemState.canceled] * 3


@pytest.mark.asyncio
async def test_in_place_edit_of_a_pinned_version_is_refused_until_the_group_finishes(env: GroupEnv) -> None:
    group_id = await _submit(env)

    for edit in (
        {"title": "Edited"},
        {"workflow_definition": WorkflowDefinition(parameters=[], blocks=[run_group_task_block()])},
    ):
        with pytest.raises(WorkflowPinnedByRunGroup) as refused:
            await app.WORKFLOW_SERVICE.update_workflow_definition(workflow_id="wf_1", organization_id=ORG, **edit)
        assert refused.value.status_code == 409
    saved = await env.database.workflows.get_workflow(workflow_id="wf_1", organization_id=ORG)
    assert saved is not None and saved.title == "Workflow"

    await group_service.cancel_workflow_run_group(group_id, ORG)
    await group_service.recover_workflow_run_groups()
    assert (await group_service.get_workflow_run_group(group_id, ORG)).status == WorkflowRunGroupStatus.finished
    edited = await app.WORKFLOW_SERVICE.update_workflow_definition(
        workflow_id="wf_1", organization_id=ORG, title="Edited"
    )
    assert edited.title == "Edited"


@pytest.mark.asyncio
async def test_submit_bound_to_a_reviewed_version_refuses_an_edit_that_landed_after_review(env: GroupEnv) -> None:
    reviewed = await env.database.workflows.get_workflow(workflow_id="wf_1", organization_id=ORG)
    assert reviewed is not None
    await app.WORKFLOW_SERVICE.update_workflow_definition(workflow_id="wf_1", organization_id=ORG, title="Edited")

    with pytest.raises(WorkflowChangedSinceReview) as refused:
        await group_service.submit_workflow_run_group(
            env.organization, _request(), expected_workflow_modified_at=reviewed.modified_at
        )

    assert refused.value.status_code == 409
    assert await count_rows(env, WorkflowRunGroupModel) == 0
    assert env.spawned == []
    current = await env.database.workflows.get_workflow(workflow_id="wf_1", organization_id=ORG)
    assert current is not None
    response = await group_service.submit_workflow_run_group(
        env.organization, _request(), expected_workflow_modified_at=current.modified_at
    )
    assert response.workflow_id == "wf_1"


@pytest.mark.asyncio
async def test_cancel_while_a_child_runs_gives_every_item_an_outcome(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    await env.database.workflow_runs.update_workflow_run(run_ids[0], status=WorkflowRunStatus.running)

    await group_service.cancel_workflow_run_group(group_id, ORG)
    await group_service.recover_workflow_run_groups()

    assert env.executor.executed == [run_ids[0]]
    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert group.status == WorkflowRunGroupStatus.finished
    assert [item.run_status for item in group.items] == [WorkflowRunStatus.canceled, None, None]
    assert [item.outcome for item in group.items] == [
        WorkflowRunGroupItemOutcome.unknown,
        WorkflowRunGroupItemOutcome.canceled,
        WorkflowRunGroupItemOutcome.canceled,
    ]


@pytest.mark.asyncio
async def test_cancel_cascades_to_a_running_childs_nested_runs_and_keeps_final_statuses(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    finished_nested = await _start_nested_run(env, run_ids[0])
    await _set_child_status(env, finished_nested, WorkflowRunStatus.completed)
    await _set_child_status(env, run_ids[0], WorkflowRunStatus.completed)
    await group_service.advance_workflow_run_group(group_id)
    await _set_child_status(env, run_ids[1], WorkflowRunStatus.running)
    running_nested = await _start_nested_run(env, run_ids[1])
    completed_nested = await _start_nested_run(env, run_ids[1])
    await _set_child_status(env, completed_nested, WorkflowRunStatus.completed)

    await group_service.cancel_workflow_run_group(group_id, ORG)

    assert await _status(env, run_ids[0]) == WorkflowRunStatus.completed
    assert await _status(env, finished_nested) == WorkflowRunStatus.completed
    assert await _status(env, run_ids[1]) == WorkflowRunStatus.canceled
    assert await _status(env, running_nested) == WorkflowRunStatus.canceled
    assert await _status(env, completed_nested) == WorkflowRunStatus.completed


@pytest.mark.asyncio
async def test_cancel_runs_terminal_side_effects_for_non_final_children_only(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    webhooks: list[str] = []

    async def record_webhook(workflow_run: WorkflowRun, **_: object) -> None:
        webhooks.append(workflow_run.workflow_run_id)

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_webhook", record_webhook)
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    await _set_child_status(env, run_ids[0], WorkflowRunStatus.completed)
    await group_service.advance_workflow_run_group(group_id)
    await _set_child_status(env, run_ids[1], WorkflowRunStatus.running)

    await group_service.cancel_workflow_run_group(group_id, ORG)
    await group_service.recover_workflow_run_groups()
    await group_service._cancel_child(run_ids[0], ORG)

    assert webhooks == [run_ids[1]]
    assert await _status(env, run_ids[0]) == WorkflowRunStatus.completed
    assert await _status(env, run_ids[1]) == WorkflowRunStatus.canceled


@pytest.mark.asyncio
async def test_failed_child_keeps_earlier_results_and_later_items_run(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    for run_id, status in zip(
        run_ids, (WorkflowRunStatus.completed, WorkflowRunStatus.failed, WorkflowRunStatus.completed), strict=True
    ):
        await group_service.advance_workflow_run_group(group_id)
        await _set_child_status(env, run_id, status)
    await group_service.advance_workflow_run_group(group_id)

    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert env.executor.executed == run_ids
    assert group.status == WorkflowRunGroupStatus.finished
    assert [item.outcome for item in group.items] == [
        WorkflowRunGroupItemOutcome.completed,
        WorkflowRunGroupItemOutcome.failed,
        WorkflowRunGroupItemOutcome.completed,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["http_request_block", "recorded_step", "script_run"])
async def test_failure_after_possible_side_effects_is_unknown_and_not_replayed(env: GroupEnv, evidence: str) -> None:
    if evidence == "http_request_block":
        async with env.database.Session() as session:
            await session.execute(
                update(WorkflowModel).values(
                    workflow_definition=run_group_definition(run_group_task_block(), _http_block())
                )
            )
            await session.commit()
    group_id = await _submit(env, _request(count=1))
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    if evidence == "recorded_step":
        task = await env.database.tasks.create_task(
            url="https://example.com",
            title="login",
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=ORG,
            workflow_run_id=run_ids[0],
        )
        await env.database.tasks.create_step(task.task_id, order=0, retry_index=0, organization_id=ORG)
    if evidence == "script_run":
        await env.database.workflow_runs.update_workflow_run(run_ids[0], script_id="s_1")
    await _set_child_status(env, run_ids[0], WorkflowRunStatus.failed)

    await group_service.advance_workflow_run_group(group_id)
    await group_service.recover_workflow_run_groups()

    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert group.items[0].outcome == WorkflowRunGroupItemOutcome.unknown
    assert env.executor.executed == run_ids
    assert await count_rows(env, WorkflowRunModel) == 1


@pytest.mark.asyncio
async def test_terminal_child_advances_the_group_through_the_run_terminal_hook(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkflowService()
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(service, "_resolve_managed_browser_profile_for_run_request", AsyncMock(return_value=None))
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "schedule_workflow_run_group_advance", AgentFunction().schedule_workflow_run_group_advance
    )
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await group_service.advance_workflow_run_group(group_id)
    while env.spawned:
        env.spawned.pop().close()

    await service.mark_workflow_run_as_failed_if_not_final(workflow_run_id=run_ids[0], failure_reason="boom")
    while env.spawned:
        await env.spawned.pop()

    assert env.executor.executed == run_ids[:2]


def test_browser_session_inputs_are_rejected() -> None:
    base = {"workflow_id": WPID, "submission_key": "k", "items": [{"key": "a", "parameters": {}}]}
    for field_name in ("browser_session_id", "browser_address", "browser_profile_id"):
        with pytest.raises(ValidationError):
            WorkflowRunGroupCreateRequest.model_validate({**base, field_name: "x"})
        with pytest.raises(ValidationError):
            WorkflowRunGroupCreateRequest.model_validate({**base, "items": [{"key": "a", field_name: "x"}]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_values",
    [
        {
            "workflow_definition": {
                **run_group_definition(run_group_task_block()),
                "retry_policy": {"retry_on": [{"status": "failed"}]},
            }
        },
        {"persist_browser_session": True},
        {"browser_profile_id": "bp_1"},
        {"sequential_key": "{{ login }}"},
        {"workflow_definition": run_group_definition()},
        {"browser_profile_key": "profile-{{ login }}"},
        {
            "workflow_definition": run_group_definition(
                run_group_task_block(), _looped_trigger(browser_session_id="{{ session_id }}")
            )
        },
        {
            "workflow_definition": run_group_definition(
                run_group_task_block(), _looped_trigger(wait_for_completion=False)
            )
        },
        {"workflow_definition": {**run_group_definition(run_group_task_block()), "finally_block_label": "login"}},
    ],
)
async def test_versions_a_group_cannot_run_are_rejected(env: GroupEnv, workflow_values: dict[str, object]) -> None:
    async with env.database.Session() as session:
        await session.execute(update(WorkflowModel).values(**workflow_values))
        await session.commit()

    with pytest.raises(SkyvernHTTPException) as exc_info:
        await _submit(env)

    assert exc_info.value.status_code == 400
    assert await count_rows(env, WorkflowRunGroupModel) == 0
    assert await count_rows(env, WorkflowRunGroupItemModel) == 0


@pytest.mark.asyncio
async def test_sweep_with_a_stale_read_cannot_reclaim_an_item_another_sweep_just_reclaimed(env: GroupEnv) -> None:
    group_id = await _submit(env)
    assert await env.database.workflow_run_groups.claim_next_item(group_id, "dead-owner") is not None
    await _age_claims(env)
    group = await env.database.workflow_run_groups.get_group(group_id)
    stale_snapshot = await env.database.workflow_run_groups.get_items(group_id)
    assert group is not None
    assert await env.database.workflow_run_groups.reclaim_stale_item(
        group_id, 0, dispatch_token="sweep-a", stale_before=naive_utc_now() - group_service.DISPATCH_GRACE
    )

    await group_service._reconcile_in_flight(group, stale_snapshot)

    assert env.executor.executed == []
    assert (await env.database.workflow_run_groups.get_items(group_id))[0].dispatch_token == "sweep-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_took_the_child", [False, True])
async def test_sweep_fails_a_dispatched_child_no_executor_took_and_leaves_a_queued_one(
    env: GroupEnv, executor_took_the_child: bool
) -> None:
    async def executor_crashes(_: str) -> None:
        raise RuntimeError("executor unavailable")

    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    env.executor.before_queue = None if executor_took_the_child else executor_crashes
    await group_service.advance_workflow_run_group(group_id)
    env.executor.before_queue = None
    await _age_claims(env)

    await group_service.recover_workflow_run_groups()

    if executor_took_the_child:
        assert await _status(env, run_ids[0]) == WorkflowRunStatus.queued
        assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.dispatched
        assert env.executor.executed == run_ids[:1]
    else:
        assert await _status(env, run_ids[0]) == WorkflowRunStatus.failed
        assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.failed_to_start
        assert env.executor.executed == run_ids[:2]


async def _leave_item_zero_unstarted(env: GroupEnv, group_id: str, run_id: str, item_state: str) -> None:
    if item_state == "dispatched":

        async def executor_crashes(_: str) -> None:
            raise RuntimeError("executor unavailable")

        env.executor.before_queue = executor_crashes
        await group_service.advance_workflow_run_group(group_id)
        env.executor.before_queue = None
    else:
        assert await env.database.workflow_run_groups.claim_next_item(group_id, "crashed-owner") is not None
        async with env.database.Session() as session:
            session.add(
                WorkflowRunModel(
                    workflow_run_id=run_id,
                    workflow_id="wf_1",
                    workflow_permanent_id=WPID,
                    organization_id=ORG,
                    status=WorkflowRunStatus.created.value,
                    start_fresh_browser=True,
                )
            )
            await session.commit()
    await _age_claims(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("item_state", ["dispatching", "dispatched"])
async def test_an_item_that_never_started_ends_failed_to_start_when_failing_its_child_advances_the_group(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch, item_state: str
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await _leave_item_zero_unstarted(env, group_id, run_ids[0], item_state)
    fail_run = app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final

    async def fail_then_run_terminal_hook(**kwargs: Any) -> WorkflowRun | None:
        failed = await fail_run(**kwargs)
        await group_service.advance_workflow_run_group(group_id)
        return failed

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", fail_then_run_terminal_hook)

    await group_service.recover_workflow_run_groups()

    assert await _status(env, run_ids[0]) == WorkflowRunStatus.failed
    assert (await _states(env, group_id))[:2] == [
        WorkflowRunGroupItemState.failed_to_start,
        WorkflowRunGroupItemState.dispatched,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("executor_status", "outcome"),
    [
        (WorkflowRunStatus.queued, WorkflowRunGroupItemOutcome.canceled),
        (WorkflowRunStatus.running, WorkflowRunGroupItemOutcome.unknown),
    ],
)
async def test_a_child_an_executor_took_while_the_sweep_failed_it_holds_the_next_item_until_it_stops(
    env: GroupEnv,
    monkeypatch: pytest.MonkeyPatch,
    executor_status: WorkflowRunStatus,
    outcome: WorkflowRunGroupItemOutcome,
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await _leave_item_zero_unstarted(env, group_id, run_ids[0], "dispatched")
    finish_item = env.database.workflow_run_groups.finish_item

    async def executor_takes_the_child_once_the_sweep_holds_it(*args: Any, **kwargs: Any) -> bool:
        finished = await finish_item(*args, **kwargs)
        if finished and kwargs["state"] == WorkflowRunGroupItemState.dispatched:
            await _set_child_status(env, run_ids[0], executor_status)
        return finished

    monkeypatch.setattr(
        env.database.workflow_run_groups, "finish_item", executor_takes_the_child_once_the_sweep_holds_it
    )

    await group_service.recover_workflow_run_groups()

    assert await _status(env, run_ids[0]) == WorkflowRunStatus.canceled
    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.dispatched
    assert env.executor.executed == run_ids[:1]

    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.workflow_run_id == run_ids[0])
            .values(finished_at=naive_utc_now() - group_service.CHILD_STOP_SETTLE - timedelta(seconds=1))
        )
        await session.commit()
    await group_service.recover_workflow_run_groups()

    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.done
    assert env.executor.executed == run_ids[:2]
    assert (await group_service.get_workflow_run_group(group_id, ORG)).items[0].outcome == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_attempts", [group_service.MAX_DISPATCH_ATTEMPTS - 1, group_service.MAX_DISPATCH_ATTEMPTS]
)
async def test_a_dispatch_that_keeps_dying_fails_its_item_at_the_attempt_cap(
    env: GroupEnv, prior_attempts: int
) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    assert await env.database.workflow_run_groups.claim_next_item(group_id, "dead-owner") is not None
    async with env.database.Session() as session:
        await session.execute(update(WorkflowRunGroupItemModel).values(dispatch_attempts=prior_attempts))
        await session.commit()
    await _age_claims(env)

    await group_service.recover_workflow_run_groups()

    if prior_attempts < group_service.MAX_DISPATCH_ATTEMPTS:
        assert env.executor.executed == run_ids[:1]
    else:
        assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.failed_to_start
        assert env.executor.executed == run_ids[1:2]


@pytest.mark.asyncio
async def test_the_child_after_a_failed_preparation_prepares_in_its_own_context(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)
    await env.database.workflow_params.create_workflow_parameter(
        workflow_id="wf_1", workflow_parameter_type=WorkflowParameterType.INTEGER, key="count", default_value="2"
    )
    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunGroupItemModel)
            .where(WorkflowRunGroupItemModel.position == 0)
            .values(parameters={"login": "cred_1", "count": "abc"})
        )
        await session.commit()
    contexts: dict[str, tuple[str | None, str | None]] = {}

    async def record_context(workflow_run_id: str) -> None:
        context = skyvern_context.current()
        contexts[workflow_run_id] = (context.run_id, context.root_workflow_run_id) if context else (None, None)

    env.executor.before_queue = record_context

    await group_service.advance_workflow_run_group(group_id)

    assert (await _states(env, group_id))[0] == WorkflowRunGroupItemState.failed_to_start
    assert env.executor.executed == [run_ids[1]]
    assert contexts[run_ids[1]] == (run_ids[1], run_ids[1])


@pytest.mark.asyncio
async def test_scrubbed_group_keeps_no_caller_keys_and_a_replay_of_its_key_conflicts(env: GroupEnv) -> None:
    group_id = await _submit(env)
    run_ids = await _run_ids(env, group_id)

    assert await env.database.workflow_run_groups.scrub_keys(ORG, [group_id]) == 1

    async with env.database.Session() as session:
        stored = [
            *(await session.scalars(select(WorkflowRunGroupModel.submission_key))).all(),
            *(await session.scalars(select(WorkflowRunGroupItemModel.item_key))).all(),
        ]
    assert not any(raw in value for value in stored for raw in ("sub-1", "acct-"))
    # A group still running when it passes the cap must keep running its pending items with their own inputs.
    await group_service.advance_workflow_run_group(group_id)
    child_parameters = await env.database.workflow_runs.get_workflow_run_parameters(run_ids[0])
    assert [parameter.value for _, parameter in child_parameters] == ["cred_2"]
    with pytest.raises(SkyvernHTTPException) as exc_info:
        await _submit(env)
    assert exc_info.value.status_code == 409
    assert await count_rows(env, WorkflowRunGroupModel) == 1
    with pytest.raises(ValidationError):
        _request(key=stored[0])
    with pytest.raises(ValidationError):
        _request(items=[{"key": stored[1], "parameters": {"login": "cred_1"}}])


@pytest.mark.asyncio
async def test_an_edit_landing_between_validation_and_insert_refuses_the_submit(
    env: GroupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    validate = app.WORKFLOW_SERVICE.validate_schedule_parameters

    async def edit_after_validation(
        workflow: Workflow, organization: Organization, request_data: dict[str, Any] | None
    ) -> None:
        await validate(workflow, organization, request_data)
        async with env.database.Session() as session:
            await session.execute(update(WorkflowModel).values(modified_at=naive_utc_now() + timedelta(seconds=1)))
            await session.commit()

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "validate_schedule_parameters", edit_after_validation)

    with pytest.raises(SkyvernHTTPException) as exc_info:
        await _submit(env)

    assert exc_info.value.status_code == 409
    assert await count_rows(env, WorkflowRunGroupModel) == 0


@pytest.mark.asyncio
async def test_another_organization_cannot_read_or_cancel_a_group(env: GroupEnv) -> None:
    group_id = await _submit(env)

    for call in (group_service.get_workflow_run_group, group_service.cancel_workflow_run_group):
        with pytest.raises(SkyvernHTTPException) as exc_info:
            await call(group_id, OTHER_ORG)
        assert exc_info.value.status_code == 404

    group = await env.database.workflow_run_groups.get_group(group_id)
    assert group is not None and group.status == WorkflowRunGroupStatus.active


@pytest.mark.asyncio
async def test_a_new_group_consumes_a_submit_token_and_a_replay_does_not(env: GroupEnv) -> None:
    first = await _submit(env)
    replay = await _submit(env)

    assert replay == first
    assert env.limiter.calls == [ORG]


@pytest.mark.asyncio
async def test_item_parameters_are_cleared_once_the_item_leaves_dispatch(env: GroupEnv) -> None:
    group_id = await _submit(env)
    assert await env.database.workflow_run_groups.claim_next_item(group_id, "owner") is not None
    assert await env.database.workflow_run_groups.finish_item(
        group_id,
        0,
        state=WorkflowRunGroupItemState.failed_to_start,
        from_states=(WorkflowRunGroupItemState.dispatching,),
        dispatch_token="owner",
    )
    await group_service.advance_workflow_run_group(group_id)
    items = await env.database.workflow_run_groups.get_items(group_id)
    assert [item.state for item in items[:2]] == [
        WorkflowRunGroupItemState.failed_to_start,
        WorkflowRunGroupItemState.dispatched,
    ]
    assert [item.parameters for item in items] == [{}, {}, {"login": "cred_2"}]

    await group_service.cancel_workflow_run_group(group_id, ORG)

    assert [item.parameters for item in await env.database.workflow_run_groups.get_items(group_id)] == [{}, {}, {}]


@pytest.mark.asyncio
async def test_prepare_error_text_never_reaches_the_item_or_the_group_read(env: GroupEnv) -> None:
    sentinel = "SENTINEL-acct-7731"
    group_id = await _submit(env)
    await env.database.workflow_params.create_workflow_parameter(
        workflow_id="wf_1", workflow_parameter_type=WorkflowParameterType.INTEGER, key="count", default_value=None
    )
    async with env.database.Session() as session:
        await session.execute(
            update(WorkflowRunGroupItemModel).values(parameters={"login": "cred_1", "count": sentinel})
        )
        await session.commit()

    await group_service.advance_workflow_run_group(group_id)

    item = (await env.database.workflow_run_groups.get_items(group_id))[0]
    assert item.state == WorkflowRunGroupItemState.failed_to_start
    assert item.failure_reason == "Workflow run could not be prepared (InvalidWorkflowParameter)"
    group = await group_service.get_workflow_run_group(group_id, ORG)
    assert sentinel not in group.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting", [0, 1, 2])
@pytest.mark.parametrize(
    ("wait_for_completion", "supplies_session"),
    [(False, False), (True, True)],
    ids=["detached", "sync_into_supplied_session"],
)
async def test_unfresh_trigger_under_a_group_child_fails_before_creating_a_run(
    env: GroupEnv,
    monkeypatch: pytest.MonkeyPatch,
    nesting: int,
    wait_for_completion: bool,
    supplies_session: bool,
) -> None:
    group_id = await _submit(env)
    await group_service.advance_workflow_run_group(group_id)
    trigger_run_id = (await _run_ids(env, group_id))[0]
    for _ in range(nesting):
        trigger_run_id = await _start_nested_run(env, trigger_run_id)
    browser_session_id = (
        (
            await env.database.browser_sessions.create_persistent_browser_session(organization_id=ORG)
        ).persistent_browser_session_id
        if supplies_session
        else None
    )
    runs_before = await count_rows(env, WorkflowRunModel)
    block = WorkflowTriggerBlock(
        label="detached",
        workflow_permanent_id=WPID,
        payload={"login": "cred_1"},
        wait_for_completion=wait_for_completion,
        browser_session_id=browser_session_id,
        output_parameter=make_block_output_parameter("detached"),
    )
    result = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(WorkflowTriggerBlock, "get_workflow_run_context", lambda self, workflow_run_id: MagicMock())
    monkeypatch.setattr(WorkflowTriggerBlock, "record_output_parameter_value", AsyncMock())
    monkeypatch.setattr(WorkflowTriggerBlock, "build_block_result", result)

    await block.execute(workflow_run_id=trigger_run_id, workflow_run_block_id="wrb_detached", organization_id=ORG)

    assert result.await_args is not None and result.await_args.kwargs["success"] is False
    assert await count_rows(env, WorkflowRunModel) == runs_before


@pytest.mark.asyncio
async def test_two_concurrent_repeat_checked_creations_on_postgres_admit_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if make_url(str(settings.DATABASE_STRING)).get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL row and advisory locks")
    read_latest = workflow_run_groups_repository._latest_group_ids_by_item_key
    reads: list[None] = []
    both_read = asyncio.Event()

    # Holds each creation after its history read, so without the locks both read before either inserts.
    async def read_then_wait_for_the_other(*args: Any) -> dict[str, str]:
        latest = await read_latest(*args)
        reads.append(None)
        if len(reads) >= 2:
            both_read.set()
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_read.wait(), 0.5)
        return latest

    monkeypatch.setattr(workflow_run_groups_repository, "_latest_group_ids_by_item_key", read_then_wait_for_the_other)
    database = AgentDB(str(settings.DATABASE_STRING))
    organization = await database.organizations.create_organization(organization_name=f"race-{uuid4().hex}")
    workflow_id, wpid = f"wf_{uuid4().hex}", f"wpid_{uuid4().hex}"
    async with database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id=workflow_id,
                workflow_permanent_id=wpid,
                organization_id=organization.organization_id,
                title="Workflow",
                version=1,
                workflow_definition=run_group_definition(run_group_task_block()),
            )
        )
        await session.commit()

    async def create(label: str) -> object:
        try:
            return await database.workflow_run_groups.create_group(
                organization_id=organization.organization_id,
                workflow_permanent_id=wpid,
                requested_version=1,
                workflow_id=workflow_id,
                submission_key=f"copilot:{label}:key",
                input_fingerprint=label,
                items=[("cred_race", {})],
                expected_latest_groups=("copilot:", {}),
            )
        except GroupAccountsRanSinceReview as e:
            return e

    try:
        results = await asyncio.gather(create("a"), create("b"))
    finally:
        await database.engine.dispose()

    assert sum(isinstance(result, GroupAccountsRanSinceReview) for result in results) == 1
