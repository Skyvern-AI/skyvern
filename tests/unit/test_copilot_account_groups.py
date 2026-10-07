import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_core import to_json
from sqlalchemy import delete, func, select, update

from skyvern.config import settings
from skyvern.exceptions import WorkflowPinnedByRunGroup
from skyvern.forge import app
from skyvern.forge.sdk.copilot.ask_user import AccountGroupReview
from skyvern.forge.sdk.copilot.browser_ablation import resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import account_groups, copilot_native_tools
from skyvern.forge.sdk.copilot.tools.account_groups import (
    AccountGroupRefusal,
    AccountGroupState,
    AccountGroupSubmission,
    account_group_status,
    cancel_account_group,
    run_for_accounts,
)
from skyvern.forge.sdk.db.models import (
    CredentialModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunBlockModel,
    WorkflowRunGroupItemModel,
    WorkflowRunGroupModel,
    WorkflowRunModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.routes import workflow_copilot as routes
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotPendingTurn
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import COPILOT_TEST_WORKFLOW_CREATOR, WorkflowRunStatus
from skyvern.schemas.workflow_run_groups import (
    MAX_WORKFLOW_RUN_GROUP_ITEMS,
    WorkflowRunGroupCreateRequest,
    WorkflowRunGroupItemOutcome,
    WorkflowRunGroupItemRequest,
    WorkflowRunGroupItemState,
    WorkflowRunGroupStatus,
)
from skyvern.services import workflow_run_group_service as group_service
from tests.unit.conftest import (
    RUN_GROUP_ORG,
    RUN_GROUP_WPID,
    GroupEnv,
    run_group_definition,
    run_group_task_block,
)

SECRET = "vault-password-fixture-81723"


@dataclass
class Chat:
    env: GroupEnv
    ctx: CopilotContext
    client: AsyncClient
    frames: asyncio.Queue[dict[str, Any]]


@pytest_asyncio.fixture
async def chat(run_group_env: GroupEnv, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Chat]:
    async with run_group_env.database.Session() as session:
        session.add_all(
            [
                CredentialModel(
                    credential_id="cred_3",
                    organization_id=RUN_GROUP_ORG,
                    name="Billing",
                    credential_type="password",
                    username="alex@example.com",
                    item_id="item_cred_3",
                ),
                CredentialModel(
                    credential_id="cred_revoked",
                    organization_id=RUN_GROUP_ORG,
                    name="Old",
                    credential_type="password",
                    item_id="item_cred_revoked",
                    deleted_at=datetime.now(UTC).replace(tzinfo=None),
                ),
            ]
        )
        await session.commit()
    await run_group_env.database.workflow_params.create_workflow_parameter(
        workflow_id="wf_1", workflow_parameter_type=WorkflowParameterType.STRING, key="note", default_value="none"
    )
    vault = SimpleNamespace(
        get_credential_item=AsyncMock(return_value=SimpleNamespace(credential=SimpleNamespace(password=SECRET)))
    )
    monkeypatch.setattr(
        object.__getattribute__(app, "_inst"),
        "CREDENTIAL_VAULT_SERVICES",
        defaultdict(lambda: vault),
        raising=False,
    )
    monkeypatch.setattr(app, "CACHE", None)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.ask_user.QUESTION_POLL_SECONDS", 0.01)
    repo = run_group_env.database.workflow_params
    row = await repo.create_workflow_copilot_chat(organization_id=RUN_GROUP_ORG, workflow_permanent_id=RUN_GROUP_WPID)
    await repo.start_copilot_turn(
        organization_id=RUN_GROUP_ORG,
        workflow_copilot_chat_id=row.workflow_copilot_chat_id,
        pending_turn=CopilotPendingTurn(turn_id="turn", started_at=datetime.now(UTC), cancel_token="stop"),
        user_message="Run it for my accounts",
    )
    api = FastAPI()
    api.dependency_overrides[routes.org_auth_service.get_current_org] = lambda: run_group_env.organization
    api.add_api_route("/reply", routes.workflow_copilot_question_response, methods=["POST"])
    api.add_api_route("/history", routes.workflow_copilot_chat_history, methods=["GET"])
    frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ctx = CopilotContext(
        organization_id=RUN_GROUP_ORG,
        workflow_id="wf_1",
        workflow_permanent_id=RUN_GROUP_WPID,
        workflow_yaml="",
        browser_session_id=None,
        stream=None,
        api_key="",
        turn_id="turn",
        workflow_copilot_chat_id=row.workflow_copilot_chat_id,
        copilot_cancel_token="stop",
    )
    ctx.stream = SimpleNamespace(send=frames.put)
    await _seed_run(run_group_env)
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://fixture") as client:
        yield Chat(run_group_env, ctx, client, frames)


async def _seed_run(
    env: GroupEnv,
    *,
    workflow_id: str = "wf_1",
    credential_id: str = "cred_1",
    blocks: tuple[tuple[str, str, str | None], ...] = (("login", "completed", None),),
    status: WorkflowRunStatus = WorkflowRunStatus.completed,
    group: bool = True,
) -> None:
    workflow = await env.database.workflows.get_workflow(workflow_id)
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow_id)
    login = next(parameter for parameter in parameters if parameter.key == "login")
    workflow_run_id = f"wr_{uuid4().hex}"
    block_ids = {label: f"wrb_{uuid4().hex}" for label, _, _ in blocks}
    async with env.database.Session() as session:
        if group:
            group_id = f"wrg_{uuid4().hex}"
            assert workflow is not None
            session.add(
                WorkflowRunGroupModel(
                    workflow_run_group_id=group_id,
                    organization_id=RUN_GROUP_ORG,
                    workflow_permanent_id=RUN_GROUP_WPID,
                    workflow_id=workflow_id,
                    submission_key=f"seed:{workflow_run_id}:{account_groups.workflow_definition_hash(workflow)}",
                    input_fingerprint="seed",
                    status=WorkflowRunGroupStatus.finished.value,
                )
            )
            session.add(
                WorkflowRunGroupItemModel(
                    workflow_run_group_id=group_id,
                    position=0,
                    item_key=credential_id,
                    parameters={"login": credential_id},
                    workflow_run_id=workflow_run_id,
                    state=WorkflowRunGroupItemState.done.value,
                )
            )
        session.add(
            WorkflowRunModel(
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=RUN_GROUP_WPID,
                organization_id=RUN_GROUP_ORG,
                status=status.value,
            )
        )
        session.add(
            WorkflowRunParameterModel(
                workflow_run_id=workflow_run_id,
                workflow_parameter_id=login.workflow_parameter_id,
                value=credential_id,
            )
        )
        session.add_all(
            WorkflowRunBlockModel(
                workflow_run_block_id=block_ids[label],
                parent_workflow_run_block_id=block_ids[parent] if parent else None,
                workflow_run_id=workflow_run_id,
                organization_id=RUN_GROUP_ORG,
                block_type="task",
                status=block_status,
                label=label,
            )
            for label, block_status, parent in blocks
        )
        await session.commit()


