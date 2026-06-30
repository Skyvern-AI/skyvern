"""End-to-end tests for the workflow tag HTTP endpoints.

Uses a real ``TagsRepository`` against in-memory SQLite, plus dependency
overrides for auth and a mocked ``workflows.get_workflow_by_permanent_id``
for the existence-check helper. The FastAPI ``base_router`` is mounted on
``/v1`` and ``legacy_base_router`` on ``/api/v1`` to mirror api_app.py.

The public tag API is list-shaped (key-optional): a tag is ``{key?, value}``,
so standalone labels (no key) and grouped labels coexist; responses are lists
of tag objects rather than key-maps.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, WorkflowModel
from skyvern.forge.sdk.db.repositories.tags import TagsRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import CallerType

ORG_ID = "o_test"
OTHER_ORG_ID = "o_other"
WPID = "wpid_alpha"
OTHER_WPID = "wpid_beta"
CALLER_ID = "user_test"


def _keys(tags: list[dict[str, Any]]) -> set[str | None]:
    return {tag["key"] for tag in tags}


def _by_key(tags: list[dict[str, Any]], key: str | None) -> dict[str, Any]:
    match = next((tag for tag in tags if tag["key"] == key), None)
    assert match is not None, f"no tag with key={key!r} in {tags}"
    return match


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # The workflow the tag routes operate on must exist (non-deleted) so the
    # value-count filter can resolve it, mirroring the existence mock below.
    session_maker = async_sessionmaker(eng, expire_on_commit=False)
    async with session_maker() as session:
        session.add(
            WorkflowModel(organization_id=ORG_ID, workflow_permanent_id=WPID, title="t", workflow_definition={})
        )
        await session.commit()
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> TagsRepository:
    db = BaseAlchemyDB(engine)
    return TagsRepository(db.Session, debug_enabled=False)


def _make_org(org_id: str = ORG_ID) -> Organization:
    now = dt.datetime.now(dt.timezone.utc)
    return Organization(
        organization_id=org_id,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture
def app_with_routes(repo: TagsRepository, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Mount the real routers with patched ``app`` module + dependency overrides.

    The route handlers reach the DB via ``app.DATABASE.tags`` and
    ``app.DATABASE.workflows``; we point both at fakes that delegate to the
    in-memory ``TagsRepository`` and a mock workflow lookup respectively.
    """
    from skyvern.forge.sdk.routes import agent_protocol as ap
    from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router

    workflows_mock = MagicMock()

    async def _get_workflow_by_permanent_id(
        workflow_permanent_id: str,
        organization_id: str | None = None,
        **kwargs: object,
    ) -> object | None:
        # Only WPID exists under ORG_ID. Cross-org callers see None.
        if workflow_permanent_id == WPID and organization_id == ORG_ID:
            return MagicMock(workflow_permanent_id=WPID, organization_id=ORG_ID)
        if workflow_permanent_id == OTHER_WPID and organization_id == OTHER_ORG_ID:
            return MagicMock(workflow_permanent_id=OTHER_WPID, organization_id=OTHER_ORG_ID)
        return None

    workflows_mock.get_workflow_by_permanent_id = AsyncMock(side_effect=_get_workflow_by_permanent_id)

    async def _get_existing_permanent_ids(workflow_permanent_ids: list[str], organization_id: str) -> set[str]:
        # Same residency rules as the per-workflow mock, but bulk.
        if organization_id == ORG_ID:
            return {wpid for wpid in workflow_permanent_ids if wpid == WPID}
        if organization_id == OTHER_ORG_ID:
            return {wpid for wpid in workflow_permanent_ids if wpid == OTHER_WPID}
        return set()

    workflows_mock.get_existing_permanent_ids = AsyncMock(side_effect=_get_existing_permanent_ids)

    database_mock = MagicMock()
    database_mock.tags = repo
    database_mock.workflows = workflows_mock

    app_mock = MagicMock()
    app_mock.DATABASE = database_mock
    app_mock.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=True)

    monkeypatch.setattr(ap, "app", app_mock)

    test_app = FastAPI()
    test_app.include_router(base_router, prefix="/v1")
    test_app.include_router(legacy_base_router, prefix="/api/v1")

    def _override_get_current_org() -> Organization:
        return _make_org(ORG_ID)

    def _override_get_current_caller_context() -> org_auth_service.CallerContext:
        return org_auth_service.CallerContext(
            organization=_make_org(ORG_ID),
            caller_id=CALLER_ID,
            caller_type=CallerType.USER,
        )

    test_app.dependency_overrides[org_auth_service.get_current_org] = _override_get_current_org
    test_app.dependency_overrides[org_auth_service.get_current_caller_context] = _override_get_current_caller_context

    return test_app


