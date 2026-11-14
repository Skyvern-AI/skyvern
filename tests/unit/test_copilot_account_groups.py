import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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
from skyvern.forge.agent_functions import CopilotOrganizationUsage
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.ask_user import AccountGroupReview, QuestionInteraction
from skyvern.forge.sdk.copilot.browser_ablation import resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.canonical_ownership import workflow_content_fingerprint
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import account_groups, copilot_native_tools
from skyvern.forge.sdk.copilot.tools import credentials as credentials_module
from skyvern.forge.sdk.copilot.tools import mcp_hooks, workflow_update
from skyvern.forge.sdk.copilot.tools.account_groups import (
    ACCOUNT_GROUP_MAX_ITEMS,
    AccountGroupRefusal,
    AccountGroupState,
    AccountGroupSubmission,
    SingleRunSubmission,
    account_group_status,
    cancel_account_group,
    run_for_accounts,
    run_saved_workflow,
)
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    proposal_workflow_fingerprint,
    proposal_workflow_fingerprint_matches,
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
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotPendingTurn,
    WorkflowCopilotApplyProposedWorkflowRequest,
    WorkflowCopilotChat,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    copilot_proposal_metadata,
)
from skyvern.forge.sdk.services import request_principal
from skyvern.forge.sdk.services.request_principal import (
    AuthKind,
    BearerIdentityStatus,
    RequestPrincipal,
    scoped_api_key_id_from_actor,
)
from skyvern.forge.sdk.workflow.authoring_yaml import workflow_to_authoring_yaml
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.google_sheets_blocks import GoogleSheetsReadBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    AzureVaultCredentialParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    COPILOT_TEST_WORKFLOW_CREATOR,
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from skyvern.schemas.workflow_run_groups import (
    WorkflowRunGroupCreateRequest,
    WorkflowRunGroupItemOutcome,
    WorkflowRunGroupItemRequest,
    WorkflowRunGroupItemState,
    WorkflowRunGroupStatus,
)
from skyvern.services import workflow_run_group_service as group_service
from skyvern.utils.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from tests.unit.conftest import (
    RUN_GROUP_ORG,
    RUN_GROUP_WPID,
    GroupEnv,
    make_block_output_parameter,
    run_group_definition,
    run_group_task_block,
)
from tests.unit.copilot_test_helpers import make_copilot_ctx

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
    credits: tuple[int, int] = (0, 0),
    note: str | None = None,
) -> str:
    workflow = await env.database.workflows.get_workflow(workflow_id)
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow_id)
    login = next(parameter for parameter in parameters if parameter.key == "login")
    note_parameter = next(parameter for parameter in parameters if parameter.key == "note")
    workflow_run_id = f"wr_{uuid4().hex}"
    row_ids = [f"wrb_{uuid4().hex}" for _ in blocks]
    block_ids = {label: row_id for (label, _, _), row_id in zip(blocks, row_ids)}
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
                credits_used=credits[0],
                cached_credits_used=credits[1],
            )
        )
        session.add(
            WorkflowRunParameterModel(
                workflow_run_id=workflow_run_id,
                workflow_parameter_id=login.workflow_parameter_id,
                value=credential_id,
            )
        )
        if note is not None:
            session.add(
                WorkflowRunParameterModel(
                    workflow_run_id=workflow_run_id,
                    workflow_parameter_id=note_parameter.workflow_parameter_id,
                    value=note,
                )
            )
        session.add_all(
            WorkflowRunBlockModel(
                workflow_run_block_id=row_id,
                parent_workflow_run_block_id=block_ids[parent] if parent else None,
                workflow_run_id=workflow_run_id,
                organization_id=RUN_GROUP_ORG,
                block_type="task",
                status=block_status,
                label=label,
            )
            for (label, block_status, parent), row_id in zip(blocks, row_ids)
        )
        await session.commit()
    return workflow_run_id


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
        "over_limit": [f"cred_{i}" for i in range(ACCOUNT_GROUP_MAX_ITEMS + 1)],
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


async def _edit_in_place(chat: Chat, version: int = 1) -> None:
    async with chat.env.database.Session() as session:
        await session.execute(
            update(WorkflowModel)
            .where(WorkflowModel.workflow_permanent_id == RUN_GROUP_WPID)
            .where(WorkflowModel.version == version)
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
        stale: dict[str, Callable[[Chat], Awaitable[None]]] = {
            "new_version": _add_version,
            "edited_in_place": _edit_in_place,
            "param_retyped": _retype_param,
        }
        await stale[case](chat)
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


@pytest.mark.asyncio
async def test_a_common_input_cannot_bind_an_unapproved_connected_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_module, "connected_account_choices", AsyncMock(return_value=None))
    workflow = SimpleNamespace(
        workflow_definition={
            "parameters": [{"key": "note", "parameter_type": "workflow", "default_value": "none"}],
            "blocks": [
                {
                    "label": "read",
                    "block_type": "google_sheets_read",
                    "spreadsheet_url": "https://example.com",
                    "credential_id": "{{ note }}",
                }
            ],
        }
    )
    ctx = make_copilot_ctx(request_policy=RequestPolicy(run_approved_connection_ids=["goac_approved"]))

    with pytest.raises(account_groups.AccountGroupRefused):
        await account_groups._check_connected_accounts(ctx, workflow, {"note": "goac_unapproved"})
    await account_groups._check_connected_accounts(ctx, workflow, {"note": "goac_approved"})


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


async def _end_turn_into_history(chat: Chat, snapshot: dict[str, Any], *, clear_pending: bool = True) -> None:
    repo = chat.env.database.workflow_params
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
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
    if clear_pending:
        await repo.clear_pending_copilot_turn(RUN_GROUP_ORG, chat_id, "turn")


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
        await _end_turn_into_history(chat, snapshot, clear_pending=start != "refused_then_turn_cleared")
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