def _start(
    chat: Chat, credential_ids: list[str], **overrides: str | dict[str, str] | None
) -> asyncio.Task[AccountGroupSubmission | AccountGroupRefusal]:
    arguments: dict[str, Any] = {
        "credential_parameter_key": "login",
        "common_inputs": {},
        "action_summary": "Download the latest statement",
        **overrides,
    }
    return asyncio.create_task(
        run_for_accounts(chat.ctx, tool_call_id=uuid4().hex, credential_ids=credential_ids, **arguments)
    )


async def _card(chat: Chat) -> dict[str, Any]:
    while (frame := await asyncio.wait_for(chat.frames.get(), 5))["type"] != "question_required":
        pass
    return frame["interactions"][0]


async def _decide(chat: Chat, card: dict[str, Any], decision: dict[str, Any]) -> int:
    reply = await chat.client.post(
        "/reply",
        json={
            "workflow_copilot_chat_id": chat.ctx.workflow_copilot_chat_id,
            "interaction_id": card["interaction_id"],
            "account_group_decision": decision,
        },
    )
    return reply.status_code


async def _groups_from_this_chat(chat: Chat) -> int:
    prefix = account_groups.account_group_key_prefix(chat.ctx.workflow_copilot_chat_id)
    async with chat.env.database.Session() as session:
        query = select(func.count()).where(WorkflowRunGroupModel.submission_key.startswith(prefix))
        return int(await session.scalar(query) or 0)


