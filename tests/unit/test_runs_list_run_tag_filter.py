"""Tag-filter tests for the GET /runs ?tags= filter (mirrors test_workflows_list_tag_filter.py).

Two layers:
- Repository tests run against in-memory SQLite so `run_tag_run_id_subqueries` executes
  against real rows in both the main `task_runs` branch and the `search_key` fallback
  branch that queries `WorkflowRunModel` directly.
- Route tests mount the real FastAPI router with a mocked DATABASE/AGENT_FUNCTION so the
  `?tags=` registration, the shared `_parse_tag_filter_terms` wiring, and the
  `is_workflow_tagging_enabled` 403 guard are exercised end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import TaskRunModel, WorkflowModel, WorkflowRunModel, WorkflowRunTagEventModel
from skyvern.forge.sdk.db.repositories.tags import TagsRepository
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import TagEventType, TagSource
from skyvern.schemas.run_enums import RunType

ORG_ID = "o_test"


@pytest_asyncio.fixture
async def engine(sqlite_engine: AsyncEngine) -> AsyncEngine:
    return sqlite_engine


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> WorkflowRunsRepository:
    db = BaseAlchemyDB(engine)
    return WorkflowRunsRepository(db.Session, debug_enabled=False)


@pytest_asyncio.fixture
async def tags_repo(engine: AsyncEngine) -> TagsRepository:
    db = BaseAlchemyDB(engine)
    return TagsRepository(db.Session, debug_enabled=False)


async def _insert_task_run(
    repo: WorkflowRunsRepository,
    *,
    run_id: str,
    task_run_type: str = RunType.workflow_run.value,
    organization_id: str = ORG_ID,
    status: str = "completed",
) -> None:
    async with repo.Session() as session:
        session.add(
            TaskRunModel(
                task_run_id=f"tskrun_{run_id}",
                organization_id=organization_id,
                task_run_type=task_run_type,
                run_id=run_id,
                status=status,
            )
        )
        await session.commit()


async def _insert_workflow_run(
    repo: WorkflowRunsRepository,
    *,
    workflow_run_id: str,
    organization_id: str = ORG_ID,
    status: str = "completed",
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=workflow_run_id,
                workflow_id=f"wf_{workflow_run_id}",
                workflow_permanent_id=f"wpid_{workflow_run_id}",
                organization_id=organization_id,
                status=status,
            )
        )
        await session.commit()


async def _set_run_tag(
    repo: WorkflowRunsRepository,
    *,
    workflow_run_id: str,
    key: str,
    value: str,
    organization_id: str = ORG_ID,
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowRunTagEventModel(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                key=key,
                value=value,
                event_type=TagEventType.SET.value,
                set_at=datetime.now(timezone.utc),
                set_by="user_test",
                source=TagSource.SYSTEM.value,
            )
        )
        await session.commit()


async def _insert_workflow(
    repo: WorkflowRunsRepository,
    *,
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str = ORG_ID,
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                title="Test Workflow",
                workflow_definition={"blocks": [], "parameters": []},
                status="published",
                version=1,
            )
        )
        await session.commit()


async def _insert_workflow_run_for_workflow(
    repo: WorkflowRunsRepository,
    *,
    workflow_run_id: str,
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str = ORG_ID,
    status: str = "completed",
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=workflow_run_id,
                workflow_id=workflow_id,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                status=status,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_get_all_runs_v2_filters_by_run_tag(repo: WorkflowRunsRepository) -> None:
    await _insert_task_run(repo, run_id="wr_tagged")
    await _insert_task_run(repo, run_id="wr_untagged")
    await _set_run_tag(repo, workflow_run_id="wr_tagged", key="skyvern.platform", value="platform_a")

    rows = await repo.get_all_runs_v2(organization_id=ORG_ID, run_tags=[("skyvern.platform", "platform_a")])
    ids = {row["run_id"] for row in rows}

    assert "wr_tagged" in ids
    assert "wr_untagged" not in ids


@pytest.mark.asyncio
async def test_get_all_runs_v2_run_tag_filter_excludes_plain_task_runs(repo: WorkflowRunsRepository) -> None:
    # task_runs is polymorphic: task_run_type distinguishes a plain task run from a
    # workflow run, but both share the same run_id column that the tag filter joins on.
    # Run tags are only ever written keyed by an actual workflow_run_id, so a plain task
    # run's run_id can never appear in the tag-event subquery: tag-filtered pages are
    # workflow-run-only by construction.
    await _insert_task_run(repo, run_id="tsk_plain", task_run_type=RunType.task_v1.value)
    await _insert_task_run(repo, run_id="wr_tagged", task_run_type=RunType.workflow_run.value)
    await _set_run_tag(repo, workflow_run_id="wr_tagged", key="skyvern.platform", value="platform_a")

    rows = await repo.get_all_runs_v2(organization_id=ORG_ID, run_tags=[("skyvern.platform", "platform_a")])
    ids = {row["run_id"] for row in rows}

    assert ids == {"wr_tagged"}


@pytest.mark.asyncio
async def test_get_all_runs_v2_run_tag_filter_applies_to_search_key_fallback(repo: WorkflowRunsRepository) -> None:
    # Both workflow runs below have no task_runs row, forcing the search_key path into
    # the WorkflowRunModel fallback branch, where the run-tag filter must also apply.
    await _insert_workflow_run(repo, workflow_run_id="wr_search_tagged")
    await _insert_workflow_run(repo, workflow_run_id="wr_search_untagged")
    await _set_run_tag(repo, workflow_run_id="wr_search_tagged", key="skyvern.platform", value="platform_a")

    rows = await repo.get_all_runs_v2(
        organization_id=ORG_ID,
        search_key="wr_search",
        run_tags=[("skyvern.platform", "platform_a")],
    )
    ids = {row["run_id"] for row in rows}

    assert ids == {"wr_search_tagged"}


@pytest.mark.asyncio
async def test_get_workflow_runs_by_id_filters_by_run_tag(repo: WorkflowRunsRepository) -> None:
    workflow_id = "wf_shared"
    workflow_permanent_id = "wpid_shared"
    await _insert_workflow(repo, workflow_id=workflow_id, workflow_permanent_id=workflow_permanent_id)
    await _insert_workflow_run_for_workflow(
        repo,
        workflow_run_id="wr_agent_tagged",
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    await _insert_workflow_run_for_workflow(
        repo,
        workflow_run_id="wr_agent_untagged",
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    await _set_run_tag(repo, workflow_run_id="wr_agent_tagged", key="skyvern.platform", value="platform_a")

    runs = await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=ORG_ID,
        run_tags=[("skyvern.platform", "platform_a")],
    )
    ids = {run.workflow_run_id for run in runs}

    assert ids == {"wr_agent_tagged"}


def _make_org(org_id: str = ORG_ID) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=org_id,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_parse_and_gate_tag_filter_terms_only_gates_nonempty_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.routes import agent_protocol as ap

    gate = AsyncMock()
    monkeypatch.setattr(ap, "require_workflow_tagging", gate)
    organization = _make_org()

    assert await ap._parse_and_gate_tag_filter_terms(None, organization) == []
    gate.assert_not_awaited()

    assert await ap._parse_and_gate_tag_filter_terms(["env:prod"], organization) == [("env", "prod")]
    gate.assert_awaited_once_with(organization)


@pytest.fixture
def route_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from skyvern.forge.sdk.routes.routers import base_router

    captured: dict[str, object] = {}

    async def _get_all_runs_v2(*args: object, **kwargs: object) -> list[dict[str, object]]:
        captured["run_tags"] = kwargs.get("run_tags")
        return []

    workflow_runs_mock = MagicMock()
    workflow_runs_mock.get_all_runs_v2 = AsyncMock(side_effect=_get_all_runs_v2)

    app_mock = MagicMock()
    app_mock.DATABASE.workflow_runs = workflow_runs_mock
    app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=True)

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.dependency_overrides[org_auth_service.get_current_org] = lambda: _make_org()

    from skyvern.forge.sdk.routes import agent_protocol as ap

    monkeypatch.setattr(ap, "app", app_mock)

    client = TestClient(test_app)
    client.captured = captured  # type: ignore[attr-defined]
    client.app_mock = app_mock  # type: ignore[attr-defined]
    return client


def test_route_no_tags_param_passes_none_run_tags(route_client: TestClient) -> None:
    resp = route_client.get("/v1/runs")
    assert resp.status_code == 200, resp.text
    assert route_client.captured["run_tags"] is None  # type: ignore[attr-defined]


def test_route_tags_param_passes_parsed_run_tags(route_client: TestClient) -> None:
    resp = route_client.get("/v1/runs", params=[("tags", "skyvern.platform:platform_a")])
    assert resp.status_code == 200, resp.text
    assert route_client.captured["run_tags"] == [("skyvern.platform", "platform_a")]  # type: ignore[attr-defined]


def test_route_tags_enforces_workflow_tagging_gate(route_client: TestClient) -> None:
    route_client.app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    resp = route_client.get("/v1/runs", params=[("tags", "skyvern.platform:platform_a")])
    assert resp.status_code == 403, resp.text


@pytest.fixture
def agent_runs_route_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router

    captured: dict[str, object] = {}

    async def _get_workflow_runs_for_workflow_permanent_id(*args: object, **kwargs: object) -> list[object]:
        captured["run_tags"] = kwargs.get("run_tags")
        captured["exclude_child_runs"] = kwargs.get("exclude_child_runs")
        return []

    workflow_service_mock = MagicMock()
    workflow_service_mock.get_workflow_runs_for_workflow_permanent_id = AsyncMock(
        side_effect=_get_workflow_runs_for_workflow_permanent_id
    )

    app_mock = MagicMock()
    app_mock.WORKFLOW_SERVICE = workflow_service_mock
    app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=True)

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.include_router(legacy_base_router, prefix="/api/v1")
    test_app.dependency_overrides[org_auth_service.get_current_org] = lambda: _make_org()

    from skyvern.forge.sdk.routes import agent_protocol as ap

    monkeypatch.setattr(ap, "app", app_mock)

    client = TestClient(test_app)
    client.captured = captured  # type: ignore[attr-defined]
    client.app_mock = app_mock  # type: ignore[attr-defined]
    return client


def test_agent_runs_route_no_tags_param_passes_none_run_tags(agent_runs_route_client: TestClient) -> None:
    resp = agent_runs_route_client.get("/v1/workflows/wpid_test/runs")
    assert resp.status_code == 200, resp.text
    assert agent_runs_route_client.captured["run_tags"] is None  # type: ignore[attr-defined]


def test_agent_runs_route_tags_param_passes_parsed_run_tags(agent_runs_route_client: TestClient) -> None:
    resp = agent_runs_route_client.get("/v1/workflows/wpid_test/runs", params=[("tags", "skyvern.platform:platform_a")])
    assert resp.status_code == 200, resp.text
    assert agent_runs_route_client.captured["run_tags"] == [("skyvern.platform", "platform_a")]  # type: ignore[attr-defined]


def test_agent_runs_route_tags_enforces_workflow_tagging_gate(agent_runs_route_client: TestClient) -> None:
    agent_runs_route_client.app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    resp = agent_runs_route_client.get("/v1/workflows/wpid_test/runs", params=[("tags", "skyvern.platform:platform_a")])
    assert resp.status_code == 403, resp.text


def test_agent_runs_legacy_route_tags_filter_keeps_child_runs(agent_runs_route_client: TestClient) -> None:
    resp = agent_runs_route_client.get(
        "/api/v1/workflows/wpid_test/runs", params=[("tags", "skyvern.platform:platform_a")]
    )
    assert resp.status_code == 200, resp.text
    assert agent_runs_route_client.captured["run_tags"] == [("skyvern.platform", "platform_a")]  # type: ignore[attr-defined]
    assert agent_runs_route_client.captured["exclude_child_runs"] is False  # type: ignore[attr-defined]


def test_agent_runs_legacy_route_tags_enforces_workflow_tagging_gate(agent_runs_route_client: TestClient) -> None:
    agent_runs_route_client.app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)  # type: ignore[attr-defined]
    resp = agent_runs_route_client.get(
        "/api/v1/workflows/wpid_test/runs", params=[("tags", "skyvern.platform:platform_a")]
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_get_run_tag_suggestions_includes_system_keys(
    repo: WorkflowRunsRepository, tags_repo: TagsRepository
) -> None:
    await _insert_workflow_run(repo, workflow_run_id="wr_suggestions")
    await _set_run_tag(repo, workflow_run_id="wr_suggestions", key="skyvern.platform", value="platform_a")

    pairs = await tags_repo.get_run_tag_suggestions(ORG_ID)

    assert ("skyvern.platform", "platform_a") in pairs


@pytest.mark.asyncio
async def test_get_run_tag_suggestions_filters_prefix_before_limit(
    repo: WorkflowRunsRepository, tags_repo: TagsRepository
) -> None:
    await _insert_workflow_run(repo, workflow_run_id="wr_prefix")
    await _set_run_tag(repo, workflow_run_id="wr_prefix", key="custom_a", value="value_a")
    await _set_run_tag(repo, workflow_run_id="wr_prefix", key="custom_b", value="value_b")
    await _set_run_tag(repo, workflow_run_id="wr_prefix", key="skyvern.platform", value="platform_a")

    pairs = await tags_repo.get_run_tag_suggestions(ORG_ID, limit=1, key_prefix="skyvern.")

    assert pairs == [("skyvern.platform", "platform_a")]


@pytest.mark.asyncio
async def test_get_run_tag_suggestions_caps_values_per_key(
    repo: WorkflowRunsRepository, tags_repo: TagsRepository
) -> None:
    for i in range(6):
        await _insert_workflow_run(repo, workflow_run_id=f"wr_hc_{i}")
        await _set_run_tag(repo, workflow_run_id=f"wr_hc_{i}", key="skyvern.high_card", value=f"v{i}")
    await _set_run_tag(repo, workflow_run_id="wr_hc_0", key="team", value="alpha")
    await _set_run_tag(repo, workflow_run_id="wr_hc_1", key="urgent", value="yes")

    pairs = await tags_repo.get_run_tag_suggestions(ORG_ID, limit=5)

    keys = {key for key, _ in pairs}
    assert "team" in keys
    assert "urgent" in keys
    high_card_values = [value for key, value in pairs if key == "skyvern.high_card"]
    assert len(high_card_values) == 3


@pytest.fixture
def run_tag_suggestions_route_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from skyvern.forge.sdk.routes.routers import base_router

    tags_mock = MagicMock()
    tags_mock.get_run_tag_suggestions = AsyncMock(return_value=[])

    app_mock = MagicMock()
    app_mock.DATABASE.tags = tags_mock
    app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=True)

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.dependency_overrides[org_auth_service.get_current_org] = lambda: _make_org()

    from skyvern.forge.sdk.routes import agent_protocol as ap

    monkeypatch.setattr(ap, "app", app_mock)

    client = TestClient(test_app)
    client.app_mock = app_mock  # type: ignore[attr-defined]
    client.tags_mock = tags_mock  # type: ignore[attr-defined]
    return client


def test_run_tag_suggestions_route_partitions_pairs_into_keys_values_and_labels(
    run_tag_suggestions_route_client: TestClient,
) -> None:
    run_tag_suggestions_route_client.tags_mock.get_run_tag_suggestions = AsyncMock(  # type: ignore[attr-defined]
        return_value=[
            ("skyvern.platform", "platform_a"),
            ("skyvern.platform", "platform_b"),
            ("env", "prod"),
            (None, "my-label"),
        ]
    )

    resp = run_tag_suggestions_route_client.get("/v1/run-tag-suggestions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keys"] == ["skyvern.platform", "env"]
    assert body["values_by_key"] == {
        "skyvern.platform": ["platform_a", "platform_b"],
        "env": ["prod"],
    }
    assert body["labels"] == ["my-label"]


def test_run_tag_suggestions_route_skips_pairs_with_null_value(
    run_tag_suggestions_route_client: TestClient,
) -> None:
    run_tag_suggestions_route_client.tags_mock.get_run_tag_suggestions = AsyncMock(  # type: ignore[attr-defined]
        return_value=[("env", None), ("env", "prod")]
    )

    resp = run_tag_suggestions_route_client.get("/v1/run-tag-suggestions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keys"] == ["env"]
    assert body["values_by_key"] == {"env": ["prod"]}
    assert body["labels"] == []


def test_run_tag_suggestions_route_enforces_workflow_tagging_gate(
    run_tag_suggestions_route_client: TestClient,
) -> None:
    run_tag_suggestions_route_client.app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )

    resp = run_tag_suggestions_route_client.get("/v1/run-tag-suggestions")

    assert resp.status_code == 403, resp.text
