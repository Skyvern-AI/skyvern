from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.exceptions import (
    InvalidCredentialId,
    RuntimeSequentialCredentialUnsupported,
    SequentialCredentialLimitExceeded,
    SkyvernHTTPException,
)
from skyvern.forge.sdk.copilot.output_policy import OutputPolicyReason, evaluate_output_policy
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import Base, WorkflowRunCredentialSelectionModel
from skyvern.forge.sdk.db.repositories.workflow_run_credential_selections import (
    WorkflowRunCredentialSelectionsRepository,
)
from skyvern.forge.sdk.workflow.browser_profile_key import build_browser_profile_key_digest
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.credential_selection import select_credential_for_run
from skyvern.forge.sdk.workflow.models.parameter import (
    ContextParameter,
    CredentialParameter,
    OutputParameter,
    Parameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import CredentialParameterYAML, WorkflowDefinitionYAML


def _credential_parameter(
    *,
    key: str = "login_cred",
    credential_id: str = "cred_a",
    credential_ids: list[str] | None = None,
    selection_strategy: str | None = None,
) -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key=key,
        credential_parameter_id=f"cp_{key}",
        workflow_id="wf_test",
        credential_id=credential_id,
        credential_ids=credential_ids,
        selection_strategy=selection_strategy,
        created_at=now,
        modified_at=now,
    )


def _workflow_parameter(
    key: str,
    workflow_parameter_type: WorkflowParameterType = WorkflowParameterType.STRING,
) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_id="wf_test",
        workflow_parameter_type=workflow_parameter_type,
        created_at=now,
        modified_at=now,
    )


class _SelectionRepo:
    def __init__(
        self,
        *,
        existing: dict[tuple[str, str], str] | None = None,
        latest: dict[str, datetime] | None = None,
        raise_on_create: bool = False,
    ) -> None:
        self.existing = existing or {}
        self.latest = latest or {}
        self.raise_on_create = raise_on_create
        self.created: list[dict[str, str]] = []

    async def get_selection(self, workflow_run_id: str, parameter_key: str) -> str | None:
        return self.existing.get((workflow_run_id, parameter_key))

    async def get_latest_selections(
        self,
        *,
        organization_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_ids: list[str],
    ) -> dict[str, datetime]:
        return {
            credential_id: self.latest[credential_id]
            for credential_id in credential_ids
            if credential_id in self.latest
        }

    async def create_selection(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_id: str,
    ) -> str:
        if self.raise_on_create:
            self.existing[(workflow_run_id, parameter_key)] = "cred_winner"
            raise IntegrityError("insert", {}, Exception("duplicate"))
        self.created.append(
            {
                "organization_id": organization_id,
                "workflow_run_id": workflow_run_id,
                "workflow_permanent_id": workflow_permanent_id,
                "parameter_key": parameter_key,
                "credential_id": credential_id,
            }
        )
        self.existing[(workflow_run_id, parameter_key)] = credential_id
        return credential_id

    async def create_round_robin_selection(
        self,
        *,
        organization_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        parameter_key: str,
        credential_ids: list[str],
    ) -> str:
        existing = await self.get_selection(workflow_run_id=workflow_run_id, parameter_key=parameter_key)
        if existing:
            return existing

        latest_selections = await self.get_latest_selections(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            parameter_key=parameter_key,
            credential_ids=credential_ids,
        )
        unseen = next((candidate for candidate in credential_ids if candidate not in latest_selections), None)
        credential_id = (
            unseen if unseen is not None else min(credential_ids, key=lambda candidate: latest_selections[candidate])
        )
        return await self.create_selection(
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            workflow_permanent_id=workflow_permanent_id,
            parameter_key=parameter_key,
            credential_id=credential_id,
        )


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncEngine:
    engine = _build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_db(sqlite_engine: AsyncEngine) -> AgentDB:
    return AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)


async def _select(repo: _SelectionRepo, credential_ids: list[str], strategy: str | None = None) -> str:
    with patch("skyvern.forge.sdk.workflow.credential_selection.app") as mock_app:
        mock_app.DATABASE.workflow_run_credential_selections = repo
        return await select_credential_for_run(
            workflow_run_id="wr_test",
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
            parameter_key="login_cred",
            credential_ids=credential_ids,
            selection_strategy=strategy,
        )


@pytest.mark.asyncio
async def test_round_robin_picks_unseen_first() -> None:
    repo = _SelectionRepo(latest={"cred_a": datetime.now(timezone.utc)})

    selected = await _select(repo, ["cred_a", "cred_b", "cred_c"])

    assert selected == "cred_b"
    assert repo.created[0]["credential_id"] == "cred_b"