async def _approved_group(chat: Chat, credential_ids: list[str]) -> str:
    task = _start(chat, credential_ids)
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": credential_ids}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, AccountGroupSubmission) and result.workflow_run_group_id is not None
    return result.workflow_run_group_id


@pytest.mark.asyncio
async def test_http_approval_submits_exactly_the_checked_subset_without_secrets(chat: Chat) -> None:
    task = _start(chat, ["cred_1", "cred_2", "cred_3"])
    card = await _card(chat)
    review = card["account_group_review"]
    assert [row["credential_id"] for row in review["rows"]] == ["cred_1", "cred_2", "cred_3"]
    assert review["rows"][2]["label"] == "Billing (al***@example.com)"
    assert (review["version"], review["workflow_id"]) == (1, "wf_1")

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupSubmission) and result.dispatched
    assert {row.outcome for row in result.rows} <= {"pending", "in_progress"}
    group_id = result.workflow_run_group_id
    assert group_id is not None
    items = await chat.env.database.workflow_run_groups.get_items(group_id)
    assert [(item.item_key, item.parameters) for item in items] == [
        ("cred_1", {"login": "cred_1"}),
        ("cred_3", {"login": "cred_3"}),
    ]
    history = await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.ctx.workflow_copilot_chat_id})
    restored = history.json()["question_interactions"]
    assert [interaction["account_group_review"]["workflow_run_group_id"] for interaction in restored] == [group_id]
    # A backend rolled back to before account groups validates the stored response with extra="forbid".
    assert set(restored[0]["response"]) <= {"answers", "text", "skipped"}
    assert restored[0]["account_group_review"]["decision"] == {"approved": True, "credential_ids": ["cred_1", "cred_3"]}
    for surface in (card, result, restored, [item.parameters for item in items]):
        assert SECRET not in to_json(surface).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "duplicate",
        "other_org",
        "revoked",
        "over_limit",
    ],
)
async def test_refused_selection_shows_no_card_and_submits_nothing(chat: Chat, case: str) -> None:
    credential_ids = {
        "unknown": ["cred_1", "cred_missing"],
        "duplicate": ["cred_1", "cred_2", "cred_1"],
        "other_org": ["cred_1", "cred_foreign"],
        "revoked": ["cred_1", "cred_revoked"],
        "over_limit": [f"cred_{i}" for i in range(MAX_WORKFLOW_RUN_GROUP_ITEMS + 1)],
    }[case]

    result = await asyncio.wait_for(_start(chat, credential_ids), 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    if case == "over_limit":
        assert all(f"'{credential_id}'" in result.error for credential_id in credential_ids)
    assert chat.frames.empty()
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["clean", "no_run", "older_version_child", "failed_branch", "other_account"])
async def test_the_card_reports_whether_the_saved_version_ran_cleanly_without_refusing(chat: Chat, case: str) -> None:
    async with chat.env.database.Session() as session:
        await session.execute(delete(WorkflowRunModel))
        await session.commit()
    if case == "clean":
        await _seed_run(
            chat.env,
            blocks=(("login", "completed", None), ("branch", "completed", None), ("unused_arm", "skipped", "branch")),
        )
    if case == "older_version_child":
        await _seed_run(chat.env)
        await _add_version(chat)
    if case == "failed_branch":
        await _seed_run(
            chat.env,
            blocks=(("login", "completed", None), ("branch", "completed", None), ("download", "failed", "branch")),
        )
    if case == "other_account":
        await _seed_run(chat.env, credential_id="cred_3")

    task = _start(chat, ["cred_1", "cred_2"])

    card = await _card(chat)
    review = card["account_group_review"]
    assert (review["clean_group_run_id"] is not None) is (case == "clean")
    assert [row["preselected"] for row in review["rows"]] == [True, True]
    assert await _decide(chat, card, {"approved": False}) == 200
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_an_account_that_already_ran_starts_unchecked_without_naming_the_prior_group(chat: Chat) -> None:
    group_id = await _approved_group(chat, ["cred_1"])
    (item,) = await chat.env.database.workflow_run_groups.get_items(group_id)
    await group_service.advance_workflow_run_group(group_id)
    await chat.env.database.workflow_runs.update_workflow_run(item.workflow_run_id, status=WorkflowRunStatus.completed)
    await group_service.advance_workflow_run_group(group_id)

    task = _start(chat, ["cred_1", "cred_2"])

    card = await _card(chat)
    rows = card["account_group_review"]["rows"]
    assert [(row["credential_id"], row["prior_outcome"], row["preselected"]) for row in rows] == [
        ("cred_1", "completed", False),
        ("cred_2", None, True),
    ]
    assert await _decide(chat, card, {"approved": False}) == 200
    await asyncio.wait_for(task, 5)