@pytest.fixture
def client(app_with_routes: FastAPI) -> TestClient:
    return TestClient(app_with_routes)


# ----------------------------- POST /workflows/{wpid}/tags ---------------------------


def test_post_tags_atomic_set_and_delete(client: TestClient) -> None:
    """Set + delete in one call: set wins on collision, delete removes other keys."""
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={
            "tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}],
            "tags_to_delete": [],
        },
    )
    assert resp.status_code == 200, resp.text
    assert _keys(resp.json()["tags"]) == {"env", "team"}

    # Same call: set "env=stage", delete "env" → set wins (same key collision)
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "stage"}], "tags_to_delete": [{"key": "env"}, {"key": "team"}]},
    )
    assert resp.status_code == 200
    tags = resp.json()["tags"]
    # team was deleted; env was kept with new value (set-wins)
    assert _keys(tags) == {"env"}
    assert _by_key(tags, "env")["value"] == "stage"
    assert _by_key(tags, "env")["set_by"] == CALLER_ID


def test_post_tags_empty_noop(client: TestClient) -> None:
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [], "tags_to_delete": []})
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_post_standalone_label(client: TestClient) -> None:
    """A single-string label (no key) is valid and round-trips with a null key."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"value": "production"}]})
    assert resp.status_code == 200, resp.text
    tags = resp.json()["tags"]
    assert len(tags) == 1
    assert tags[0]["key"] is None
    assert tags[0]["value"] == "production"


def test_post_grouped_and_standalone_coexist(client: TestClient) -> None:
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"value": "urgent"}]},
    )
    assert resp.status_code == 200, resp.text
    tags = resp.json()["tags"]
    assert _keys(tags) == {"env", None}
    assert _by_key(tags, None)["value"] == "urgent"


def test_post_tags_invalid_key_returns_422(client: TestClient) -> None:
    """Keys starting with the reserved ``skyvern.`` prefix fail validation."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "skyvern.foo", "value": "bar"}]})
    assert resp.status_code == 422


def test_post_tags_bad_value_char_returns_422(client: TestClient) -> None:
    """Comma in value would break the ``?tags=`` filter encoding."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "team", "value": "a,b"}]})
    assert resp.status_code == 422


def test_post_tags_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.post(
        "/v1/workflows/wpid_does_not_exist/tags",
        json={"tags": [{"key": "env", "value": "prod"}]},
    )
    assert resp.status_code == 404


def test_post_tags_legacy_route_works(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "tags_to_delete": []},
    )
    assert resp.status_code == 200


# ----------------------------- DELETE /workflows/{wpid}/tags/{key} -------------------


def test_delete_tag_supersedes_prior_set(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.delete(f"/v1/workflows/{WPID}/tags/env")
    assert resp.status_code == 200
    assert "env" not in _keys(resp.json()["tags"])


def test_delete_tag_noop_when_absent(client: TestClient) -> None:
    resp = client.delete(f"/v1/workflows/{WPID}/tags/nonexistent")
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_delete_standalone_label_via_body(client: TestClient) -> None:
    """Standalone labels have no key, so they're removed by value through the
    apply endpoint's tags_to_delete."""
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"value": "production"}]})
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [], "tags_to_delete": [{"value": "production"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == []


def test_delete_tag_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.delete("/v1/workflows/wpid_missing/tags/env")
    assert resp.status_code == 404


# ----------------------------- GET /workflows/{wpid}/tags ----------------------------


def test_get_tags_returns_current_state(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}]},
    )
    resp = client.get(f"/v1/workflows/{WPID}/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_permanent_id"] == WPID
    assert _keys(body["tags"]) == {"env", "team"}
    for entry in body["tags"]:
        # Per-tag attribution surfaced (source/set_at/set_by).
        assert entry["source"] == "manual"
        assert entry["set_by"] == CALLER_ID
        assert "set_at" in entry