@pytest.mark.asyncio
async def test_round_robin_picks_oldest_last_used_and_ties_by_list_order() -> None:
    now = datetime.now(timezone.utc)
    repo = _SelectionRepo(latest={"cred_a": now, "cred_b": now - timedelta(minutes=5), "cred_c": now})

    selected = await _select(repo, ["cred_a", "cred_b", "cred_c"])

    assert selected == "cred_b"

    tied_repo = _SelectionRepo(latest={"cred_a": now, "cred_b": now, "cred_c": now})
    tied_selected = await _select(tied_repo, ["cred_a", "cred_b", "cred_c"])

    assert tied_selected == "cred_a"


@pytest.mark.asyncio
async def test_selection_is_idempotent_for_run_and_key() -> None:
    repo = _SelectionRepo(existing={("wr_test", "login_cred"): "cred_a"})

    first = await _select(repo, ["cred_a", "cred_b"])
    second = await _select(repo, ["cred_a", "cred_b"])

    assert first == "cred_a"
    assert second == "cred_a"
    assert repo.created == []


@pytest.mark.asyncio
async def test_run_credential_override_persists_for_rotation_parameter() -> None:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential()
    workflow_run = _setup_workflow_run()
    repo = _SelectionRepo()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.workflow_run_credential_selections = repo
        overrides = await service._apply_run_credential_parameter_overrides(
            workflow=workflow,
            workflow_run=workflow_run,
            organization_id="org_test",
            request_data={"login_cred": "cred_b"},
        )

    assert overrides == {"login_cred": "cred_b"}
    assert repo.created == [
        {
            "organization_id": "org_test",
            "workflow_run_id": "wr_test",
            "workflow_permanent_id": "wpid_test",
            "parameter_key": "login_cred",
            "credential_id": "cred_b",
        }
    ]


@pytest.mark.asyncio
async def test_run_credential_override_rejects_credentials_outside_rotation_pool() -> None:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential()

    with pytest.raises(SkyvernHTTPException, match="configured rotation or fallback credentials"):
        await service._apply_run_credential_parameter_overrides(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
            request_data={"login_cred": "cred_other"},
        )


@pytest.mark.asyncio
async def test_run_credential_override_rejects_conflicting_existing_selection() -> None:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential()
    repo = _SelectionRepo(existing={("wr_test", "login_cred"): "cred_a"})

    with (
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        pytest.raises(SkyvernHTTPException, match="conflicts with an existing credential selection"),
    ):
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.workflow_run_credential_selections = repo
        await service._apply_run_credential_parameter_overrides(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
            request_data={"login_cred": "cred_b"},
        )

    assert repo.created == []


@pytest.mark.asyncio
async def test_select_rotating_credentials_keeps_override_and_selects_remaining() -> None:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key="{{ login_cred }}-{{ backup_cred }}")
    workflow.workflow_definition.parameters = [
        _credential_parameter(key="login_cred", credential_ids=["cred_a", "cred_b"]),
        _credential_parameter(key="backup_cred", credential_id="cred_c", credential_ids=["cred_c", "cred_d"]),
    ]
    select_mock = AsyncMock(return_value="cred_d")

    with patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock):
        selections = await service._select_rotating_credential_parameters_for_render(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
            credential_parameter_overrides={"login_cred": "cred_b"},
        )

    assert selections == {"login_cred": "cred_b", "backup_cred": "cred_d"}
    select_mock.assert_awaited_once_with(
        workflow_run_id="wr_test",
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        parameter_key="backup_cred",
        credential_ids=["cred_c", "cred_d"],
        selection_strategy=None,
    )


