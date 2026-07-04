from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.exceptions import InvalidCredentialId, SkyvernHTTPException
from skyvern.forge.sdk.copilot.output_policy import OutputPolicyReason, evaluate_output_policy
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import Base, WorkflowRunCredentialSelectionModel
from skyvern.forge.sdk.db.repositories.workflow_run_credential_selections import (
    WorkflowRunCredentialSelectionsRepository,
)
from skyvern.forge.sdk.workflow.credential_selection import select_credential_for_run
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import CredentialParameterYAML, WorkflowDefinitionYAML


def _credential_parameter(
    *,
    credential_id: str = "cred_a",
    credential_ids: list[str] | None = None,
    selection_strategy: str | None = None,
) -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key="login_cred",
        credential_parameter_id="cp_test",
        workflow_id="wf_test",
        credential_id=credential_id,
        credential_ids=credential_ids,
        selection_strategy=selection_strategy,
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
        mock_app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts = {"wr_test": context}
        credential_ids = await service._resolve_login_block_credential_ids(
            block=block,
            workflow_run_id="wr_test",
            organization_id="org_test",
            workflow_permanent_id="wpid_test",
        )

    assert credential_ids == ["cred_b"]
    context.resolve_credential_parameter_id.assert_awaited_once_with(parameter, "org_test")