def test_get_tags_includes_standalone_and_grouped(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"value": "urgent"}]},
    )
    resp = client.get(f"/v1/workflows/{WPID}/tags")
    assert resp.status_code == 200
    tags = resp.json()["tags"]
    assert _keys(tags) == {"env", None}


def test_get_tags_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.get("/v1/workflows/wpid_missing/tags")
    assert resp.status_code == 404


def test_tagging_gate_returns_403_when_disabled(client: TestClient) -> None:
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)
    assert client.get(f"/v1/workflows/{WPID}/tags").status_code == 403
    assert client.get("/v1/tag-keys").status_code == 403
    assert (
        client.post(
            f"/v1/workflows/{WPID}/tags",
            json={"tags": [{"key": "env", "value": "prod"}]},
        ).status_code
        == 403
    )


# ----------------------------- GET /workflows tag-filter gate ------------------------


def test_get_workflows_with_tag_filter_returns_403_when_disabled(client: TestClient) -> None:
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.WORKFLOW_SERVICE.get_workflows_by_organization_id = AsyncMock(return_value=[])
    ap.app.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)
    assert client.get("/v1/workflows?tags=env:prod").status_code == 403


def test_get_workflows_without_tag_filter_succeeds_when_disabled(client: TestClient) -> None:
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.WORKFLOW_SERVICE.get_workflows_by_organization_id = AsyncMock(return_value=[])
    ap.app.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=False)
    assert client.get("/v1/workflows").status_code == 200


def test_get_workflows_with_tag_filter_succeeds_when_enabled(client: TestClient) -> None:
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.WORKFLOW_SERVICE.get_workflows_by_organization_id = AsyncMock(return_value=[])
    ap.app.AGENT_FUNCTION.is_workflow_tagging_enabled = AsyncMock(return_value=True)
    assert client.get("/v1/workflows?tags=env:prod").status_code == 200


# ----------------------------- GET /workflows/{wpid}/tags/history --------------------


def test_get_history_includes_set_and_delete_events(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    client.delete(f"/v1/workflows/{WPID}/tags/env")
    resp = client.get(f"/v1/workflows/{WPID}/tags/history")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 2
    types = {e["event_type"] for e in events}
    assert types == {"set", "delete"}
    # Newest first.
    assert events[0]["event_type"] == "delete"
    # Grouped DELETE row has null value (identified by key); SET row carries the value.
    delete_evt = next(e for e in events if e["event_type"] == "delete")
    set_evt = next(e for e in events if e["event_type"] == "set")
    assert delete_evt["value"] is None
    assert set_evt["value"] == "prod"


def test_get_history_filter_by_key(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}]},
    )
    resp = client.get(f"/v1/workflows/{WPID}/tags/history?key=team")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert all(e["key"] == "team" for e in events)
    assert len(events) == 1


# ----------------------------- GET/PATCH /tag-keys -----------------------------------


def test_list_tag_keys_returns_registered_keys(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}]},
    )
    resp = client.get("/v1/tag-keys")
    assert resp.status_code == 200
    keys = [row["key"] for row in resp.json()]
    assert keys == ["env", "team"]


def test_list_tag_keys_excludes_standalone_labels(client: TestClient) -> None:
    """Standalone labels have no group, so they don't appear in the key registry."""
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"value": "urgent"}]},
    )
    resp = client.get("/v1/tag-keys")
    assert resp.status_code == 200
    assert [row["key"] for row in resp.json()] == ["env"]


def test_patch_tag_key_404_when_unknown(client: TestClient) -> None:
    resp = client.patch("/v1/tag-keys/never_registered", json={"description": "hi"})
    assert resp.status_code == 404