async def _add_version(
    chat: Chat, workflow_id: str = "wf_2", version: int = 2, *, deleted: bool = False, created_by: str | None = None
) -> None:
    async with chat.env.database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id=workflow_id,
                workflow_permanent_id=RUN_GROUP_WPID,
                organization_id=RUN_GROUP_ORG,
                title="Workflow",
                version=version,
                workflow_definition=run_group_definition(run_group_task_block()),
                deleted_at=datetime.now(UTC).replace(tzinfo=None) if deleted else None,
                created_by=created_by,
            )
        )
        await session.commit()
    await chat.env.database.workflow_params.create_workflow_parameter(
        workflow_id=workflow_id,
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        key="login",
        default_value=None,
    )


async def _edit_in_place(chat: Chat) -> None:
    async with chat.env.database.Session() as session:
        await session.execute(
            update(WorkflowModel)
            .where(WorkflowModel.workflow_id == "wf_1")
            .values(modified_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=5))
        )
        await session.commit()


async def _retype_param(chat: Chat) -> None:
    async with chat.env.database.Session() as session:
        await session.execute(
            update(WorkflowParameterModel)
            .where(WorkflowParameterModel.key == "login")
            .values(workflow_parameter_type="string")
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["decline", "new_version", "edited_in_place", "param_retyped"],
)
async def test_decline_or_stale_approval_dispatches_nothing(chat: Chat, case: str) -> None:
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    if case == "decline":
        assert await _decide(chat, card, {"approved": False}) == 200
    else:
        await {"new_version": _add_version, "edited_in_place": _edit_in_place, "param_retyped": _retype_param}[case](
            chat
        )
        assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200

    result = await asyncio.wait_for(task, 5)

    if case == "decline":
        assert isinstance(result, AccountGroupSubmission) and not result.dispatched
    else:
        assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
async def test_copilot_restore_is_refused_while_the_group_runs(chat: Chat) -> None:
    await _approved_group(chat, ["cred_1", "cred_2"])

    saved = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=RUN_GROUP_WPID, organization_id=RUN_GROUP_ORG
    )
    with pytest.raises(WorkflowPinnedByRunGroup):
        await routes._restore_workflow_definition(saved, RUN_GROUP_ORG)


async def _running_group_of_three(chat: Chat) -> str:
    group_id = await _approved_group(chat, ["cred_1", "cred_2", "cred_3"])
    await group_service.advance_workflow_run_group(group_id)
    first, second, _ = await chat.env.database.workflow_run_groups.get_items(group_id)
    await chat.env.database.workflow_runs.update_workflow_run(first.workflow_run_id, status=WorkflowRunStatus.completed)
    await group_service.advance_workflow_run_group(group_id)
    await chat.env.database.workflow_runs.update_workflow_run(second.workflow_run_id, status=WorkflowRunStatus.running)
    while not chat.frames.empty():
        chat.frames.get_nowait()
    return group_id


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["approve", "decline"])
async def test_only_an_approved_cancel_card_cancels_the_group(chat: Chat, answer: str) -> None:
    group_id = await _running_group_of_three(chat)

    task = asyncio.create_task(
        cancel_account_group(chat.ctx, tool_call_id="call-cancel", workflow_run_group_id=group_id)
    )
    card = await _card(chat)
    unfinished = card["account_group_cancel"]["unfinished_rows"]
    assert [(row["credential_id"], row["outcome"]) for row in unfinished] == [
        ("cred_2", "in_progress"),
        ("cred_3", "pending"),
    ]
    assert await _decide(chat, card, {"approved": answer == "approve", "credential_ids": []}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, AccountGroupState) and result.cancel_approved is (answer == "approve")

    group = await group_service.get_workflow_run_group(group_id, RUN_GROUP_ORG)
    if answer == "approve":
        assert [item.outcome for item in group.items] == [
            WorkflowRunGroupItemOutcome.completed,
            WorkflowRunGroupItemOutcome.unknown,
            WorkflowRunGroupItemOutcome.canceled,
        ]
    else:
        assert group.status == WorkflowRunGroupStatus.active
        assert WorkflowRunGroupItemOutcome.canceled not in [item.outcome for item in group.items]