def _fallback_only_credential_parameter() -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key="login_cred",
        credential_parameter_id="cp_login",
        workflow_id="wf_test",
        credential_id="cred_primary",
        credential_ids=None,
        fallback_credential_ids=["cred_fb1", "cred_fb2"],
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_select_render_includes_fallback_only_primary_credential() -> None:
    # A login credential with fallbacks but no rotation pool serializes as a block-scoped
    # CredentialParameter (credential_ids empty). Its primary must still reach the profile-key render
    # values, or a browser_profile_key referencing this parameter fails setup on the initial run.
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key="{{ login_cred }}")
    workflow.workflow_definition.parameters = [_fallback_only_credential_parameter()]
    select_mock = AsyncMock()

    with patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock):
        selections = await service._select_rotating_credential_parameters_for_render(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
        )

    assert selections == {"login_cred": "cred_primary"}
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_render_resolves_indirect_fallback_primary_from_parameter_values() -> None:
    # A fallback-only credential_id can indirectly reference another workflow parameter carrying the
    # real credential value (mirrors WorkflowRunContext.resolve_credential_parameter_id). The render
    # must resolve it, or a browser_profile_key would collapse distinct accounts onto one profile.
    now = datetime.now(timezone.utc)
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key="{{ login_cred }}")
    workflow.workflow_definition.parameters = [
        CredentialParameter(
            key="login_cred",
            credential_parameter_id="cp_login",
            workflow_id="wf_test",
            credential_id="account_param",
            credential_ids=None,
            fallback_credential_ids=["cred_fb1"],
            created_at=now,
            modified_at=now,
        )
    ]
    select_mock = AsyncMock()

    with patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock):
        selections = await service._select_rotating_credential_parameters_for_render(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
            parameter_values={"account_param": "cred_runtime"},
        )

    assert selections == {"login_cred": "cred_runtime"}
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_render_fallback_override_takes_precedence_over_primary() -> None:
    # On a fallback retry the chosen fallback arrives as a credential_parameter_override and must win
    # over the primary.
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key="{{ login_cred }}")
    workflow.workflow_definition.parameters = [_fallback_only_credential_parameter()]
    select_mock = AsyncMock()

    with patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock):
        selections = await service._select_rotating_credential_parameters_for_render(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
            credential_parameter_overrides={"login_cred": "cred_fb1"},
        )

    assert selections == {"login_cred": "cred_fb1"}
    select_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_random_selection_returns_member(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _SelectionRepo()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.credential_selection.random.choice", lambda ids: ids[-1])

    selected = await _select(repo, ["cred_a", "cred_b"], "random")

    assert selected == "cred_b"
    assert selected in {"cred_a", "cred_b"}


@pytest.mark.asyncio
async def test_duplicate_insert_race_returns_existing_winner() -> None:
    repo = _SelectionRepo(raise_on_create=True)

    selected = await _select(repo, ["cred_a", "cred_b"])

    assert selected == "cred_winner"


@pytest.mark.asyncio
async def test_round_robin_repository_serialized_path_picks_distinct_credentials(sqlite_db: AgentDB) -> None:
    repo = sqlite_db.workflow_run_credential_selections

    first = await repo.create_round_robin_selection(
        organization_id="org_test",
        workflow_run_id="wr_one",
        workflow_permanent_id="wpid_test",
        parameter_key="login_cred",
        credential_ids=["cred_a", "cred_b"],
    )
    second = await repo.create_round_robin_selection(
        organization_id="org_test",
        workflow_run_id="wr_two",
        workflow_permanent_id="wpid_test",
        parameter_key="login_cred",
        credential_ids=["cred_a", "cred_b"],
    )

    assert first == "cred_a"
    assert second == "cred_b"


@pytest.mark.asyncio
async def test_round_robin_repository_idempotent_recall_returns_existing(sqlite_db: AgentDB) -> None:
    repo = sqlite_db.workflow_run_credential_selections

    first = await repo.create_round_robin_selection(
        organization_id="org_test",
        workflow_run_id="wr_one",
        workflow_permanent_id="wpid_test",
        parameter_key="login_cred",
        credential_ids=["cred_a", "cred_b"],
    )
    second = await repo.create_round_robin_selection(
        organization_id="org_test",
        workflow_run_id="wr_one",
        workflow_permanent_id="wpid_test",
        parameter_key="login_cred",
        credential_ids=["cred_a", "cred_b"],
    )

    async with sqlite_db.Session() as session:
        count = (
            await session.execute(select(func.count()).select_from(WorkflowRunCredentialSelectionModel))
        ).scalar_one()

    assert first == "cred_a"
    assert second == "cred_a"
    assert count == 1


@pytest.mark.asyncio
async def test_repository_get_selections_for_run_returns_mapping(sqlite_db: AgentDB) -> None:
    repo = sqlite_db.workflow_run_credential_selections

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunCredentialSelectionModel(
                    organization_id="org_test",
                    workflow_run_id="wr_test",
                    workflow_permanent_id="wpid_test",
                    parameter_key="login_cred",
                    credential_id="cred_a",
                ),
                WorkflowRunCredentialSelectionModel(
                    organization_id="org_test",
                    workflow_run_id="wr_test",
                    workflow_permanent_id="wpid_test",
                    parameter_key="backup_cred",
                    credential_id="cred_b",
                ),
                WorkflowRunCredentialSelectionModel(
                    organization_id="org_test",
                    workflow_run_id="wr_other",
                    workflow_permanent_id="wpid_test",
                    parameter_key="login_cred",
                    credential_id="cred_other",
                ),
            ]
        )
        await session.commit()

    assert await repo.get_selections_for_run("wr_test") == {
        "backup_cred": "cred_b",
        "login_cred": "cred_a",
    }
    assert await repo.get_selections_for_run("wr_missing") == {}