def test_patch_tag_key_updates_description(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/v1/tag-keys/env", json={"description": "deployment environment"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "deployment environment"


def test_patch_tag_key_description_too_long_returns_422(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/v1/tag-keys/env", json={"description": "x" * 501})
    assert resp.status_code == 422


def test_list_tag_keys_includes_workflow_count(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}]},
    )
    resp = client.get("/v1/tag-keys")
    assert resp.status_code == 200
    by_key = {row["key"]: row for row in resp.json()}
    assert by_key["env"]["workflow_count"] == 1
    assert by_key["team"]["workflow_count"] == 1


# ----------------------------- DELETE /tag-keys/{key} --------------------------------


def test_delete_tag_key_removes_from_registry_and_workflow(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}]},
    )
    resp = client.delete("/v1/tag-keys/env")

    assert resp.status_code == 200
    assert resp.json() == {"key": "env", "removed_from_workflow_count": 1}
    assert [row["key"] for row in client.get("/v1/tag-keys").json()] == ["team"]
    assert _keys(client.get(f"/v1/workflows/{WPID}/tags").json()["tags"]) == {"team"}


def test_delete_tag_key_404_when_unknown(client: TestClient) -> None:
    resp = client.delete("/v1/tag-keys/never_registered")
    assert resp.status_code == 404


def test_delete_tag_key_reserved_prefix_returns_400(client: TestClient) -> None:
    resp = client.delete("/v1/tag-keys/skyvern.system")
    assert resp.status_code == 400


def test_delete_tag_key_legacy_route_works(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.delete("/api/v1/tag-keys/env")
    assert resp.status_code == 200
    assert resp.json()["removed_from_workflow_count"] == 1


# ----------------------------- Batch endpoints ---------------------------------------


def test_batch_get_returns_map_keyed_by_wpid(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: [{"key": "env", "value": "prod"}]}


def test_batch_get_includes_standalone_labels_in_stable_order(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "team", "value": "growth"}, {"key": "env", "value": "prod"}, {"value": "urgent"}]},
    )
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    tags = resp.json()["workflow_tags"][WPID]
    # Deterministic: standalone labels first, then grouped by key, then value.
    assert tags == [
        {"key": None, "value": "urgent"},
        {"key": "env", "value": "prod"},
        {"key": "team", "value": "growth"},
    ]


def test_batch_get_returns_empty_list_for_workflow_with_no_tags(client: TestClient) -> None:
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: []}


def test_batch_get_cross_org_isolation(client: TestClient, repo: TagsRepository) -> None:
    """Tags belonging to a different org must not appear in the response."""
    import asyncio

    from skyvern.forge.sdk.workflow.models.tags import TagSource, TagWriteContext

    # Plant a tag in a different org for OTHER_WPID.
    asyncio.run(
        repo.apply_tag_changes(
            workflow_permanent_id=OTHER_WPID,
            organization_id=OTHER_ORG_ID,
            sets={"secret": "leak_me"},
            deletes=set(),
            context=TagWriteContext(
                caller_id="user_other",
                source=TagSource.MANUAL,
                caller_type=CallerType.USER,
            ),
        )
    )
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID},{OTHER_WPID}")
    assert resp.status_code == 200
    body = resp.json()["workflow_tags"]
    # OTHER_WPID is echoed (since the caller asked for it) but carries no
    # values — cross-org filter in the repo zeroes it out.
    assert body[OTHER_WPID] == []
    assert body[WPID] == []


def test_batch_post_accepts_body(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.post("/v1/workflow-tags", json={"workflow_permanent_ids": [WPID]})
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: [{"key": "env", "value": "prod"}]}


def test_batch_get_legacy_route_works(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.get(f"/api/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200


# --------------------- Shape/namespace guards (CORR-1, CORR-3, CORR-4, RISK-1, RISK-3, RISK-4) ---------------------


def test_post_tags_dict_body_returns_422(client: TestClient) -> None:
    """CORR-1: the old dict shape must fail cleanly (422), not coerce."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    assert resp.status_code == 422


def test_post_tags_bare_string_items_return_422(client: TestClient) -> None:
    """CORR-1: list items must be {key?, value} objects, not bare strings."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": ["env"]})
    assert resp.status_code == 422