@pytest.mark.parametrize("withheld_by", [None, "browser_authority", "rollback_flag"])
def test_withholding_submission_hides_only_the_run_tool(
    monkeypatch: pytest.MonkeyPatch, withheld_by: str | None
) -> None:
    if withheld_by == "rollback_flag":
        monkeypatch.setattr(settings, "COPILOT_ACCOUNT_GROUP_SUBMIT_ENABLED", False)
    surface = resolve_copilot_tool_surface(
        mode=None,
        native_tools=copilot_native_tools(
            supports_question_tool=True,
            supports_account_group_card=True,
            browser_code_available=False,
            run_tools_available=True,
        ),
        alias_map={},
        overlays={},
        browser_tools_available=withheld_by != "browser_authority",
    )

    names = set(surface.ordered_native_names)
    assert {"get_account_group_status", "cancel_account_group"} <= names
    assert ("run_workflow_for_accounts" in names) is (withheld_by is None)


@pytest.mark.asyncio
async def test_retry_starts_running_and_repeat_risk_rows_unchecked(chat: Chat) -> None:
    group_id = await _approved_group(chat, ["cred_1", "cred_2", "cred_3"])

    early = _start(chat, ["cred_1", "cred_2", "cred_3"])
    early_card = await _card(chat)
    assert [row["preselected"] for row in early_card["account_group_review"]["rows"]] == [False] * 3
    assert await _decide(chat, early_card, {"approved": False}) == 200
    await asyncio.wait_for(early, 5)

    items = await chat.env.database.workflow_run_groups.get_items(group_id)
    for item, status in zip(
        items, (WorkflowRunStatus.completed, WorkflowRunStatus.failed, WorkflowRunStatus.failed), strict=True
    ):
        await group_service.advance_workflow_run_group(group_id)
        if item.item_key == "cred_3":
            await chat.env.database.workflow_runs.update_workflow_run(item.workflow_run_id, script_id="s_1")
        await chat.env.database.workflow_runs.update_workflow_run(
            item.workflow_run_id, status=status, failure_reason=f"page said: wrong password for {item.item_key}"
        )
    await group_service.advance_workflow_run_group(group_id)

    status = await account_group_status(chat.ctx, group_id)
    assert isinstance(status, AccountGroupState) and "wrong password" not in status.model_dump_json()
    assert [row.outcome for row in status.rows] == [
        WorkflowRunGroupItemOutcome.completed,
        WorkflowRunGroupItemOutcome.failed,
        WorkflowRunGroupItemOutcome.unknown,
    ]

    retry = _start(chat, ["cred_1", "cred_2", "cred_3"])
    card = await _card(chat)
    rows = card["account_group_review"]["rows"]
    assert [(row["prior_outcome"], row["preselected"]) for row in rows] == [
        ("completed", False),
        ("failed", True),
        ("unknown", False),
    ]

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    result = await asyncio.wait_for(retry, 5)

    assert isinstance(result, AccountGroupSubmission)
    retry_id = result.workflow_run_group_id
    assert [row.credential_id for row in result.repeated_after_prior_effect] == ["cred_1"]
    retry_items = await chat.env.database.workflow_run_groups.get_items(retry_id)
    assert [item.item_key for item in retry_items] == ["cred_1", "cred_2"]
    assert not {item.workflow_run_id for item in retry_items} & {item.workflow_run_id for item in items}