async def _stage_draft(
    chat: Chat,
    *,
    login_default: str | None = None,
    block_credential: str | None = None,
    connected_account: str | None = None,
    browser_profile_id: str | None = None,
    tested: bool = True,
    tested_run_id: str | None = "wr_tested",
) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None
    draft = saved.model_copy(deep=True)
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=saved.workflow_id)
    draft.workflow_definition.parameters = [
        parameter.model_copy(update={"default_value": login_default}) if parameter.key == "login" else parameter
        for parameter in parameters
    ]
    block = draft.workflow_definition.blocks[0]
    assert isinstance(block, TaskBlock)
    block.url = "https://example.com/statements"
    if browser_profile_id is not None:
        draft.browser_profile_id = browser_profile_id
    if connected_account is not None:
        draft.workflow_definition.blocks.append(
            GoogleSheetsReadBlock(
                label="read_sheet",
                output_parameter=block.output_parameter,
                spreadsheet_url="https://example.com/sheet",
                credential_id=connected_account,
            )
        )
    if block_credential is not None:
        draft.workflow_definition.parameters.append(
            CredentialParameter(
                key="portal",
                credential_parameter_id="cp_portal",
                workflow_id=saved.workflow_id,
                credential_id=block_credential,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            )
        )
    repo = chat.env.database.workflow_params
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    published = await repo.publish_workflow_copilot_candidate(
        RUN_GROUP_ORG,
        chat_id,
        proposal={**draft.model_dump(mode="json"), "_copilot_yaml": workflow_to_authoring_yaml(draft)},
        owner_turn_id="turn_draft",
        canonical_fingerprint=proposal_workflow_fingerprint(saved),
        canonical_title=saved.title,
        disposition="review_tested" if tested else "review_untested",
        expected_owner_turn_id=None,
        expected_revision=None,
    )
    if tested_run_id is None:
        return
    proposal = dict(published.proposed_workflow or {})
    proposal["_copilot_proposal"] = {**proposal["_copilot_proposal"], "workflow_run_id": tested_run_id}
    await repo.update_workflow_copilot_chat(RUN_GROUP_ORG, chat_id, proposed_workflow=proposal)


async def _versions(chat: Chat) -> list[int]:
    async with chat.env.database.Session() as session:
        rows = await session.scalars(
            select(WorkflowModel.version).where(WorkflowModel.workflow_permanent_id == RUN_GROUP_WPID)
        )
        return sorted(rows)


async def _chat_row(chat: Chat) -> WorkflowCopilotChat:
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    row = await chat.env.database.workflow_params.get_workflow_copilot_chat_by_id(RUN_GROUP_ORG, chat_id)
    assert row is not None
    return row


async def _saved_draft_update(chat: Chat) -> dict[str, Any]:
    while True:
        frame = await asyncio.wait_for(chat.frames.get(), 5)
        if frame["type"] == "question_resolved" and "saved_version" in frame["interaction"]["account_group_review"]:
            return frame["interaction"]["account_group_review"]


@pytest.mark.asyncio
async def test_approving_the_run_card_saves_the_draft_once_and_runs_that_version(chat: Chat) -> None:
    await _stage_draft(chat)
    # The turn read the draft's token, as a turn that picks up an earlier turn's draft does.
    chat.ctx.proposal_owner_turn_id, chat.ctx.proposal_revision = "turn_draft", 1
    auto_accept_before = (await _chat_row(chat)).auto_accept
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3", "note": "May"})
    card = await _card(chat)
    review = card["account_group_review"]
    assert review["saves_draft"] is True and review["draft_tested"] is True
    assert review["tested_workflow_run_id"] == "wr_tested"
    assert (review["proposal_owner_turn_id"], review["proposal_revision"]) == ("turn_draft", 1)
    assert await _versions(chat) == [1]

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, SingleRunSubmission) and result.dispatched is True and result.saved_version == 2
    assert await _versions(chat) == [1, 2]
    assert await _run_count(chat) == before + 1
    assert result.workflow_run_id is not None
    run = await chat.env.database.workflow_runs.get_workflow_run(result.workflow_run_id, organization_id=RUN_GROUP_ORG)
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert run is not None and saved is not None and run.workflow_id == saved.workflow_id
    block = saved.workflow_definition.blocks[0]
    assert isinstance(block, TaskBlock) and block.url == "https://example.com/statements"
    row = await _chat_row(chat)
    assert row.proposed_workflow is None and row.auto_accept == auto_accept_before
    restored = (await _restored_review(chat))["account_group_review"]
    assert (restored["saved_version"], restored["run_started"], restored["workflow_run_id"]) == (
        2,
        True,
        result.workflow_run_id,
    )


async def _url_of(chat: Chat, workflow_id: str) -> str | None:
    workflow = await chat.env.database.workflows.get_workflow(workflow_id, RUN_GROUP_ORG)
    assert workflow is not None
    block = workflow.workflow_definition.blocks[0]
    assert isinstance(block, TaskBlock)
    return block.url