def test_post_tags_non_string_value_returns_422(client: TestClient) -> None:
    """CORR-1: JSON int values must surface as 422, not crash inside value handling."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": 1}]})
    assert resp.status_code == 422


def test_post_tags_to_delete_as_string_returns_422(client: TestClient) -> None:
    """CORR-1: a string tags_to_delete must 422, not iterate char-by-char."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags_to_delete": "env"})
    assert resp.status_code == 422


def test_post_tags_to_delete_empty_target_returns_422(client: TestClient) -> None:
    """A delete target with neither key nor value identifies nothing."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags_to_delete": [{}]})
    assert resp.status_code == 422


def test_post_tags_to_delete_reserved_namespace_returns_422(client: TestClient) -> None:
    """CORR-3: skyvern.* keys are blocked on SET; the same boundary applies to
    the body-DELETE path."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags_to_delete": [{"key": "skyvern.managed"}]})
    assert resp.status_code == 422


def test_post_tags_to_delete_over_cap_returns_422(client: TestClient) -> None:
    """RISK-1: tags_to_delete must be capped at the same scale as `tags`."""
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags_to_delete": [{"value": f"v{i}"} for i in range(21)]},
    )
    assert resp.status_code == 422


def test_delete_path_reserved_namespace_returns_400(client: TestClient) -> None:
    """CORR-3: URL-path-keys violating the namespace must return 400 (not 422)
    because the offending value is in the path, not the body."""
    resp = client.delete(f"/v1/workflows/{WPID}/tags/skyvern.managed")
    assert resp.status_code == 400


def test_delete_path_bad_shape_returns_400(client: TestClient) -> None:
    """CORR-3: same path-level rule for shape (regex) violations."""
    resp = client.delete(f"/v1/workflows/{WPID}/tags/.leading-dot")
    assert resp.status_code == 400


def test_patch_tag_key_reserved_namespace_returns_400(client: TestClient) -> None:
    """CORR-4: same namespace boundary on the registry PATCH surface."""
    resp = client.patch("/v1/tag-keys/skyvern.managed", json={"description": "hi"})
    assert resp.status_code == 400


def test_patch_tag_key_bad_shape_returns_400(client: TestClient) -> None:
    resp = client.patch("/v1/tag-keys/.bad-key", json={"description": "hi"})
    assert resp.status_code == 400


def test_patch_tag_key_description_non_string_returns_422(client: TestClient) -> None:
    """CORR-1: TagKeyUpdate.description type guard for non-str payloads."""
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/v1/tag-keys/env", json={"description": 123})
    assert resp.status_code == 422


def test_batch_get_drops_workflow_deleted_between_checks(
    monkeypatch: pytest.MonkeyPatch, app_with_routes: FastAPI, repo: TagsRepository
) -> None:
    """RISK-4: the double-check pattern must drop a wpid that was soft-deleted
    *between* the pre-read existence query and the post-read existence query."""
    import asyncio

    from skyvern.forge.sdk.workflow.models.tags import TagSource, TagWriteContext

    # Plant a tag so the read returns something to filter.
    asyncio.run(
        repo.apply_tag_changes(
            workflow_permanent_id=WPID,
            organization_id=ORG_ID,
            sets={"env": "prod"},
            deletes=set(),
            context=TagWriteContext(caller_id=CALLER_ID, source=TagSource.MANUAL, caller_type=CallerType.USER),
        )
    )

    call_count = {"n": 0}

    async def _existence_check_then_disappear(workflow_permanent_ids: list[str], organization_id: str) -> set[str]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {WPID}  # first check: workflow still present
        return set()  # second check: workflow was soft-deleted in between

    app_with_routes.dependency_overrides  # ensure fixture build
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.DATABASE.workflows.get_existing_permanent_ids = AsyncMock(side_effect=_existence_check_then_disappear)

    client = TestClient(app_with_routes)
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    # Final response must NOT carry the now-deleted workflow's tags.
    assert resp.json()["workflow_tags"] == {WPID: []}


def test_post_returns_409_when_integrity_error_persists(app_with_routes: FastAPI) -> None:
    """RISK-3: two consecutive IntegrityErrors from apply_tag_changes surface
    as 409 Conflict, not 500."""
    from sqlalchemy.exc import IntegrityError

    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.DATABASE.tags.apply_tag_changes = AsyncMock(
        side_effect=IntegrityError("statement", {}, Exception("conflict"))
    )

    client = TestClient(app_with_routes)
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    assert resp.status_code == 409