@pytest.mark.asyncio
async def test_rotation_advisory_lock_skips_non_postgres_dialect() -> None:
    repo = WorkflowRunCredentialSelectionsRepository(MagicMock())
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    session.execute = AsyncMock()

    await repo._take_rotation_advisory_lock(session, "wrcs:org:wpid:login_cred")

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_empty_credential_ids() -> None:
    service = WorkflowService()
    org = SimpleNamespace(organization_id="org_test")
    parameter = _credential_parameter(credential_ids=[])

    with pytest.raises(SkyvernHTTPException, match="credential_ids"):
        await service._validate_and_normalize_credential_rotation_parameters([parameter], org)


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_unknown_credential_id() -> None:
    service = WorkflowService()
    org = SimpleNamespace(organization_id="org_test")
    parameter = _credential_parameter(credential_ids=["cred_missing"])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=[])
        with pytest.raises(InvalidCredentialId):
            await service._validate_and_normalize_credential_rotation_parameters([parameter], org)


@pytest.mark.asyncio
async def test_workflow_save_validation_rejects_bad_strategy() -> None:
    service = WorkflowService()
    org = SimpleNamespace(organization_id="org_test")
    parameter = _credential_parameter(credential_ids=["cred_a"], selection_strategy="newest")

    with pytest.raises(SkyvernHTTPException, match="selection_strategy"):
        await service._validate_and_normalize_credential_rotation_parameters([parameter], org)


@pytest.mark.asyncio
async def test_workflow_save_validation_normalizes_credential_id_to_first_rotating_id() -> None:
    service = WorkflowService()
    org = SimpleNamespace(organization_id="org_test")
    parameter = _credential_parameter(credential_id="cred_stale", credential_ids=["cred_a", "cred_b"])
    existing = [SimpleNamespace(credential_id="cred_a"), SimpleNamespace(credential_id="cred_b")]

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(return_value=existing)
        await service._validate_and_normalize_credential_rotation_parameters([parameter], org)

    assert parameter.credential_id == "cred_a"


@pytest.mark.asyncio
async def test_workflow_save_validation_dedupes_credential_ids_preserving_order() -> None:
    service = WorkflowService()
    org = SimpleNamespace(organization_id="org_test")
    parameter = _credential_parameter(
        credential_id="cred_stale",
        credential_ids=["cred_a", "cred_b", "cred_a", "cred_c", "cred_b"],
    )
    existing = [
        SimpleNamespace(credential_id="cred_a"),
        SimpleNamespace(credential_id="cred_b"),
        SimpleNamespace(credential_id="cred_c"),
    ]

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_get_credentials = AsyncMock(return_value=existing)
        mock_app.DATABASE.credentials.get_credentials_by_ids = mock_get_credentials
        await service._validate_and_normalize_credential_rotation_parameters([parameter], org)

    assert parameter.credential_ids == ["cred_a", "cred_b", "cred_c"]
    assert parameter.credential_id == "cred_a"
    mock_get_credentials.assert_awaited_once_with(["cred_a", "cred_b", "cred_c"], organization_id="org_test")


def test_output_policy_origin_broadening_checks_non_first_rotating_credential() -> None:
    workflow_yaml = """
title: Login
workflow_definition:
  parameters:
    - parameter_type: credential
      key: login_cred
      credential_id: cred_first
      credential_ids:
        - cred_first
        - cred_second
  blocks:
    - block_type: login
      label: Login
      url: https://portal.example.com/login
      parameter_keys:
        - login_cred
"""
    request_policy = RequestPolicy(
        resolved_credentials=[
            SimpleNamespace(credential_id="cred_first", tested_url="https://portal.example.com/login"),
            SimpleNamespace(credential_id="cred_second", tested_url="https://other.example.com/login"),
        ]
    )

    verdict = evaluate_output_policy(request_policy=request_policy, workflow_yaml=workflow_yaml)

    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_yaml_to_credential_parameter_round_trip_preserves_rotation_fields() -> None:
    yaml_definition = WorkflowDefinitionYAML(
        parameters=[
            CredentialParameterYAML(
                key="login_cred",
                credential_id="cred_a",
                credential_ids=["cred_a", "cred_b"],
                selection_strategy="round_robin",
            )
        ],
        blocks=[],
    )

    definition = convert_workflow_definition(yaml_definition, workflow_id="wf_test")
    parameter = definition.parameters[0]

    assert isinstance(parameter, CredentialParameter)
    assert parameter.credential_id == "cred_a"
    assert parameter.credential_ids == ["cred_a", "cred_b"]
    assert parameter.selection_strategy == "round_robin"