async def _write_canonical_in_place(chat: Chat, workflow_id: str, url: str) -> None:
    workflow = await chat.env.database.workflows.get_workflow(workflow_id, RUN_GROUP_ORG)
    assert workflow is not None
    definition = workflow.workflow_definition.model_copy(deep=True)
    block = definition.blocks[0]
    assert isinstance(block, TaskBlock)
    block.url = url
    await app.WORKFLOW_SERVICE.update_workflow_definition(
        workflow_id=workflow_id, organization_id=RUN_GROUP_ORG, workflow_definition=definition
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_accept", [False, True], ids=["manual", "auto_accept"])
@pytest.mark.parametrize(
    "case",
    [
        "run_started",
        "run_finished",
        "dispatch_refused",
        "receipt_failed",
        "adoption_failed",
        "degraded_write",
        "degraded_write_interrupted",
    ],
)
async def test_an_edit_after_the_run_card_save_waits_for_accept_and_turn_end_keeps_the_saved_version(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, auto_accept: bool, case: str
) -> None:
    original = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert original is not None
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    await _stage_draft(chat)
    chat.ctx.proposal_owner_turn_id, chat.ctx.proposal_revision = "turn_draft", 1
    await chat.env.database.workflow_params.update_workflow_copilot_chat(
        RUN_GROUP_ORG, chat_id, auto_accept=auto_accept
    )
    if case == "dispatch_refused":
        monkeypatch.setattr(
            app.WORKFLOW_SERVICE,
            "_refuse_unusable_request_parameters",
            AsyncMock(side_effect=[None, SkyvernHTTPException("Credential not accessible", status_code=403)]),
        )
    elif case == "receipt_failed":
        monkeypatch.setattr(
            chat.env.database.workflow_params,
            "record_copilot_single_run_save",
            AsyncMock(side_effect=RuntimeError("down")),
        )
    elif case == "adoption_failed":
        monkeypatch.setattr(account_groups, "adopt_stored_workflow_candidate", AsyncMock(return_value="read_failed"))
    task = _start_single_run(chat, {"login": "cred_3"})
    assert await _decide(chat, await _card(chat), {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert isinstance(result, SingleRunSubmission) and saved is not None and result.saved_version == saved.version == 2
    assert result.dispatched is (case not in ("dispatch_refused", "receipt_failed"))
    if case == "run_finished":
        async with chat.env.database.Session() as session:
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_run_id == result.workflow_run_id)
                .values(status=WorkflowRunStatus.completed)
            )
            await session.commit()

    chat.ctx.proposal_adoptions_seen_by_model = chat.ctx.proposal_adoptions
    edited = await workflow_update._update_workflow(
        {
            "workflow_yaml": chat.ctx.workflow_yaml.replace(
                "https://example.com/statements", "https://example.com/invoices"
            )
        },
        chat.ctx,
    )
    assert edited["ok"], edited
    workflow_update._record_workflow_update_result(chat.ctx, edited)
    if case.startswith("degraded_write"):
        await _write_canonical_in_place(chat, saved.workflow_id, "https://example.com/degraded")
        chat.ctx.workflow_persisted = True
    if case == "degraded_write_interrupted":
        current = await chat.env.database.workflows.get_workflow(saved.workflow_id, RUN_GROUP_ORG)
        assert current is not None
        await chat.env.database.workflow_params.record_pending_copilot_turn_canonical_write(
            RUN_GROUP_ORG, chat_id, "turn", workflow_content_fingerprint(current.model_dump(mode="json"))
        )
        row = await _chat_row(chat)
        await routes._recover_interrupted_copilot_turn(row, RUN_GROUP_ORG, row.pending_turns["turn"])
    else:
        turn_end = agent_module._make_agent_result(
            chat.ctx,
            user_response="Edited the statement URL.",
            updated_workflow=chat.ctx.last_workflow,
            workflow_yaml=chat.ctx.last_workflow_yaml,
            workflow_was_persisted=chat.ctx.workflow_persisted,
            has_staged_proposal=chat.ctx.has_staged_proposal,
            staged_workflow_yaml=chat.ctx.staged_workflow_yaml,
            staged_workflow=chat.ctx.staged_workflow,
            proposal_disposition="auto_applicable" if auto_accept else "review_untested",
            turn_id="turn",
            narrative_payload=agent_module._build_narrative_payload(
                chat.ctx, terminal="response", terminal_message="Edited the statement URL.", narrative_summary=None
            ),
        )
        await routes._finalise_normal_turn(
            stream=SimpleNamespace(send=AsyncMock(return_value=True), is_disconnected=AsyncMock(return_value=False)),
            chat=await _chat_row(chat),
            organization_id=RUN_GROUP_ORG,
            original_workflow=original,
            chat_request=WorkflowCopilotChatRequest(
                workflow_id=original.workflow_id,
                workflow_permanent_id=RUN_GROUP_WPID,
                workflow_copilot_chat_id=chat_id,
                message="Run it now",
                workflow_yaml="",
            ),
            agent_result=turn_end,
            turn_id="turn",
        )

    assert await _versions(chat) == [1, 2]
    latest = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert latest is not None and latest.workflow_id == saved.workflow_id
    assert await _url_of(chat, saved.workflow_id) == "https://example.com/statements"
    untouched = await chat.env.database.workflows.get_workflow(original.workflow_id, RUN_GROUP_ORG)
    assert untouched is not None and untouched.modified_at == original.modified_at
    original_block = original.workflow_definition.blocks[0]
    assert isinstance(original_block, TaskBlock) and await _url_of(chat, original.workflow_id) == original_block.url
    proposal = (await _chat_row(chat)).proposed_workflow
    metadata = copilot_proposal_metadata(proposal)
    assert proposal is not None and metadata is not None and metadata.owner_turn_id == "turn"
    draft_block = Workflow.model_validate(proposal).workflow_definition.blocks[0]
    assert isinstance(draft_block, TaskBlock) and draft_block.url == "https://example.com/invoices"
    # A rollback rewrites the row with explicit empty browser settings, so only an untouched row keeps its fingerprint.
    if not case.startswith("degraded_write"):
        assert proposal_workflow_fingerprint_matches(latest, metadata.canonical_fingerprint)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["parameter_default", "fixed_credential", "connected_account", "browser_profile", "non_string_credential"],
)
async def test_a_draft_that_needs_accept_shows_no_card_and_saves_nothing(chat: Chat, change: str) -> None:
    await _stage_draft(
        chat,
        login_default="cred_3" if change == "parameter_default" else None,
        block_credential="cred_2" if change == "fixed_credential" else None,
        connected_account="goac_other" if change == "connected_account" else None,
        browser_profile_id="bp_other" if change == "browser_profile" else None,
    )
    inputs: dict[str, JsonValue] = {"login": 123} if change == "non_string_credential" else {}

    result = await asyncio.wait_for(_start_single_run(chat, inputs), 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    if change != "non_string_credential":
        assert "Nothing was started" in result.error and "accepting it" in result.error
    assert chat.frames.empty()
    assert await _versions(chat) == [1]
    assert copilot_proposal_metadata((await _chat_row(chat)).proposed_workflow) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft_primary", "draft_fallbacks"),
    [("cred_b", ["cred_a"]), ("cred_a", []), ("cred_a", ["cred_b", "cred_c"])],
)
async def test_reordering_or_moving_a_fallback_login_counts_as_a_binding_change(
    chat: Chat, draft_primary: str, draft_fallbacks: list[str]
) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None

    def with_portal(primary: str, fallbacks: list[str]) -> Workflow:
        workflow = saved.model_copy(deep=True)
        workflow.workflow_definition.parameters.append(
            CredentialParameter(
                key="portal",
                credential_parameter_id="cp_portal",
                workflow_id=saved.workflow_id,
                credential_id=primary,
                fallback_credential_ids=fallbacks,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            )
        )
        return workflow

    base = account_groups._credential_bindings(with_portal("cred_a", ["cred_b"]))

    assert base == account_groups._credential_bindings(with_portal("cred_a", ["cred_b"]))
    assert base != account_groups._credential_bindings(with_portal(draft_primary, draft_fallbacks))