def test_post_succeeds_when_first_integrity_error_then_succeeds(app_with_routes: FastAPI) -> None:
    """RISK-3: the retry is actually exercised — first attempt raises, second
    succeeds, response is 200."""
    from sqlalchemy.exc import IntegrityError

    from skyvern.forge.sdk.routes import agent_protocol as ap

    attempts = {"n": 0}

    async def _flaky(*args: object, **kwargs: object) -> list:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise IntegrityError("statement", {}, Exception("conflict"))
        return []

    ap.app.DATABASE.tags.apply_tag_changes = AsyncMock(side_effect=_flaky)
    # _build_tags_response also reaches the repo — stub the read path so the
    # 200 response can render after the retry succeeds.
    ap.app.DATABASE.tags.get_active_tag_events_for_workflow = AsyncMock(return_value=[])

    client = TestClient(app_with_routes)
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    assert resp.status_code == 200
    assert attempts["n"] == 2


# ----------------------------- GET /tag-values -------------------------------------


def test_get_tag_values_reflects_applied_colors(client: TestClient) -> None:
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={
            "tags": [{"key": "env", "value": "prod"}, {"key": "team", "value": "growth"}],
            "colors": {"env": "blue", "team": "purple"},
        },
    )
    assert resp.status_code == 200, resp.text

    values = client.get("/v1/tag-values").json()
    by_pair = {(row["key"], row["value"]): row["color"] for row in values}
    assert by_pair == {("env", "prod"): "blue", ("team", "growth"): "purple"}


def test_get_tag_values_assigns_random_palette_color_when_unset(client: TestClient) -> None:
    from skyvern.forge.sdk.workflow.models.validators import TAG_COLOR_PALETTE

    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})

    values = client.get("/v1/tag-values").json()
    assert len(values) == 1
    assert values[0]["color"] in TAG_COLOR_PALETTE


def test_get_tag_values_excludes_standalone_labels(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"value": "urgent"}]})
    assert client.get("/v1/tag-values").json() == []


def test_get_tag_values_legacy_route_works(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "green"}},
    )
    resp = client.get("/api/v1/tag-values")
    assert resp.status_code == 200
    assert resp.json()[0]["color"] == "green"


def test_post_tags_invalid_color_returns_422(client: TestClient) -> None:
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "#ff0000"}},
    )
    assert resp.status_code == 422


def test_post_tags_color_is_case_insensitive(client: TestClient) -> None:
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "BLUE"}},
    )
    assert resp.status_code == 200, resp.text
    assert client.get("/v1/tag-values").json()[0]["color"] == "blue"


# ----------------------------- PATCH /tag-values/{key} -----------------------------


def test_patch_tag_value_recolors(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "blue"}},
    )
    resp = client.patch("/v1/tag-values/env", json={"value": "prod", "color": "pink"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "env", "value": "prod", "color": "pink", "workflow_count": 1}
    assert client.get("/v1/tag-values").json()[0]["color"] == "pink"


def test_patch_tag_value_recolors_value_containing_slash(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "region", "value": "us/west"}], "colors": {"region": "blue"}},
    )
    resp = client.patch("/v1/tag-values/region", json={"value": "us/west", "color": "pink"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "region", "value": "us/west", "color": "pink", "workflow_count": 1}


def test_patch_tag_value_404_when_absent(client: TestClient) -> None:
    resp = client.patch("/v1/tag-values/env", json={"value": "prod", "color": "pink"})
    assert resp.status_code == 404


def test_patch_tag_value_invalid_color_returns_422(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "blue"}},
    )
    resp = client.patch("/v1/tag-values/env", json={"value": "prod", "color": "chartreuse"})
    assert resp.status_code == 422


def test_patch_tag_value_reserved_prefix_returns_400(client: TestClient) -> None:
    resp = client.patch("/v1/tag-values/skyvern.system", json={"value": "prod", "color": "blue"})
    assert resp.status_code == 400


