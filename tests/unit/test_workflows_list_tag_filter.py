"""Tag-filter tests for the GET /workflows ?tags= filter.

Two layers:
- Repository tests run against in-memory SQLite so the per-key semi-join SQL
  executes against real rows — the only way to verify superseded/soft-delete
  exclusion and the AND-across-keys / OR-within-key semantics.
- Route tests mount the real FastAPI router with a mocked WORKFLOW_SERVICE so
  the ?tags= query param registration and key:value parsing (incl. 400s) are
  exercised — repo-only tests can't catch route-registration or parser bugs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, WorkflowModel, WorkflowTagEventModel
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import TagEventType, TagSource

ORG_ID = "o_test"
OTHER_ORG_ID = "o_other"


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> WorkflowsRepository:
    db = BaseAlchemyDB(engine)
    return WorkflowsRepository(db.Session, debug_enabled=False)


async def _create_workflow(
    repo: WorkflowsRepository,
    *,
    wpid: str,
    title: str,
    organization_id: str = ORG_ID,
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id=f"wid_{wpid}",
                workflow_permanent_id=wpid,
                organization_id=organization_id,
                title=title,
                workflow_definition={"blocks": [], "parameters": []},
                status="published",
                version=1,
            )
        )
        await session.commit()


async def _set_tag(
    repo: WorkflowsRepository,
    *,
    wpid: str,
    key: str,
    value: str,
    organization_id: str = ORG_ID,
    superseded_at: datetime | None = None,
    deleted_at: datetime | None = None,
    event_type: TagEventType = TagEventType.SET,
) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowTagEventModel(
                workflow_permanent_id=wpid,
                organization_id=organization_id,
                key=key,
                value=value if event_type == TagEventType.SET else None,
                event_type=event_type.value,
                set_at=datetime.now(timezone.utc),
                set_by="user_test",
                source=TagSource.MANUAL.value,
                superseded_at=superseded_at,
                deleted_at=deleted_at,
            )
        )
        await session.commit()


async def _wpids(repo: WorkflowsRepository, **kwargs) -> set[str]:
    workflows = await repo.get_workflows_by_organization_id(organization_id=ORG_ID, **kwargs)
    return {w.workflow_permanent_id for w in workflows}


@pytest.mark.asyncio
async def test_no_tags_param_returns_all(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _create_workflow(repo, wpid="wpid_b", title="B")

    assert await _wpids(repo) == {"wpid_a", "wpid_b"}
    assert await _wpids(repo, workflow_tags=None) == {"wpid_a", "wpid_b"}
    assert await _wpids(repo, workflow_tags=[]) == {"wpid_a", "wpid_b"}


@pytest.mark.asyncio
async def test_single_key_single_value(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _create_workflow(repo, wpid="wpid_b", title="B")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod")
    await _set_tag(repo, wpid="wpid_b", key="env", value="staging")

    assert await _wpids(repo, workflow_tags=[("env", "prod")]) == {"wpid_a"}


@pytest.mark.asyncio
async def test_or_within_key(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _create_workflow(repo, wpid="wpid_b", title="B")
    await _create_workflow(repo, wpid="wpid_c", title="C")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod")
    await _set_tag(repo, wpid="wpid_b", key="env", value="staging")
    await _set_tag(repo, wpid="wpid_c", key="env", value="dev")

    result = await _wpids(repo, workflow_tags=[("env", "prod"), ("env", "staging")])
    assert result == {"wpid_a", "wpid_b"}


@pytest.mark.asyncio
async def test_and_across_keys(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_both", title="Both")
    await _create_workflow(repo, wpid="wpid_env_only", title="EnvOnly")
    await _create_workflow(repo, wpid="wpid_cust_only", title="CustOnly")
    await _set_tag(repo, wpid="wpid_both", key="env", value="prod")
    await _set_tag(repo, wpid="wpid_both", key="customer", value="acme")
    await _set_tag(repo, wpid="wpid_env_only", key="env", value="prod")
    await _set_tag(repo, wpid="wpid_cust_only", key="customer", value="acme")

    result = await _wpids(repo, workflow_tags=[("customer", "acme"), ("env", "prod")])
    assert result == {"wpid_both"}


@pytest.mark.asyncio
async def test_superseded_value_excluded(repo: WorkflowsRepository) -> None:
    """Re-tagged env:prod -> env:staging must not match the stale prod value."""
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod", superseded_at=datetime.now(timezone.utc))
    await _set_tag(repo, wpid="wpid_a", key="env", value="staging")

    assert await _wpids(repo, workflow_tags=[("env", "prod")]) == set()
    assert await _wpids(repo, workflow_tags=[("env", "staging")]) == {"wpid_a"}


@pytest.mark.asyncio
async def test_deleted_tag_excluded(repo: WorkflowsRepository) -> None:
    """A soft-deleted (deleted_at set) tag event row must not match."""
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod", deleted_at=datetime.now(timezone.utc))

    assert await _wpids(repo, workflow_tags=[("env", "prod")]) == set()


@pytest.mark.asyncio
async def test_delete_event_excluded(repo: WorkflowsRepository) -> None:
    """A DELETE event row (value NULL, event_type='delete') must not match."""
    await _create_workflow(repo, wpid="wpid_a", title="A")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod", superseded_at=datetime.now(timezone.utc))
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod", event_type=TagEventType.DELETE)

    assert await _wpids(repo, workflow_tags=[("env", "prod")]) == set()


@pytest.mark.asyncio
async def test_cross_org_isolation(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_mine", title="Mine", organization_id=ORG_ID)
    await _set_tag(repo, wpid="wpid_mine", key="env", value="prod", organization_id=ORG_ID)
    # Different org, same tag — must not leak into ORG_ID's results.
    await _create_workflow(repo, wpid="wpid_theirs", title="Theirs", organization_id=OTHER_ORG_ID)
    await _set_tag(repo, wpid="wpid_theirs", key="env", value="prod", organization_id=OTHER_ORG_ID)

    assert await _wpids(repo, workflow_tags=[("env", "prod")]) == {"wpid_mine"}


@pytest.mark.asyncio
async def test_tag_filter_composes_with_search_key(repo: WorkflowsRepository) -> None:
    await _create_workflow(repo, wpid="wpid_a", title="alpha")
    await _create_workflow(repo, wpid="wpid_b", title="beta")
    await _set_tag(repo, wpid="wpid_a", key="env", value="prod")
    await _set_tag(repo, wpid="wpid_b", key="env", value="prod")

    result = await _wpids(repo, workflow_tags=[("env", "prod")], search_key="alpha")
    assert result == {"wpid_a"}


def _make_org(org_id: str = ORG_ID) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=org_id,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture
def route_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Mount the real GET /workflows route with a mocked WORKFLOW_SERVICE.

    Captures the workflow_tags value the route passes to the service so the
    ?tags= registration + key:value parsing (including 400s) are tested end to
    end. Importing the router module also guards against the route-registration
    failure caught in debate round 1 — a bad param declaration raises at import.
    """
    from skyvern.forge.sdk.routes import agent_protocol as ap
    from skyvern.forge.sdk.routes.routers import base_router

    captured: dict[str, object] = {}

    async def _get_workflows_by_organization_id(**kwargs: object) -> list:
        captured["workflow_tags"] = kwargs.get("workflow_tags")
        return []

    service_mock = MagicMock()
    service_mock.get_workflows_by_organization_id = AsyncMock(side_effect=_get_workflows_by_organization_id)

    app_mock = MagicMock()
    app_mock.WORKFLOW_SERVICE = service_mock
    # template=true path: empty global list short-circuits to [] before any service call.
    app_mock.STORAGE.retrieve_global_workflows = AsyncMock(return_value=[])
    monkeypatch.setattr(ap, "app", app_mock)

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.dependency_overrides[org_auth_service.get_current_org] = lambda: _make_org(ORG_ID)

    client = TestClient(test_app)
    client.captured = captured  # type: ignore[attr-defined]
    return client


