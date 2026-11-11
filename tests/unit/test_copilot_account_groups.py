import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue
from pydantic_core import to_json
from sqlalchemy import delete, func, select, update

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException, WorkflowPinnedByRunGroup
from skyvern.forge import app
from skyvern.forge.sdk.copilot.ask_user import AccountGroupReview, QuestionInteraction
from skyvern.forge.sdk.copilot.browser_ablation import resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import account_groups, copilot_native_tools
from skyvern.forge.sdk.copilot.tools.account_groups import (
    AccountGroupRefusal,
    AccountGroupState,
    AccountGroupSubmission,
    SingleRunSubmission,
    account_group_status,
    cancel_account_group,
    run_for_accounts,
    run_saved_workflow,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.models import (
    BrowserProfileModel,
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
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotPendingTurn, WorkflowCopilotChatSender
from skyvern.forge.sdk.services import request_principal
from skyvern.forge.sdk.services.request_principal import (
    AuthKind,
    BearerIdentityStatus,
    RequestPrincipal,
    scoped_api_key_id_from_actor,
)
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import (
    COPILOT_TEST_WORKFLOW_CREATOR,
    WorkflowDefinition,
    WorkflowRunStatus,
)
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
ORG_KEY = RequestPrincipal(RUN_GROUP_ORG, AuthKind.api_key)


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
        get_credential_item=AsyncMock(
            return_value=SimpleNamespace(credential=SimpleNamespace(password=SECRET), login_sites=None)
        )
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


@contextmanager
def _as_requester(principal: RequestPrincipal) -> Iterator[None]:
    token = request_principal._request_principal.set(
        (("fixture", None, None, None), principal, BearerIdentityStatus.no_bearer)
    )
    try:
        yield
    finally:
        request_principal._request_principal.reset(token)


def _start(
    chat: Chat, credential_ids: list[str], **overrides: str | dict[str, str] | None
) -> asyncio.Task[AccountGroupSubmission | AccountGroupRefusal]:
    arguments: dict[str, Any] = {
        "credential_parameter_key": "login",
        "common_inputs": {},
        "action_summary": "Download the latest statement",
        **overrides,
    }
    # Submitting needs a requester allowed to run workflows; a principal the test already set wins.
    with _as_requester(request_principal.get_request_principal() or ORG_KEY):
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


async def _run_count(chat: Chat) -> int:
    async with chat.env.database.Session() as session:
        return int(await session.scalar(select(func.count()).select_from(WorkflowRunModel)) or 0)


@contextmanager
def _in_turn(principal: RequestPrincipal) -> Iterator[None]:
    # A run created inside the turn's context would inherit its copilot_session_id.
    turn = SkyvernContext(organization_id=RUN_GROUP_ORG, request_id="req_turn", copilot_session_id="wcc_turn")
    token = request_principal._request_principal.set(
        (("fixture", None, None, None), principal, BearerIdentityStatus.no_bearer)
    )
    try:
        with skyvern_context.scoped(turn):
            yield
    finally:
        request_principal._request_principal.reset(token)


def _start_single_run(
    chat: Chat, inputs: dict[str, JsonValue], principal: RequestPrincipal = ORG_KEY
) -> asyncio.Task[SingleRunSubmission | AccountGroupRefusal]:
    with _in_turn(principal):
        return asyncio.create_task(
            run_saved_workflow(
                chat.ctx, tool_call_id=uuid4().hex, inputs=inputs, action_summary="Download the latest statement"
            )
        )


async def _restored_review(chat: Chat) -> dict[str, Any]:
    history = await chat.client.get("/history", params={"workflow_copilot_chat_id": chat.ctx.workflow_copilot_chat_id})
    (restored,) = history.json()["question_interactions"]
    return restored


async def _save_profile_default_and_fixed_credential(chat: Chat) -> None:
    now = datetime.now(UTC)
    fixed = CredentialParameter(
        key="portal",
        credential_parameter_id="cp_portal",
        workflow_id="wf_1",
        credential_id="cred_2",
        created_at=now,
        modified_at=now,
    )
    async with chat.env.database.Session() as session:
        session.add(BrowserProfileModel(browser_profile_id="bp_saved", organization_id=RUN_GROUP_ORG, name="Saved"))
        await session.execute(
            update(WorkflowModel)
            .where(WorkflowModel.workflow_id == "wf_1")
            .values(
                browser_profile_id="bp_saved",
                workflow_definition=WorkflowDefinition(parameters=[fixed], blocks=[run_group_task_block()]).model_dump(
                    mode="json"
                ),
            )
        )
        await session.execute(
            update(WorkflowParameterModel).where(WorkflowParameterModel.key == "login").values(default_value="cred_3")
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login_source", "requester"),
    [("inputs", "scoped_key"), ("saved_default", "member"), ("explicit_null", "scoped_key")],
)
async def test_an_approved_single_run_starts_one_real_run_of_the_reviewed_version_with_the_shown_accounts(
    chat: Chat, login_source: str, requester: str
) -> None:
    await _save_profile_default_and_fixed_credential(chat)
    before = await _run_count(chat)
    # An explicit null takes the saved default, as the run service resolves it, so the card must show that account.
    login: dict[str, JsonValue] = {"inputs": {"login": "cred_3"}, "explicit_null": {"login": None}}.get(
        login_source, {}
    )
    inputs: dict[str, JsonValue] = {"note": "May"} | login
    linked_at_dispatch: list[str | None] = []
    context_at_dispatch: list[tuple[str | None, str | None]] = []

    async def read_link(_: str) -> None:
        linked_at_dispatch.append((await _restored_review(chat))["account_group_review"].get("workflow_run_id"))
        run_context = skyvern_context.current()
        assert run_context is not None
        context_at_dispatch.append((run_context.request_id, run_context.copilot_session_id))

    chat.env.executor.before_queue = read_link
    principal = (
        RequestPrincipal(
            RUN_GROUP_ORG, AuthKind.scoped_api_key, api_key_token_id="tok_scoped", api_key_access_mode="scoped"
        )
        if requester == "scoped_key"
        else RequestPrincipal(RUN_GROUP_ORG, AuthKind.bearer, "user_member", "org:member")
    )
    task = _start_single_run(chat, inputs, principal)
    card = await _card(chat)
    review = card["account_group_review"]
    assert review["single_run"] is True
    # The definition's fixed "portal" login runs as the saved workflow binds it and stays off the shared card.
    assert [(row["parameter_key"], row["label"]) for row in review["rows"]] == [
        ("login", "Billing (al***@example.com)")
    ]
    assert review["common_inputs"] == {"note": "May"}
    assert await _run_count(chat) == before

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, SingleRunSubmission) and result.dispatched and result.workflow_run_id is not None
    assert await _run_count(chat) == before + 1
    run = await chat.env.database.workflow_runs.get_workflow_run(result.workflow_run_id, organization_id=RUN_GROUP_ORG)
    assert run is not None
    assert (run.workflow_id, run.trigger_type, run.copilot_session_id) == ("wf_1", WorkflowRunTriggerType.manual, None)
    assert run.browser_profile_id == "bp_saved" and not run.start_fresh_browser
    if requester == "scoped_key":
        assert scoped_api_key_id_from_actor(run.requested_by) == "tok_scoped"
    else:
        assert run.created_by == "user_member"
    parameters = await chat.env.database.workflow_runs.get_workflow_run_parameters(result.workflow_run_id)
    assert {parameter.key: value.value for parameter, value in parameters} == {"login": "cred_3", "note": "May"}
    assert chat.env.executor.executed == [result.workflow_run_id]
    assert linked_at_dispatch == [result.workflow_run_id]
    assert context_at_dispatch == [("req_turn", None)]
    restored = await _restored_review(chat)
    assert restored["account_group_review"]["workflow_run_id"] == result.workflow_run_id
    assert await _groups_from_this_chat(chat) == 0
    assert SECRET not in to_json([card, result, restored]).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "decline",
        "new_version",
        "edited_in_place",
        "account_changed",
        "proposal_published",
        "partial_approval",
        "default_added",
        "edited_while_waiting_for_slot",
    ],
)
async def test_a_declined_or_stale_single_run_starts_nothing(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    before = await _run_count(chat)
    # default_added: the card showed no login, then a saved default appears without touching modified_at.
    task = _start_single_run(chat, {} if case == "default_added" else {"login": "cred_3"})
    card = await _card(chat)
    decision: dict[str, Any] = {"approved": True, "credential_ids": [] if case == "default_added" else ["cred_3"]}
    if case == "default_added":
        async with chat.env.database.Session() as session:
            await session.execute(
                update(WorkflowParameterModel)
                .where(WorkflowParameterModel.key == "login")
                .values(default_value="cred_3")
            )
            await session.commit()
    elif case == "edited_while_waiting_for_slot":
        # Another chat's auto-accept saves over the version after the recheck, while no run row holds it yet.
        @asynccontextmanager
        async def slot_after_an_in_place_save(_: str) -> AsyncIterator[None]:
            await _edit_in_place(chat)
            yield

        monkeypatch.setattr(account_groups, "run_submission_slot", slot_after_an_in_place_save)
    elif case == "decline":
        decision = {"approved": False}
    elif case == "new_version":
        await _add_version(chat)
    elif case == "edited_in_place":
        await _edit_in_place(chat)
    elif case == "account_changed":
        async with chat.env.database.Session() as session:
            await session.execute(
                update(CredentialModel)
                .where(CredentialModel.credential_id == "cred_3")
                .values(username="al@example.com")
            )
            await session.commit()
    elif case == "proposal_published":
        chat.ctx.has_staged_proposal = True
    elif case == "partial_approval":
        decision = {"approved": True, "credential_ids": []}

    status = await _decide(chat, card, decision)

    if case == "partial_approval":
        assert status == 409
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    else:
        assert status == 200
        result = await asyncio.wait_for(task, 5)
        assert result.dispatched is False
    assert await _run_count(chat) == before
    assert chat.env.executor.executed == []


@pytest.mark.asyncio
async def test_a_single_run_with_a_secret_input_shows_no_card(chat: Chat) -> None:
    register_secret_scrub_value(chat.ctx, SECRET)
    before = await _run_count(chat)

    result = await asyncio.wait_for(_start_single_run(chat, {"login": "cred_3", "note": SECRET}), 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert "secret" in result.error and SECRET not in result.error
    assert chat.frames.empty()
    assert await _run_count(chat) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [{}, {"login": "   "}])
async def test_a_credential_input_with_no_value_runs_without_a_credential_as_run_does(
    chat: Chat, inputs: dict[str, JsonValue]
) -> None:
    before = await _run_count(chat)
    task = _start_single_run(chat, inputs)
    card = await _card(chat)
    assert card["account_group_review"]["rows"] == []

    assert await _decide(chat, card, {"approved": True, "credential_ids": []}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, SingleRunSubmission) and result.dispatched and result.workflow_run_id is not None
    assert await _run_count(chat) == before + 1
    parameters = await chat.env.database.workflow_runs.get_workflow_run_parameters(result.workflow_run_id)
    assert not ({parameter.key: value.value for parameter, value in parameters}.get("login") or "").strip()


@pytest.mark.parametrize(("supports_single_run_card", "submit_enabled"), [(True, True), (False, True), (True, False)])
def test_the_single_run_tool_is_offered_only_to_clients_that_render_its_card(
    monkeypatch: pytest.MonkeyPatch, supports_single_run_card: bool, submit_enabled: bool
) -> None:
    monkeypatch.setattr(settings, "COPILOT_ACCOUNT_GROUP_SUBMIT_ENABLED", submit_enabled)
    names = {
        tool.name
        for tool in copilot_native_tools(
            supports_question_tool=True,
            supports_single_run_card=supports_single_run_card,
            browser_code_available=False,
            run_tools_available=True,
        )
    }

    assert ("run_saved_workflow" in names) is (supports_single_run_card and submit_enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raised_with_run_row", "run_lookup_raised", "raised_before_run_row"])
async def test_a_single_run_whose_start_raises_keeps_its_link_only_if_a_run_may_exist(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def fail_after_the_run_row_exists(_: str) -> None:
        if failure == "run_lookup_raised":
            monkeypatch.setattr(
                app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(side_effect=RuntimeError("database down"))
            )
        raise RuntimeError("executor down")

    if failure == "raised_before_run_row":
        monkeypatch.setattr(
            app.WORKFLOW_SERVICE,
            "_refuse_unusable_request_parameters",
            AsyncMock(side_effect=SkyvernHTTPException("Credential not accessible", status_code=403)),
        )
    else:
        chat.env.executor.before_queue = fail_after_the_run_row_exists
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200

    result = await asyncio.wait_for(task, 5)

    restored = await _restored_review(chat)
    if failure == "raised_before_run_row":
        assert isinstance(result, AccountGroupRefusal)
        assert (result.status_code, result.dispatched) == (403, False)
        assert "workflow_run_id" not in restored["account_group_review"]
        assert await _run_count(chat) == before
    else:
        assert isinstance(result, SingleRunSubmission) and result.workflow_run_id is not None
        assert result.dispatched is None
        assert chat.env.executor.executed == [result.workflow_run_id]
        assert restored["account_group_review"]["workflow_run_id"] == result.workflow_run_id
        assert QuestionInteraction.model_validate(restored).tool_result()["dispatched"] is None


@pytest.mark.asyncio
# refused_then_turn_cleared: the turn row is written with the link, the refusal unlinks, and only then the turn clears.
@pytest.mark.parametrize("start", ["succeeds", "refused_before_run_row", "refused_then_turn_cleared"])
async def test_a_turn_that_ends_after_the_link_leaves_the_saved_card_true_to_the_run(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, start: str
) -> None:
    repo = chat.env.database.workflow_params
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None

    async def end_the_turn_from_a_snapshot_taken_before_the_link(*_: object, **__: object) -> None:
        snapshot = await _restored_review(chat)
        if start != "refused_then_turn_cleared":
            snapshot["account_group_review"].pop("workflow_run_id")
        await repo.create_workflow_copilot_chat_message(
            organization_id=RUN_GROUP_ORG,
            workflow_copilot_chat_id=chat_id,
            sender=WorkflowCopilotChatSender.AI,
            content="Stopped",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.RECOVER, copilot_turn_id="turn"),
            narrative_payload={
                "turnId": "turn",
                "turnIndex": 0,
                "questionInteractions": [snapshot],
                "designStarted": False,
                "designEnded": False,
                "draft": None,
                "blocks": [],
                "terminal": "stopped",
                "terminalMessage": None,
                "narrativeSummary": None,
                "priorBlockCount": None,
                "designActivity": [],
                "startedAt": None,
                "endedAt": None,
            },
        )
        if start != "refused_then_turn_cleared":
            await repo.clear_pending_copilot_turn(RUN_GROUP_ORG, chat_id, "turn")
        if start != "succeeds":
            raise SkyvernHTTPException("Credential not accessible", status_code=403)

    if start == "succeeds":
        chat.env.executor.before_queue = end_the_turn_from_a_snapshot_taken_before_the_link
    else:
        monkeypatch.setattr(
            app.WORKFLOW_SERVICE,
            "_refuse_unusable_request_parameters",
            end_the_turn_from_a_snapshot_taken_before_the_link,
        )
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)
    if start == "refused_then_turn_cleared":
        await repo.clear_pending_copilot_turn(RUN_GROUP_ORG, chat_id, "turn")

    restored = await _restored_review(chat)
    if start == "succeeds":
        assert isinstance(result, SingleRunSubmission) and result.workflow_run_id is not None
        assert restored["account_group_review"]["workflow_run_id"] == result.workflow_run_id
        assert QuestionInteraction.model_validate(restored).tool_result()["dispatched"] is not False
    else:
        assert isinstance(result, AccountGroupRefusal) and (result.status_code, result.dispatched) == (403, False)
        assert "workflow_run_id" not in restored["account_group_review"]
        assert await _run_count(chat) == before


@pytest.mark.asyncio
async def test_an_unfinished_run_of_a_version_holds_it_against_in_place_saves_from_any_chat(chat: Chat) -> None:
    async def holding(workflow_id: str) -> str | None:
        return await chat.env.database.workflow_runs.get_unfinished_run_id_on_version(
            RUN_GROUP_ORG, RUN_GROUP_WPID, workflow_id
        )

    assert await holding("wf_1") is None
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, SingleRunSubmission) and result.workflow_run_id is not None

    assert await holding("wf_1") == result.workflow_run_id
    assert await holding("wf_other") is None
    # A paused run resumes from the definition it already loaded, so it does not hold the row.
    for status, held in [(WorkflowRunStatus.paused, False), (WorkflowRunStatus.running, True)]:
        await chat.env.database.workflow_runs.update_workflow_run(workflow_run_id=result.workflow_run_id, status=status)
        assert (await holding("wf_1") is not None) is held
    await chat.env.database.workflow_runs.update_workflow_run(
        workflow_run_id=result.workflow_run_id, status=WorkflowRunStatus.completed
    )
    assert await holding("wf_1") is None
    # A run another chat, or the Run button, started holds the version too.
    await _seed_run(chat.env, status=WorkflowRunStatus.queued, group=False)
    assert await holding("wf_1") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["group", "single_run"])
@pytest.mark.parametrize("denied_at", ["proposal", "approval"])
@pytest.mark.parametrize("status_code", [403, 503])
async def test_a_requester_who_may_not_run_workflows_starts_nothing(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, tool: str, denied_at: str, status_code: int
) -> None:
    denied = denied_at == "proposal"

    async def enforce_request_role(_: RequestPrincipal, action: str | None, __: BearerIdentityStatus | None) -> None:
        if denied and action == "workflow.run":
            raise HTTPException(status_code=status_code, detail="Forbidden")

    monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_request_role", enforce_request_role)
    before = await _run_count(chat)
    task = _start(chat, ["cred_3"]) if tool == "group" else _start_single_run(chat, {"login": "cred_3"})
    if denied_at == "approval":
        card = await _card(chat)
        denied = True
        assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200

    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert ("permission to run workflows" in result.error) is (status_code == 403)
    if denied_at == "proposal":
        assert chat.frames.empty()
    assert await _run_count(chat) == before
    assert await _groups_from_this_chat(chat) == 0