@pytest.mark.asyncio
async def test_resolve_login_block_credential_ids_returns_selected_rotating_id() -> None:
    service = WorkflowService()
    parameter = _credential_parameter(credential_ids=["cred_a", "cred_b"])
    context = MagicMock()
    context.resolve_credential_parameter_id = AsyncMock(return_value="cred_b")
    block = SimpleNamespace(parameters=[parameter])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts = {"wr_test": context}
        credential_ids = await service._resolve_login_block_credential_ids(
            block=block,
            workflow_run_id="wr_test",
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
        )

    assert credential_ids == ["cred_b"]
    context.resolve_credential_parameter_id.assert_awaited_once_with(parameter, "org_test")


def _setup_workflow_with_rotating_credential(browser_profile_key: str | None = "{{ login_cred }}") -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        proxy_location=None,
        webhook_callback_url=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_profile_id=None,
        persist_browser_session=True,
        pin_saved_session_ip=False,
        browser_profile_key=browser_profile_key,
        title="Workflow",
        max_elapsed_time_minutes=None,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
        workflow_definition=SimpleNamespace(
            parameters=[_credential_parameter(credential_ids=["cred_a", "cred_b"])], blocks=[]
        ),
    )


def _setup_workflow_run() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        browser_session_id=None,
        browser_profile_id=None,
        browser_seed_source=None,
        browser_sink_profile_id=None,
        retried_from_workflow_run_id=None,
        proxy_location=None,
    )


async def _setup_rotation_profile_run(
    *,
    select_side_effect: str | Exception,
    profile_id: str,
) -> tuple[SimpleNamespace, MagicMock]:
    result, mock_app, _, caught = await _attempt_setup_rotation_profile_run(
        select_side_effect=select_side_effect,
        profile_id=profile_id,
    )
    if caught:
        raise caught
    assert result is not None
    return result, mock_app


async def _attempt_setup_rotation_profile_run(
    *,
    select_side_effect: str | Exception,
    profile_id: str,
    browser_profile_key: str | None = "{{ login_cred }}",
) -> tuple[SimpleNamespace | None, MagicMock, WorkflowService, Exception | None]:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key=browser_profile_key)
    workflow_run = _setup_workflow_run()
    updated_run_values = dict(workflow_run.__dict__)
    updated_run_values["browser_profile_id"] = profile_id
    updated_run = SimpleNamespace(**updated_run_values)
    organization = SimpleNamespace(
        organization_id="org_test",
        organization_name="Test Org",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]

    select_mock = (
        AsyncMock(side_effect=select_side_effect)
        if isinstance(select_side_effect, Exception)
        else AsyncMock(return_value=select_side_effect)
    )
    with (
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock),
    ):
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.is_browser_memory_engine_enabled = AsyncMock(return_value=False)
        mock_app.DATABASE.browser_sessions.get_or_create_managed_browser_profile = AsyncMock(
            return_value=(
                SimpleNamespace(browser_profile_id=profile_id, is_managed=True, proxy_session_id=None),
                False,
            )
        )
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock(return_value=updated_run)
        mock_app.DATABASE.organizations.get_organization = AsyncMock(return_value=organization)
        selected_credential_id = select_side_effect if isinstance(select_side_effect, str) else "cred_a"
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(
            return_value=[SimpleNamespace(credential_id=selected_credential_id, run_sequentially=True)]
        )

        result = None
        caught = None
        try:
            result = await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=WorkflowRequestBody(data={}),
                workflow_permanent_id="wpid_test",
                organization=organization,
            )
        except Exception as exc:
            caught = exc

    return result, mock_app, service, caught


async def _setup_bound_credentials(
    credential_parameters: list[Parameter],
    *,
    workflow_parameters: list[WorkflowParameter] | None = None,
    request_data: dict[str, str] | None = None,
    credentials: dict[str, bool],
    selection_repo: _SelectionRepo | None = None,
) -> SimpleNamespace:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key=None)
    workflow.persist_browser_session = False
    workflow.workflow_definition.parameters = credential_parameters
    workflow_run = _setup_workflow_run()
    workflow_run.sequential_credential_id = None
    organization = SimpleNamespace(
        organization_id="org_test",
        organization_name="Test Org",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )
    repo = selection_repo or _SelectionRepo()

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=workflow_parameters or [])  # type: ignore[method-assign]
    service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    # These tests assert credential identity, not browser-seed resolution; stub the seed step so they
    # do not depend on the unrelated browser-memory machinery reached via _resolve_and_stamp_run_seed.
    service._resolve_and_stamp_run_seed = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **kwargs: kwargs["workflow_run"]
    )

    async def update_workflow_run(*, workflow_run_id: str, **values: object) -> SimpleNamespace:
        assert workflow_run_id == workflow_run.workflow_run_id
        for key, value in values.items():
            setattr(workflow_run, key, value)
        return workflow_run

    async def get_credentials_by_ids(credential_ids: list[str], *, organization_id: str) -> list[SimpleNamespace]:
        assert organization_id == "org_test"
        return [
            SimpleNamespace(credential_id=credential_id, run_sequentially=credentials[credential_id])
            for credential_id in credential_ids
            if credential_id in credentials
        ]

    with (
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.credential_selection.app") as selection_app,
    ):
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock(side_effect=update_workflow_run)
        mock_app.DATABASE.credentials.get_credentials_by_ids = AsyncMock(side_effect=get_credentials_by_ids)
        selection_app.DATABASE.workflow_run_credential_selections = repo
        return await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=WorkflowRequestBody(data=request_data or {}),
            workflow_permanent_id="wpid_test",
            organization=organization,
        )