@pytest.mark.asyncio
async def test_changing_the_default_a_connected_account_field_reads_counts_as_a_binding_change(chat: Chat) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None
    block = saved.workflow_definition.blocks[0]
    assert isinstance(block, TaskBlock)

    def with_account_default(default: str) -> Workflow:
        workflow = saved.model_copy(deep=True)
        workflow.workflow_definition.parameters.append(
            WorkflowParameter(
                key="account",
                workflow_parameter_id="wp_account",
                workflow_parameter_type=WorkflowParameterType.STRING,
                workflow_id=saved.workflow_id,
                default_value=default,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            )
        )
        workflow.workflow_definition.blocks.append(
            GoogleSheetsReadBlock(
                label="read_sheet",
                output_parameter=block.output_parameter,
                spreadsheet_url="https://example.com/sheet",
                credential_id="{{ account }}",
            )
        )
        return workflow

    base = account_groups._credential_bindings(with_account_default("goac_a"))

    assert base == account_groups._credential_bindings(with_account_default("goac_a"))
    assert base != account_groups._credential_bindings(with_account_default("goac_b"))


@pytest.mark.asyncio
async def test_changing_the_default_a_credential_parameter_names_counts_as_a_binding_change(chat: Chat) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None

    def with_login_default(default: str, *, indirect: bool = True) -> Workflow:
        workflow = saved.model_copy(deep=True)
        workflow.workflow_definition.parameters += [
            WorkflowParameter(
                key="account_param",
                workflow_parameter_id="wp_account",
                workflow_parameter_type=WorkflowParameterType.STRING,
                workflow_id=saved.workflow_id,
                default_value=default,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            ),
            CredentialParameter(
                key="portal",
                credential_parameter_id="cp_portal",
                workflow_id=saved.workflow_id,
                credential_id="account_param" if indirect else default,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            ),
        ]
        return workflow

    base = account_groups._credential_bindings(with_login_default("cred_a"))

    assert base != account_groups._credential_bindings(with_login_default("cred_b"))
    assert base == account_groups._credential_bindings(with_login_default("cred_a", indirect=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("vault", ["onepassword", "azure"])
async def test_changing_a_vault_login_parameter_counts_as_a_binding_change(chat: Chat, vault: str) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None

    def with_vault_login(item: str, *, parameter_id: str = "vp_1") -> Workflow:
        workflow = saved.model_copy(deep=True)
        common = {"workflow_id": saved.workflow_id, "created_at": saved.created_at, "modified_at": saved.modified_at}
        workflow.workflow_definition.parameters.append(
            OnePasswordCredentialParameter(
                key="vault_login",
                onepassword_credential_parameter_id=parameter_id,
                vault_id="v",
                item_id=item,
                **common,
            )
            if vault == "onepassword"
            else AzureVaultCredentialParameter(
                key="vault_login",
                azure_vault_credential_parameter_id=parameter_id,
                vault_name="v",
                username_key=item,
                password_key="pw",
                **common,
            )
        )
        return workflow

    base = account_groups._credential_bindings(with_vault_login("item_a"))

    assert base == account_groups._credential_bindings(with_vault_login("item_a", parameter_id="vp_regenerated"))
    assert base != account_groups._credential_bindings(with_vault_login("item_b"))

    def with_templated_vault_login(default: str) -> Workflow:
        workflow = with_vault_login("{{ account }}")
        workflow.workflow_definition.parameters.append(
            WorkflowParameter(
                key="account",
                workflow_parameter_id="wp_account",
                workflow_parameter_type=WorkflowParameterType.STRING,
                workflow_id=saved.workflow_id,
                default_value=default,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            )
        )
        return workflow

    templated = account_groups._credential_bindings(with_templated_vault_login("item_a"))

    assert templated == account_groups._credential_bindings(with_templated_vault_login("item_a"))
    assert templated != account_groups._credential_bindings(with_templated_vault_login("item_b"))


@pytest.mark.asyncio
@pytest.mark.parametrize("input_key", ["profile_segment", "账户"])
async def test_changing_the_default_a_browser_profile_key_reads_counts_as_a_binding_change(
    chat: Chat, input_key: str
) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None

    def with_profile_segment(default: str) -> Workflow:
        workflow = saved.model_copy(deep=True, update={"browser_profile_key": f"{{{{ {input_key} }}}}"})
        workflow.workflow_definition.parameters.append(
            WorkflowParameter(
                key=input_key,
                workflow_parameter_id="wp_segment",
                workflow_parameter_type=WorkflowParameterType.STRING,
                workflow_id=saved.workflow_id,
                default_value=default,
                created_at=saved.created_at,
                modified_at=saved.modified_at,
            )
        )
        return workflow

    base = account_groups._credential_bindings(with_profile_segment("team_a"))

    assert base == account_groups._credential_bindings(with_profile_segment("team_a"))
    assert base != account_groups._credential_bindings(with_profile_segment("team_b"))


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["persist_browser_session", "reuse_browser_session"])
async def test_turning_on_browser_memory_counts_as_a_binding_change(chat: Chat, switch: str) -> None:
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None
    assert not saved.persist_browser_session and not saved.reuse_browser_session
    draft = saved.model_copy(deep=True, update={switch: True})

    assert account_groups._credential_bindings(saved) != account_groups._credential_bindings(draft)


async def _accept(chat: Chat, revision: int = 1) -> None:
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    with _as_requester(ORG_KEY):
        await routes.workflow_copilot_apply_proposed_workflow(
            WorkflowCopilotApplyProposedWorkflowRequest(
                workflow_copilot_chat_id=chat_id, owner_turn_id="turn_draft", revision=revision
            ),
            chat.env.organization,
        )


async def _restage(chat: Chat) -> None:
    row = await _chat_row(chat)
    metadata = copilot_proposal_metadata(row.proposed_workflow)
    assert metadata is not None and row.proposed_workflow is not None
    await chat.env.database.workflow_params.publish_workflow_copilot_candidate(
        RUN_GROUP_ORG,
        row.workflow_copilot_chat_id,
        proposal={key: value for key, value in row.proposed_workflow.items() if key != "_copilot_proposal"},
        owner_turn_id="turn_draft",
        canonical_fingerprint=metadata.canonical_fingerprint,
        canonical_title=metadata.canonical_title,
        disposition="review_untested",
        expected_owner_turn_id="turn_draft",
        expected_revision=1,
    )


async def _run_of(chat: Chat, result: SingleRunSubmission | AccountGroupRefusal) -> tuple[str, str, int]:
    assert isinstance(result, SingleRunSubmission) and result.dispatched is True and result.workflow_run_id
    run = await chat.env.database.workflow_runs.get_workflow_run(result.workflow_run_id, organization_id=RUN_GROUP_ORG)
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert run is not None and saved is not None
    return run.workflow_id, saved.workflow_id, saved.version


@pytest.mark.asyncio
async def test_a_login_change_saved_by_accept_then_runs_on_the_accepted_version(chat: Chat) -> None:
    await _stage_draft(chat, login_default="cred_3")
    refused = await asyncio.wait_for(_start_single_run(chat, {"login": "cred_3"}), 5)
    assert isinstance(refused, AccountGroupRefusal) and await _versions(chat) == [1]
    before = await _run_count(chat)

    await _accept(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert "saves_draft" not in card["account_group_review"]
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    run_workflow_id, saved_workflow_id, version = await _run_of(chat, result)
    assert (run_workflow_id, version) == (saved_workflow_id, 2)
    assert await _versions(chat) == [1, 2] and await _run_count(chat) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["while_pending", "after_approval", "after_a_declined_card"])
async def test_accept_while_the_draft_run_card_waits_runs_the_accepted_version_without_another_save(
    chat: Chat, case: str
) -> None:
    await _stage_draft(chat)
    before = await _run_count(chat)
    if case == "after_a_declined_card":
        declined = _start_single_run(chat, {"login": "cred_3"})
        assert await _decide(chat, await _card(chat), {"approved": False}) == 200
        await asyncio.wait_for(declined, 5)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    approval = {"approved": True, "credential_ids": ["cred_3"]}
    if case == "after_approval":
        # The approved card's dispatch waits on this lock, so Accept lands between the approval and the save.
        async with chat.ctx.proposal_mutation_lock:
            assert await _decide(chat, card, approval) == 200
            await _accept(chat)
    else:
        await _accept(chat)
        assert await _decide(chat, card, approval) == 200

    result = await asyncio.wait_for(task, 5)

    run_workflow_id, saved_workflow_id, version = await _run_of(chat, result)
    assert (run_workflow_id, version) == (saved_workflow_id, 2)
    assert isinstance(result, SingleRunSubmission) and result.saved_version == 2
    assert await _versions(chat) == [1, 2] and await _run_count(chat) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "restaged_then_accepted",
        "edited_in_place_after_accept",
        "rewritten_before_receipt",
        "another_chat_saved",
        "receipt_not_written",
    ],
)
async def test_accept_of_anything_but_the_reviewed_draft_refuses_the_waiting_run_card(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await _stage_draft(chat)
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    if case == "restaged_then_accepted":
        await _restage(chat)
        await _accept(chat, revision=2)
    else:
        if case == "receipt_not_written":
            monkeypatch.setattr(
                chat.env.database.workflow_params,
                "record_copilot_draft_acceptance",
                AsyncMock(side_effect=RuntimeError("down")),
            )
        elif case == "rewritten_before_receipt":
            clear_candidate = chat.env.database.workflow_params.clear_workflow_copilot_candidate

            async def clear_then_rewrite(*args: Any, **kwargs: Any) -> Any:
                cleared = await clear_candidate(*args, **kwargs)
                latest = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
                assert latest is not None
                await _write_canonical_in_place(chat, latest.workflow_id, "https://example.com/other-chat")
                return cleared

            monkeypatch.setattr(
                chat.env.database.workflow_params, "clear_workflow_copilot_candidate", clear_then_rewrite
            )
        await _accept(chat)
        if case == "edited_in_place_after_accept":
            await _edit_in_place(chat, version=2)
        elif case == "another_chat_saved":
            await _add_version(chat, "wf_3", 3)
    versions = await _versions(chat)

    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, AccountGroupRefusal) and result.dispatched is False
    assert await _versions(chat) == versions and await _run_count(chat) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "decline",
        "update_role_denied",
        "run_role_denied",
        "folder_denied",
        "restaged_while_waiting",
        "rejected_while_waiting",
    ],
)
async def test_a_declined_or_stale_draft_run_card_saves_and_starts_nothing(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    await _stage_draft(chat)
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    decision: dict[str, Any] = {"approved": True, "credential_ids": ["cred_3"]}
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    if case == "decline":
        decision = {"approved": False}
    elif case == "update_role_denied":

        async def deny_update(_: RequestPrincipal, action: str | None, __: BearerIdentityStatus | None) -> None:
            if action == "workflow.update":
                raise HTTPException(status_code=403, detail="Forbidden")

        monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_request_role", deny_update)
    elif case == "folder_denied":
        monkeypatch.setattr(
            app.AGENT_FUNCTION, "enforce_folder_access", AsyncMock(side_effect=HTTPException(status_code=403))
        )
    elif case == "run_role_denied":

        async def deny_run(_: RequestPrincipal, action: str | None, __: BearerIdentityStatus | None) -> None:
            if action == "workflow.run":
                raise HTTPException(status_code=403, detail="Forbidden")

        monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_request_role", deny_run)
    elif case == "restaged_while_waiting":
        await _restage(chat)
    elif case == "rejected_while_waiting":
        await chat.env.database.workflow_params.clear_workflow_copilot_candidate(
            organization_id=RUN_GROUP_ORG,
            workflow_copilot_chat_id=chat_id,
            expected_owner_turn_id="turn_draft",
            expected_revision=1,
        )
    versions_before_decision = await _versions(chat)

    assert await _decide(chat, card, decision) == 200
    result = await asyncio.wait_for(task, 5)

    assert result.dispatched is False
    assert await _versions(chat) == versions_before_decision
    assert await _run_count(chat) == before
    if case in ("decline", "update_role_denied", "run_role_denied", "folder_denied"):
        assert copilot_proposal_metadata((await _chat_row(chat)).proposed_workflow) is not None
    if case in ("update_role_denied", "folder_denied"):
        assert isinstance(result, AccountGroupRefusal) and "permission to save" in result.error
    if case == "run_role_denied":
        assert isinstance(result, AccountGroupRefusal) and "permission to run workflows" in result.error
    if case == "rejected_while_waiting":
        assert isinstance(result, AccountGroupRefusal) and "rejected" in result.error
        assert "updated after the review" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["audit", "receipt", "start_refused", "start_raised", "start_unconfirmed", "rewritten_in_place"]
)
async def test_a_draft_saved_from_the_run_card_reports_its_version_when_the_run_does_not_start(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    await _stage_draft(chat)
    if failure == "start_raised":
        monkeypatch.setattr(
            account_groups.workflow_service, "run_workflow", AsyncMock(side_effect=RuntimeError("service down"))
        )
    elif failure == "rewritten_in_place":
        record_save = chat.env.database.workflow_params.record_copilot_single_run_save

        async def record_then_rewrite(*args: Any, **kwargs: Any) -> Any:
            recorded = await record_save(*args, **kwargs)
            await _write_canonical_in_place(chat, kwargs["saved_workflow_id"], "https://example.com/other-chat")
            return recorded

        monkeypatch.setattr(chat.env.database.workflow_params, "record_copilot_single_run_save", record_then_rewrite)
    elif failure == "audit":
        monkeypatch.setattr(app.AGENT_FUNCTION, "record_audit_event", AsyncMock(side_effect=RuntimeError("down")))
    elif failure == "receipt":
        monkeypatch.setattr(
            chat.env.database.workflow_params,
            "record_copilot_single_run_save",
            AsyncMock(side_effect=RuntimeError("down")),
        )
    elif failure == "start_refused":
        monkeypatch.setattr(
            app.WORKFLOW_SERVICE,
            "_refuse_unusable_request_parameters",
            AsyncMock(side_effect=[None, SkyvernHTTPException("Credential not accessible", status_code=403)]),
        )
    else:

        async def executor_down(_: str) -> None:
            raise RuntimeError("executor down")

        chat.env.executor.before_queue = executor_down
    before = await _run_count(chat)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200

    result = await asyncio.wait_for(task, 5)

    assert isinstance(result, SingleRunSubmission) and result.saved_version == 2
    assert result.error is not None and "version 2" in result.error
    assert await _versions(chat) == [1, 2]
    if failure == "start_unconfirmed":
        assert result.dispatched is None and await _run_count(chat) == before + 1
    else:
        assert result.dispatched is False and await _run_count(chat) == before
    resolved = await _saved_draft_update(chat)
    assert resolved["saved_version"] == 2 and "run_started" not in resolved


@pytest.mark.asyncio
async def test_a_draft_run_card_cancelled_after_approval_still_reports_and_saves_once(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stage_draft(chat)
    accept = account_groups.accept_copilot_proposal
    tasks: list[asyncio.Task[SingleRunSubmission | AccountGroupRefusal]] = []

    async def accept_then_cancel_the_tool_call(*args: Any, **kwargs: Any) -> tuple[Workflow, tuple[str, ...]]:
        saved = await accept(*args, **kwargs)
        tasks[0].cancel()
        return saved

    monkeypatch.setattr(account_groups, "accept_copilot_proposal", accept_then_cancel_the_tool_call)
    tasks.append(_start_single_run(chat, {"login": "cred_3"}))
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]

    resolved = await _saved_draft_update(chat)

    assert resolved["saved_version"] == 2 and resolved["run_started"] is True
    retry = _start_single_run(chat, {"login": "cred_3"})
    retry_card = await _card(chat)
    assert "saves_draft" not in retry_card["account_group_review"]
    assert await _decide(chat, retry_card, {"approved": False}) == 200
    await asyncio.wait_for(retry, 5)
    assert await _versions(chat) == [1, 2]


@pytest.mark.asyncio
async def test_a_draft_saved_after_the_turn_ended_is_recorded_in_the_saved_history(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _stage_draft(chat)
    accept = account_groups.accept_copilot_proposal

    async def accept_then_end_the_turn(*args: Any, **kwargs: Any) -> tuple[Workflow, tuple[str, ...]]:
        saved = await accept(*args, **kwargs)
        await _end_turn_into_history(chat, await _restored_review(chat))
        return saved

    monkeypatch.setattr(account_groups, "accept_copilot_proposal", accept_then_end_the_turn)
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    await asyncio.wait_for(task, 5)

    assert await _versions(chat) == [1, 2]
    assert (await _restored_review(chat))["account_group_review"]["saved_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tested", [True, False])
async def test_the_run_card_says_tested_from_the_proposal_disposition_without_a_recorded_run(
    chat: Chat, tested: bool
) -> None:
    await _stage_draft(chat, tested=tested, tested_run_id=None)
    chat.ctx.proposal_owner_turn_id, chat.ctx.proposal_revision = "turn_draft", 1
    task = _start_single_run(chat, {"login": "cred_3"})
    card = await _card(chat)
    review = card["account_group_review"]

    assert review["saves_draft"] is True
    assert review.get("draft_tested", False) is tested
    assert "tested_workflow_run_id" not in review
    assert await _decide(chat, card, {"approved": False}) == 200
    await asyncio.wait_for(task, 5)
    assert await _versions(chat) == [1]


@pytest.mark.asyncio
async def test_group_schedule_and_run_refusals_state_the_same_fact_about_an_unsaved_change(chat: Chat) -> None:
    chat.ctx.has_staged_proposal = True
    run = await asyncio.wait_for(_start_single_run(chat, {"login": "cred_3"}), 5)
    group = await asyncio.wait_for(_start(chat, ["cred_1"]), 5)
    schedule = await mcp_hooks._create_workflow_schedule_pre_hook({}, chat.ctx)

    assert isinstance(run, AccountGroupRefusal) and isinstance(group, AccountGroupRefusal) and schedule is not None
    for error in (run.error, group.error, schedule["error"]):
        assert error.endswith(mcp_hooks.PENDING_PROPOSAL_FACT)
        assert "auto-accept" not in error


STOP_RULE = "Each run checks this account's credits before it starts, and its steps check again while it runs."


def _metered(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = CopilotOrganizationUsage(plan_tier="pro", remaining_credits=500)
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_organization_usage_quota", AsyncMock(return_value=usage))
    monkeypatch.setattr(app.AGENT_FUNCTION, "copilot_run_stop_rule", AsyncMock(return_value=STOP_RULE))


async def _save_blocks_with_finally(chat: Chat) -> None:
    confirm, wrapup = (
        TaskBlock(label=label, url="https://example.com", output_parameter=make_block_output_parameter(label))
        for label in ("confirm", "wrapup")
    )
    definition = run_group_definition(run_group_task_block(), confirm, wrapup, finally_block_label="wrapup")
    async with chat.env.database.Session() as session:
        await session.execute(
            update(WorkflowModel).where(WorkflowModel.workflow_id == "wf_1").values(workflow_definition=definition)
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocks", "completed"),
    [
        ((("login", "completed", None), ("confirm", "completed", None), ("wrapup", "failed", None)), 2),
        ((("login", "completed", None),), 1),
        ((("login", "completed", None), ("login", "completed", None)), 1),
    ],
    ids=["whole_workflow", "partial", "retried_block"],
)
async def test_the_run_card_and_its_result_report_the_cost_of_the_versions_latest_completed_run(
    chat: Chat,
    monkeypatch: pytest.MonkeyPatch,
    blocks: tuple[tuple[str, str, str | None], ...],
    completed: int,
) -> None:
    _metered(monkeypatch)
    register_secret_scrub_value(chat.ctx, SECRET)
    await _save_blocks_with_finally(chat)
    run_id = await _seed_run(chat.env, blocks=blocks, group=False, credits=(30, 12), note=f"ref {SECRET}")

    task = _start_single_run(chat, {"note": "May", "login": "cred_3"})
    card = await _card(chat)
    review = card["account_group_review"]
    cost = review["cost"]

    assert cost == {
        "runs_to_start": 1,
        "based_on_run_id": run_id,
        "based_on_credits": 42,
        "blocks_completed": completed,
        "blocks_total": 2,
        "balance_credits": 500,
        "stop_rule": STOP_RULE,
    } | ({"estimate_from_one_run": 42} if completed == 2 else {})
    assert review["based_on_inputs"] == {
        "login": REDACTED_SECRET_PLACEHOLDER,
        "note": f"ref {REDACTED_SECRET_PLACEHOLDER}",
    }
    assert "cred_1" not in to_json(review).decode() and SECRET not in to_json(review).decode()
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_3"]}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, SingleRunSubmission) and result.dispatched
    assert result.cost is not None and result.cost.model_dump(mode="json") == cost
    assert "based_on_inputs" not in result.model_dump_json() and "ref " not in result.model_dump_json()
    assert (await _restored_review(chat))["account_group_review"]["cost"] == cost


@pytest.mark.asyncio
@pytest.mark.parametrize("quota", ["note", "unreadable"])
async def test_an_account_whose_credits_are_not_tracked_sees_no_credit_numbers(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, quota: str
) -> None:
    run_id = await _seed_run(chat.env, group=False, credits=(30, 12))
    note = "Custom enterprise billing, so credit counters do not apply."
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "get_organization_usage_quota",
        AsyncMock(return_value=CopilotOrganizationUsage(remaining_credits=0, credits_note=note))
        if quota == "note"
        else AsyncMock(side_effect=RuntimeError("billing read failed")),
    )

    task = _start(chat, ["cred_2", "cred_3"])
    card = await _card(chat)
    cost = card["account_group_review"]["cost"]

    assert cost == {
        "based_on_run_id": run_id,
        "credits_note": note if quota == "note" else account_groups._QUOTA_UNREADABLE_NOTE,
    }
    assert card["account_group_review"]["based_on_inputs"] == {"login": REDACTED_SECRET_PLACEHOLDER}
    assert await _decide(chat, card, {"approved": True, "credential_ids": ["cred_2"]}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, AccountGroupSubmission) and result.cost is not None
    assert result.cost.model_dump(mode="json") == cost
    assert "based_on_inputs" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_a_draft_card_reports_its_tested_run_with_no_estimate_when_that_run_failed(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    _metered(monkeypatch)
    run_id = await _seed_run(chat.env, group=False, status=WorkflowRunStatus.failed, credits=(5, 0))
    await _stage_draft(chat, tested_run_id=run_id)
    chat.ctx.proposal_owner_turn_id, chat.ctx.proposal_revision = "turn_draft", 1

    task = _start_single_run(chat, {"login": "cred_1"})
    card = await _card(chat)
    cost = card["account_group_review"]["cost"]

    assert cost == {
        "runs_to_start": 1,
        "based_on_run_id": run_id,
        "based_on_credits": 5,
        "blocks_completed": 1,
        "blocks_total": 1,
        "balance_credits": 500,
        "stop_rule": STOP_RULE,
    }
    assert await _decide(chat, card, {"approved": False}) == 200
    result = await asyncio.wait_for(task, 5)
    assert isinstance(result, SingleRunSubmission) and result.cost is not None
    assert result.cost.model_dump(mode="json") == {
        key: value for key, value in cost.items() if key not in ("runs_to_start", "estimate_from_one_run")
    }


@pytest.mark.asyncio
async def test_the_group_card_carries_the_single_run_facts_and_its_result_counts_the_approved_runs(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    _metered(monkeypatch)
    await _seed_run(chat.env, group=False, credits=(30, 12))
    single = _start_single_run(chat, {"login": "cred_1"})
    single_card = await _card(chat)
    assert await _decide(chat, single_card, {"approved": False}) == 200
    await asyncio.wait_for(single, 5)

    group = _start(chat, ["cred_1", "cred_2", "cred_3"])
    group_card = await _card(chat)
    group_cost = group_card["account_group_review"]["cost"]

    assert group_cost == single_card["account_group_review"]["cost"] | {
        "runs_to_start": 3,
        "estimate_from_one_run": 126,
    }
    assert await _decide(chat, group_card, {"approved": True, "credential_ids": ["cred_2"]}) == 200
    result = await asyncio.wait_for(group, 5)
    assert isinstance(result, AccountGroupSubmission) and result.cost is not None
    assert (result.cost.runs_to_start, result.cost.estimate_from_one_run) == (1, 42)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["untested_draft", "saved_version_never_completed_since_its_last_write"])
async def test_a_card_with_no_qualifying_run_shows_no_run_blocks_or_estimate(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _metered(monkeypatch)
    await _seed_run(chat.env, group=False, credits=(30, 12))
    if case == "untested_draft":
        await _stage_draft(chat, tested=False, tested_run_id=None)
        chat.ctx.proposal_owner_turn_id, chat.ctx.proposal_revision = "turn_draft", 1
    else:
        async with chat.env.database.Session() as session:
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_permanent_id == RUN_GROUP_WPID)
                .values(created_at=datetime(2000, 1, 1))
            )
            await session.commit()

    task = _start_single_run(chat, {"login": "cred_1"})
    card = await _card(chat)

    assert card["account_group_review"]["cost"] == {
        "runs_to_start": 1,
        "balance_credits": 500,
        "stop_rule": STOP_RULE,
    }
    assert "based_on_inputs" not in card["account_group_review"]
    assert await _decide(chat, card, {"approved": False}) == 200
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_a_copilot_test_run_of_the_saved_version_is_cited_as_its_based_on_run(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    _metered(monkeypatch)
    run_id = await _seed_run(chat.env, group=False, credits=(30, 12))
    async with chat.env.database.Session() as session:
        await session.execute(
            update(WorkflowRunModel)
            .where(WorkflowRunModel.workflow_run_id == run_id)
            .values(copilot_session_id=chat.ctx.workflow_copilot_chat_id)
        )
        await session.commit()

    task = _start_single_run(chat, {"login": "cred_1"})
    card = await _card(chat)

    cost = card["account_group_review"]["cost"]
    assert cost["based_on_run_id"] == run_id
    assert cost["blocks_completed"] == cost["blocks_total"]
    assert cost["estimate_from_one_run"] == 42
    assert await _decide(chat, card, {"approved": False}) == 200
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tested", "edited_after_accept", "untested", "newer_real_run", "receipt_unreadable"])
async def test_after_accept_the_saved_version_cards_cite_the_run_that_tested_the_accepted_draft(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _metered(monkeypatch)
    tested_id = await _seed_run(chat.env, group=False, credits=(7, 0))
    await _stage_draft(chat, tested=case != "untested", tested_run_id=tested_id)
    await _accept(chat)
    saved = await chat.env.database.workflows.get_workflow_by_permanent_id(RUN_GROUP_WPID, RUN_GROUP_ORG)
    assert saved is not None and saved.version == 2
    receipt = (await _chat_row(chat)).accepted_tested_run
    assert receipt is not None
    assert (receipt.workflow_id, receipt.modified_at) == (saved.workflow_id, saved.modified_at)
    assert receipt.workflow_run_id == (None if case == "untested" else tested_id)
    expected: str | None = tested_id if case == "tested" else None
    if case == "edited_after_accept":
        await _edit_in_place(chat, version=2)
    elif case in ("newer_real_run", "receipt_unreadable"):
        expected = await _seed_run(chat.env, workflow_id=saved.workflow_id, group=False, credits=(30, 12))
    if case == "receipt_unreadable":
        get_run = chat.env.database.workflow_runs.get_workflow_run

        async def unreadable_receipt_run(workflow_run_id: str, *args: Any, **kwargs: Any) -> WorkflowRun | None:
            if workflow_run_id == tested_id:
                raise RuntimeError("down")
            return await get_run(workflow_run_id, *args, **kwargs)

        monkeypatch.setattr(chat.env.database.workflow_runs, "get_workflow_run", unreadable_receipt_run)

    single = _start_single_run(chat, {"login": "cred_1"})
    single_card = await _card(chat)
    assert await _decide(chat, single_card, {"approved": False}) == 200
    await asyncio.wait_for(single, 5)
    group = _start(chat, ["cred_1", "cred_2"])
    group_card = await _card(chat)
    assert await _decide(chat, group_card, {"approved": False}) == 200
    await asyncio.wait_for(group, 5)

    for card in (single_card, group_card):
        assert card["account_group_review"]["cost"].get("based_on_run_id") == expected


@pytest.mark.asyncio
async def test_an_older_accept_does_not_replace_the_tested_run_receipt_of_a_newer_one(chat: Chat) -> None:
    repo = chat.env.database.workflow_params
    chat_id = chat.ctx.workflow_copilot_chat_id
    assert chat_id is not None
    newer = datetime(2026, 1, 2)

    async def accept(modified_at: datetime, run_id: str | None) -> None:
        await repo.record_copilot_draft_acceptance(
            RUN_GROUP_ORG,
            chat_id,
            owner_turn_id="turn_draft",
            revision=1,
            workflow_id="wf_1",
            version=1,
            modified_at=modified_at,
            tested_workflow_run_id=run_id,
        )

    await accept(newer, "wr_newer")
    await accept(datetime(2026, 1, 1), "wr_older")
    receipt = (await _chat_row(chat)).accepted_tested_run
    assert receipt is not None and (receipt.modified_at, receipt.workflow_run_id) == (newer, "wr_newer")

    await accept(newer, None)
    receipt = (await _chat_row(chat)).accepted_tested_run
    assert receipt is not None and (receipt.modified_at, receipt.workflow_run_id) == (newer, None)