@pytest.mark.asyncio
async def test_an_account_that_ran_elsewhere_after_the_review_refuses_the_approval(chat: Chat) -> None:
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    await group_service.submit_workflow_run_group(
        chat.env.organization,
        WorkflowRunGroupCreateRequest(
            workflow_id=RUN_GROUP_WPID,
            submission_key=f"{account_groups.account_group_key_prefix('wcc_other')}interaction:hash",
            items=[WorkflowRunGroupItemRequest(key="cred_1", parameters={"login": "cred_1"})],
        ),
    )

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
async def test_a_group_started_elsewhere_after_the_recheck_is_caught_under_the_creation_lock(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    recheck = account_groups._recheck_approval

    async def recheck_then_race(
        ctx: CopilotContext, review: AccountGroupReview, approved_ids: list[str]
    ) -> dict[str, str]:
        latest_groups = await recheck(ctx, review, approved_ids)
        await group_service.submit_workflow_run_group(
            chat.env.organization,
            WorkflowRunGroupCreateRequest(
                workflow_id=RUN_GROUP_WPID,
                submission_key=f"{account_groups.account_group_key_prefix('wcc_other')}interaction:hash",
                items=[WorkflowRunGroupItemRequest(key="cred_1", parameters={"login": "cred_1"})],
            ),
        )
        return latest_groups

    monkeypatch.setattr(account_groups, "_recheck_approval", recheck_then_race)
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and result.status_code == 409
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
async def test_an_account_replaced_under_the_same_id_after_the_review_refuses_the_approval(chat: Chat) -> None:
    task = _start(chat, ["cred_1", "cred_3"])
    card = await _card(chat)
    async with chat.env.database.Session() as session:
        await session.execute(
            update(CredentialModel).where(CredentialModel.credential_id == "cred_3").values(username="al@example.com")
        )
        await session.commit()

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and "cred_3" in result.error
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
async def test_history_does_not_link_a_group_that_claimed_the_card_key_with_other_inputs(chat: Chat) -> None:
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    review = card["account_group_review"]
    await group_service.submit_workflow_run_group(
        chat.env.organization,
        WorkflowRunGroupCreateRequest(
            workflow_id=RUN_GROUP_WPID,
            submission_key=account_groups.account_group_submission_key(
                chat.ctx.workflow_copilot_chat_id, card["interaction_id"], review["definition_hash"]
            ),
            items=[WorkflowRunGroupItemRequest(key="cred_1", parameters={"login": "cred_1", "note": "other"})],
        ),
    )

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    assert isinstance(await asyncio.wait_for(task, 5), AccountGroupRefusal)

    history = await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.ctx.workflow_copilot_chat_id})
    (restored,) = history.json()["question_interactions"]
    assert restored["account_group_review"]["workflow_run_group_id"] is None


@pytest.mark.asyncio
async def test_a_proposal_published_while_the_card_waits_refuses_the_approval(chat: Chat) -> None:
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)
    chat.ctx.has_staged_proposal = True

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert await _groups_from_this_chat(chat) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_by", [None, COPILOT_TEST_WORKFLOW_CREATOR])
async def test_only_a_saved_version_after_the_recheck_refuses_at_group_creation(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, saved_by: str | None
) -> None:
    recheck = account_groups._recheck_approval

    async def recheck_then_save_a_version(
        ctx: CopilotContext, review: AccountGroupReview, approved_ids: list[str]
    ) -> dict[str, str]:
        latest_groups = await recheck(ctx, review, approved_ids)
        await _add_version(chat, created_by=saved_by)
        return latest_groups

    monkeypatch.setattr(account_groups, "_recheck_approval", recheck_then_save_a_version)
    task = _start(chat, ["cred_1", "cred_2"])
    card = await _card(chat)

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_1", "cred_2"]}) == 200
    result = await asyncio.wait_for(task, 5)

    if saved_by is None:
        assert isinstance(result, AccountGroupRefusal) and result.status_code == 409
        assert await _groups_from_this_chat(chat) == 0
    else:
        assert isinstance(result, AccountGroupSubmission) and result.dispatched