def _runtime_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="Workflow",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )


@pytest.mark.asyncio
async def test_direct_bindings_snapshot_only_opted_in_credentials_and_runtime_resolves_same_ids() -> None:
    sequential = _credential_parameter(key="serial_login", credential_id="cred_serial")
    parallel = _credential_parameter(key="parallel_login", credential_id="cred_parallel")

    workflow_run = await _setup_bound_credentials(
        [sequential, parallel],
        credentials={"cred_serial": True, "cred_parallel": False},
    )
    sequential_context = _runtime_context()
    sequential_context.values["cred_serial"] = "cred_unrelated_runtime_value"
    parallel_context = _runtime_context()

    assert workflow_run.sequential_credential_id == "cred_serial"
    assert await sequential_context.resolve_credential_parameter_id(sequential, "org_test") == "cred_serial"
    assert await parallel_context.resolve_credential_parameter_id(parallel, "org_test") == "cred_parallel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_parameter_type",
    [WorkflowParameterType.STRING, WorkflowParameterType.CREDENTIAL_ID],
)
async def test_plain_indirect_binding_snapshots_and_runtime_resolves_bound_parameter_value(
    workflow_parameter_type: WorkflowParameterType,
) -> None:
    account_parameter = _workflow_parameter("account_param", workflow_parameter_type)
    login = _credential_parameter(credential_id=account_parameter.key)

    workflow_run = await _setup_bound_credentials(
        [login],
        workflow_parameters=[account_parameter],
        request_data={account_parameter.key: "cred_runtime"},
        credentials={"cred_runtime": True},
    )
    context = _runtime_context()
    context.parameters[account_parameter.key] = account_parameter
    if workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
        context.values[account_parameter.key] = {"context": "credential placeholders"}
        context.resolved_credential_parameter_ids[account_parameter.key] = "cred_runtime"
    else:
        context.values[account_parameter.key] = "cred_runtime"

    assert workflow_run.sequential_credential_id == "cred_runtime"
    assert await context.resolve_credential_parameter_id(login, "org_test") == "cred_runtime"
    assert context.get_resolved_credential_parameter_id(login.key) == "cred_runtime"