def test_route_no_tags_param_passes_none(route_client: TestClient) -> None:
    resp = route_client.get("/v1/workflows")
    assert resp.status_code == 200, resp.text
    assert route_client.captured["workflow_tags"] is None  # type: ignore[attr-defined]


def test_route_empty_tags_is_noop(route_client: TestClient) -> None:
    resp = route_client.get("/v1/workflows", params=[("tags", "")])
    assert resp.status_code == 200, resp.text
    assert route_client.captured["workflow_tags"] is None  # type: ignore[attr-defined]


def test_route_comma_and_repeated_parse_identically(route_client: TestClient) -> None:
    comma = route_client.get("/v1/workflows", params=[("tags", "customer:acme,env:prod")])
    assert comma.status_code == 200, comma.text
    comma_tags = route_client.captured["workflow_tags"]  # type: ignore[attr-defined]

    repeated = route_client.get("/v1/workflows", params=[("tags", "customer:acme"), ("tags", "env:prod")])
    assert repeated.status_code == 200, repeated.text
    repeated_tags = route_client.captured["workflow_tags"]  # type: ignore[attr-defined]

    assert comma_tags == [("customer", "acme"), ("env", "prod")]
    assert repeated_tags == comma_tags


def test_route_value_keeps_later_colons(route_client: TestClient) -> None:
    resp = route_client.get("/v1/workflows", params=[("tags", "url:http://x:8000")])
    assert resp.status_code == 200, resp.text
    assert route_client.captured["workflow_tags"] == [("url", "http://x:8000")]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "tags_params",
    [
        [("tags", "envprod")],  # no colon
        [("tags", ":prod")],  # empty key
        [("tags", "env:")],  # empty value
        [("tags", "env:prod,")],  # trailing blank segment (comma form)
        [("tags", "env:prod,,customer:acme")],  # interior blank segment
        [("tags", "env:prod"), ("tags", "")],  # blank repeated param (must match comma form -> CORR-3)
    ],
)
def test_route_malformed_tags_400(route_client: TestClient, tags_params: list[tuple[str, str]]) -> None:
    resp = route_client.get("/v1/workflows", params=tags_params)
    assert resp.status_code == 400, resp.text


def test_route_tags_with_template_rejected(route_client: TestClient) -> None:
    # Templates are global; tags are org-scoped, so combining them is a 400.
    resp = route_client.get("/v1/workflows", params=[("tags", "env:prod"), ("template", "true")])
    assert resp.status_code == 400, resp.text


def test_route_template_without_tags_allowed(route_client: TestClient) -> None:
    # template=true alone must still work (no tags filter applied).
    resp = route_client.get("/v1/workflows", params=[("template", "true")])
    assert resp.status_code == 200, resp.text