# ----------------------------- GET /tag-values workflow_count ----------------------


def test_get_tag_values_includes_workflow_count(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "blue"}},
    )
    values = client.get("/v1/tag-values").json()
    assert len(values) == 1
    assert values[0]["workflow_count"] == 1


# ----------------------------- PATCH /tag-values/{key}/rename -----------------------


def test_rename_tag_value_cascades_and_carries_color(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "blue"}},
    )
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "key": "env",
        "value": "production",
        "color": "blue",
        "renamed_workflow_count": 1,
    }
    # The value list now reflects the new label, color carried over.
    values = client.get("/v1/tag-values").json()
    assert [(v["value"], v["color"]) for v in values] == [("production", "blue")]


def test_rename_tag_value_404_when_absent(client: TestClient) -> None:
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 404


def test_rename_tag_value_409_on_collision(client: TestClient) -> None:
    # One workflow holds one value per key, so SET stg then prod: both color rows
    # stay registered org-wide, making stg a rename collision target for prod.
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "stg"}]})
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "stg"})
    assert resp.status_code == 409


def test_rename_tag_value_422_when_same_value(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "prod"})
    assert resp.status_code == 422


def test_rename_tag_value_reserved_prefix_returns_400(client: TestClient) -> None:
    resp = client.patch("/v1/tag-values/skyvern.system/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 400


def test_rename_tag_value_handles_value_with_slash(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "region", "value": "us/west"}], "colors": {"region": "blue"}},
    )
    resp = client.patch("/v1/tag-values/region/rename", json={"value": "us/west", "new_value": "us/east"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == "us/east"
    assert resp.json()["color"] == "blue"


def test_rename_tag_value_legacy_route_works(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.patch("/api/v1/tag-values/env/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["renamed_workflow_count"] == 1


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("duplicate active SET"))


def test_rename_tag_value_retries_then_succeeds_on_transient_integrity_error(
    repo: TagsRepository, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    real_rename = repo.rename_tag_value
    calls = {"n": 0}

    async def _flaky_rename(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _integrity_error()
        return await real_rename(*args, **kwargs)

    monkeypatch.setattr(repo, "rename_tag_value", _flaky_rename)
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 200, resp.text
    assert calls["n"] == 2


def test_rename_tag_value_409_on_persistent_integrity_error(
    repo: TagsRepository, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})

    async def _always_conflict(*args: Any, **kwargs: Any) -> Any:
        raise _integrity_error()

    monkeypatch.setattr(repo, "rename_tag_value", _always_conflict)
    resp = client.patch("/v1/tag-values/env/rename", json={"value": "prod", "new_value": "production"})
    assert resp.status_code == 409


# ----------------------------- DELETE /tag-values/{key} ----------------------------


def test_delete_tag_value_cascades_and_returns_count(client: TestClient) -> None:
    client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": [{"key": "env", "value": "prod"}], "colors": {"env": "blue"}},
    )
    resp = client.request("DELETE", "/v1/tag-values/env", json={"value": "prod"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "env", "value": "prod", "removed_from_workflow_count": 1}
    # The label is gone from the workflow and from the value registry.
    assert client.get(f"/v1/workflows/{WPID}/tags").json()["tags"] == []
    assert client.get("/v1/tag-values").json() == []


def test_delete_tag_value_404_when_absent(client: TestClient) -> None:
    resp = client.request("DELETE", "/v1/tag-values/env", json={"value": "prod"})
    assert resp.status_code == 404


def test_delete_tag_value_reserved_prefix_returns_400(client: TestClient) -> None:
    resp = client.request("DELETE", "/v1/tag-values/skyvern.system", json={"value": "prod"})
    assert resp.status_code == 400


def test_delete_tag_value_handles_value_with_slash(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "region", "value": "us/west"}]})
    resp = client.request("DELETE", "/v1/tag-values/region", json={"value": "us/west"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_from_workflow_count"] == 1


def test_delete_tag_value_legacy_route_works(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": [{"key": "env", "value": "prod"}]})
    resp = client.request("DELETE", "/api/v1/tag-values/env", json={"value": "prod"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_from_workflow_count"] == 1