@pytest.mark.asyncio
async def test_plain_indirect_binding_rejects_unknown_bound_credential_not_literal_key() -> None:
    account_parameter = _workflow_parameter("account_param")
    login = _credential_parameter(credential_id=account_parameter.key)

    with pytest.raises(InvalidCredentialId, match="cred_missing"):
        await _setup_bound_credentials(
            [login],
            workflow_parameters=[account_parameter],
            request_data={account_parameter.key: "cred_missing"},
            credentials={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_parameter_type", ["context", "output"])
async def test_runtime_only_indirect_binding_defers_credential_validation_until_execution(
    runtime_parameter_type: str,
) -> None:
    source = _workflow_parameter("source")
    now = datetime.now(timezone.utc)
    runtime_parameter = (
        ContextParameter(key="runtime_credential", source=source)
        if runtime_parameter_type == "context"
        else OutputParameter(
            key="runtime_credential",
            output_parameter_id="op_runtime_credential",
            workflow_id="wf_test",
            created_at=now,
            modified_at=now,
        )
    )
    login = _credential_parameter(credential_id=runtime_parameter.key)

    workflow_run = await _setup_bound_credentials(
        [login, runtime_parameter],
        credentials={},
    )

    assert workflow_run.sequential_credential_id is None


@pytest.mark.asyncio
async def test_runtime_only_indirect_binding_fails_closed_if_resolved_credential_is_sequential() -> None:
    context = _runtime_context()
    source = _workflow_parameter("source")
    runtime_parameter = ContextParameter(key="runtime_credential", source=source)
    login = _credential_parameter(credential_id=runtime_parameter.key)
    context.parameters[runtime_parameter.key] = runtime_parameter
    context.values[runtime_parameter.key] = "cred_runtime"
    organization = SimpleNamespace(organization_id="org_test")

    with patch("skyvern.forge.sdk.workflow.context_manager.app") as mock_app:
        mock_app.DATABASE.credentials.get_credential = AsyncMock(
            return_value=SimpleNamespace(credential_id="cred_runtime", run_sequentially=True)
        )
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
            return_value=SimpleNamespace(sequential_credential_id=None)
        )

        with pytest.raises(RuntimeSequentialCredentialUnsupported, match="wr_test"):
            await context.register_credential_parameter_value(login, organization)

    mock_app.CREDENTIAL_VAULT_SERVICES.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_sequentially", "stamped_credential_id"),
    [
        (True, "cred_runtime"),
        (False, None),
    ],
)
async def test_runtime_credential_registration_proceeds_when_lane_is_safe(
    run_sequentially: bool,
    stamped_credential_id: str | None,
) -> None:
    context = _runtime_context()
    source = _workflow_parameter("source")
    runtime_parameter = ContextParameter(key="runtime_credential", source=source)
    login = _credential_parameter(credential_id=runtime_parameter.key)
    context.parameters[runtime_parameter.key] = runtime_parameter
    context.values[runtime_parameter.key] = "cred_runtime"
    organization = SimpleNamespace(organization_id="org_test")
    db_credential = SimpleNamespace(
        credential_id="cred_runtime",
        run_sequentially=run_sequentially,
        tested_url=None,
        vault_type=None,
        totp_identifier=None,
    )
    credential = MagicMock()
    credential.model_dump.return_value = {}
    credential_service = MagicMock()
    credential_service.get_credential_item = AsyncMock(return_value=SimpleNamespace(credential=credential))

    with patch("skyvern.forge.sdk.workflow.context_manager.app") as mock_app:
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=db_credential)
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
            return_value=SimpleNamespace(sequential_credential_id=stamped_credential_id)
        )
        mock_app.CREDENTIAL_VAULT_SERVICES.get.return_value = credential_service
        # Mirror the OSS no-op hook: return the resolved item unchanged.
        mock_app.AGENT_FUNCTION.process_registered_credential_item = AsyncMock(
            side_effect=lambda *, workflow_run_id, db_credential, credential_item: credential_item
        )

        await context.register_credential_parameter_value(login, organization)

    credential_service.get_credential_item.assert_awaited_once_with(db_credential)
    assert context.get_resolved_credential_parameter_id(login.key) == "cred_runtime"
    if run_sequentially:
        mock_app.DATABASE.workflow_runs.get_workflow_run.assert_awaited_once_with("wr_test", "org_test")
    else:
        mock_app.DATABASE.workflow_runs.get_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyed_rotation_snapshot_and_runtime_reuse_one_persisted_selection() -> None:
    login = _credential_parameter(credential_ids=["cred_a", "cred_b"])
    repo = _SelectionRepo()

    workflow_run = await _setup_bound_credentials(
        [login],
        credentials={"cred_a": True, "cred_b": False},
        selection_repo=repo,
    )
    context = _runtime_context()
    with patch("skyvern.forge.sdk.workflow.credential_selection.app") as selection_app:
        selection_app.DATABASE.workflow_run_credential_selections = repo
        runtime_credential_id = await context.resolve_credential_parameter_id(login, "org_test")

    assert workflow_run.sequential_credential_id == "cred_a"
    assert runtime_credential_id == "cred_a"
    assert repo.existing[("wr_test", "login_cred")] == "cred_a"
    assert [created["credential_id"] for created in repo.created] == ["cred_a"]


@pytest.mark.asyncio
async def test_two_opted_in_credentials_fail_closed_before_publication() -> None:
    first = _credential_parameter(key="first_login", credential_id="cred_first")
    second = _credential_parameter(key="second_login", credential_id="cred_second")

    with pytest.raises(SequentialCredentialLimitExceeded):
        await _setup_bound_credentials(
            [first, second],
            credentials={"cred_first": True, "cred_second": True},
        )


@pytest.mark.asyncio
async def test_setup_workflow_run_uses_selected_rotating_credential_for_profile_key() -> None:
    result, mock_app = await _setup_rotation_profile_run(select_side_effect="cred_b", profile_id="bp_selected")

    assert result.browser_profile_id == "bp_selected"
    mock_app.DATABASE.browser_sessions.get_or_create_managed_browser_profile.assert_awaited_once_with(
        organization_id="org_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=build_browser_profile_key_digest("cred_b"),
        name="Workflow (auto-saved: cred_b)",
    )


@pytest.mark.asyncio
async def test_keyed_setup_workflow_run_fails_when_rotation_selection_fails() -> None:
    result, _, service, caught = await _attempt_setup_rotation_profile_run(
        select_side_effect=RuntimeError("selection failed"),
        profile_id="bp_keyless",
    )

    assert result is None
    assert isinstance(caught, RuntimeError)
    assert str(caught) == "selection failed"
    service.mark_workflow_run_as_failed.assert_awaited_once()
    assert service.mark_workflow_run_as_failed.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert service.mark_workflow_run_as_failed.await_args.kwargs["failure_reason"].startswith(
        "Setup workflow failed. failure reason:"
    )


@pytest.mark.asyncio
async def test_keyless_setup_workflow_run_fails_closed_when_rotation_selection_fails() -> None:
    result, mock_app, service, caught = await _attempt_setup_rotation_profile_run(
        select_side_effect=RuntimeError("selection failed"),
        profile_id="bp_keyless",
        browser_profile_key=None,
    )

    assert result is None
    assert isinstance(caught, RuntimeError)
    assert str(caught) == "selection failed"
    mock_app.DATABASE.browser_sessions.get_or_create_managed_browser_profile.assert_not_awaited()
    service.mark_workflow_run_as_failed.assert_awaited_once()
    assert service.mark_workflow_run_as_failed.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert service.mark_workflow_run_as_failed.await_args.kwargs["failure_reason"].startswith(
        "Setup workflow failed. failure reason:"
    )


async def _select_render_with_failed_rotation(
    *,
    browser_profile_key: str | None,
    candidate_credentials: list[SimpleNamespace] | Exception,
) -> dict[str, str]:
    service = WorkflowService()
    workflow = _setup_workflow_with_rotating_credential(browser_profile_key=browser_profile_key)
    select_mock = AsyncMock(side_effect=RuntimeError("selection failed"))
    get_credentials = (
        AsyncMock(side_effect=candidate_credentials)
        if isinstance(candidate_credentials, Exception)
        else AsyncMock(return_value=candidate_credentials)
    )
    with (
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        patch("skyvern.forge.sdk.workflow.service.select_credential_for_run", select_mock),
    ):
        mock_app.DATABASE.organizations.get_organization = AsyncMock(
            return_value=SimpleNamespace(organization_id="org_test")
        )
        mock_app.DATABASE.credentials.get_credentials_by_ids = get_credentials
        return await service._select_rotating_credential_parameters_for_render(
            workflow=workflow,
            workflow_run=_setup_workflow_run(),
            organization_id="org_test",
        )


@pytest.mark.asyncio
async def test_keyless_rotation_selection_failure_preserves_legacy_when_no_candidate_is_sequential() -> None:
    # A keyless workflow whose rotation pool is provably non-sequential keeps the legacy
    # best-effort partial selection when rotation selection fails — the pre-feature behavior for runs
    # that cannot resolve to a sequential credential. It must NOT hard-fail setup.
    selections = await _select_render_with_failed_rotation(
        browser_profile_key=None,
        candidate_credentials=[
            SimpleNamespace(credential_id="cred_a", run_sequentially=False),
            SimpleNamespace(credential_id="cred_b", run_sequentially=False),
        ],
    )
    assert selections == {}


@pytest.mark.asyncio
async def test_keyless_rotation_selection_failure_fails_closed_when_a_candidate_is_sequential() -> None:
    # A keyless workflow with any opted-in candidate must fail closed on selection failure: the run
    # could have resolved to that sequential credential, and silently skipping it drops the lane.
    with pytest.raises(RuntimeError, match="selection failed"):
        await _select_render_with_failed_rotation(
            browser_profile_key=None,
            candidate_credentials=[
                SimpleNamespace(credential_id="cred_a", run_sequentially=False),
                SimpleNamespace(credential_id="cred_b", run_sequentially=True),
            ],
        )


@pytest.mark.asyncio
async def test_keyless_rotation_selection_failure_fails_closed_when_candidate_unverifiable() -> None:
    # A candidate that cannot be verified (missing/invalid id -> InvalidCredentialId, or a lookup
    # error) means we cannot prove the pool is non-sequential, so fail closed conservatively.
    with pytest.raises(RuntimeError, match="selection failed"):
        await _select_render_with_failed_rotation(
            browser_profile_key=None,
            candidate_credentials=[],  # neither cred_a nor cred_b resolves -> InvalidCredentialId
        )


@pytest.mark.asyncio
async def test_keyed_rotation_selection_failure_fails_closed_even_with_non_sequential_pool() -> None:
    # A browser_profile_key must render a real value; a selection failure fails closed regardless of
    # the pool so distinct accounts never collapse onto one keyless managed profile.
    with pytest.raises(RuntimeError, match="selection failed"):
        await _select_render_with_failed_rotation(
            browser_profile_key="{{ login_cred }}",
            candidate_credentials=[
                SimpleNamespace(credential_id="cred_a", run_sequentially=False),
                SimpleNamespace(credential_id="cred_b", run_sequentially=False),
            ],
        )
