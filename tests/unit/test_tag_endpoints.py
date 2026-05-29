"""End-to-end tests for the workflow tag HTTP endpoints (Phase 3).

Uses a real ``TagsRepository`` against in-memory SQLite, plus dependency
overrides for auth and a mocked ``workflows.get_workflow_by_permanent_id``
for the existence-check helper. The FastAPI ``base_router`` is mounted on
``/v1`` and ``legacy_base_router`` on ``/api/v1`` to mirror api_app.py.
"""

from __future__ import annotations

import datetime as dt
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.db.repositories.tags import TagsRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import CallerType

ORG_ID = "o_test"
OTHER_ORG_ID = "o_other"
WPID = "wpid_alpha"
OTHER_WPID = "wpid_beta"
CALLER_ID = "user_test"


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
        json={"tags": {"env": "prod", "team": "growth"}, "tags_to_delete": []},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["tags"].keys()) == {"env", "team"}

    # Same call: set "env=stage", delete "env" → set wins (same key collision)
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags": {"env": "stage"}, "tags_to_delete": ["env", "team"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # team was deleted; env was kept with new value (set-wins)
    assert set(body["tags"].keys()) == {"env"}
    assert body["tags"]["env"]["value"] == "stage"
    assert body["tags"]["env"]["set_by"] == CALLER_ID


def test_post_tags_empty_noop(client: TestClient) -> None:
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {}, "tags_to_delete": []})
    assert resp.status_code == 200
    assert resp.json()["tags"] == {}


def test_post_tags_invalid_key_returns_422(client: TestClient) -> None:
    """Keys starting with the reserved ``skyvern.`` prefix fail validation."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"skyvern.foo": "bar"}})
    assert resp.status_code == 422


def test_post_tags_bad_value_char_returns_422(client: TestClient) -> None:
    """Comma in value would break the ``?tags=k:v,k2:v2`` filter encoding."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"team": "a,b"}})
    assert resp.status_code == 422


def test_post_tags_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.post(
        "/v1/workflows/wpid_does_not_exist/tags",
        json={"tags": {"env": "prod"}},
    )
    assert resp.status_code == 404


def test_post_tags_legacy_route_works(client: TestClient) -> None:
    resp = client.post(f"/api/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}, "tags_to_delete": []})
    assert resp.status_code == 200


# ----------------------------- DELETE /workflows/{wpid}/tags/{key} -------------------


def test_delete_tag_supersedes_prior_set(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.delete(f"/v1/workflows/{WPID}/tags/env")
    assert resp.status_code == 200
    assert "env" not in resp.json()["tags"]


def test_delete_tag_noop_when_absent(client: TestClient) -> None:
    resp = client.delete(f"/v1/workflows/{WPID}/tags/nonexistent")
    assert resp.status_code == 200
    assert resp.json()["tags"] == {}


def test_delete_tag_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.delete("/v1/workflows/wpid_missing/tags/env")
    assert resp.status_code == 404


# ----------------------------- GET /workflows/{wpid}/tags ----------------------------


def test_get_tags_returns_current_state(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod", "team": "growth"}})
    resp = client.get(f"/v1/workflows/{WPID}/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_permanent_id"] == WPID
    assert set(body["tags"].keys()) == {"env", "team"}
    for entry in body["tags"].values():
        # Per-tag attribution surfaced (source/set_at/set_by).
        assert entry["source"] == "manual"
        assert entry["set_by"] == CALLER_ID
        assert "set_at" in entry


def test_get_tags_404_when_workflow_not_in_org(client: TestClient) -> None:
    resp = client.get("/v1/workflows/wpid_missing/tags")
    assert resp.status_code == 404


# ----------------------------- GET /workflows/{wpid}/tags/history --------------------


def test_get_history_includes_set_and_delete_events(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    client.delete(f"/v1/workflows/{WPID}/tags/env")
    resp = client.get(f"/v1/workflows/{WPID}/tags/history")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 2
    types = {e["event_type"] for e in events}
    assert types == {"set", "delete"}
    # Newest first.
    assert events[0]["event_type"] == "delete"
    # DELETE row has null value; SET row carries the value.
    delete_evt = next(e for e in events if e["event_type"] == "delete")
    set_evt = next(e for e in events if e["event_type"] == "set")
    assert delete_evt["value"] is None
    assert set_evt["value"] == "prod"


def test_get_history_filter_by_key(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod", "team": "growth"}})
    resp = client.get(f"/v1/workflows/{WPID}/tags/history?key=team")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert all(e["key"] == "team" for e in events)
    assert len(events) == 1


# ----------------------------- GET/PATCH /tag-keys -----------------------------------


def test_list_tag_keys_returns_registered_keys(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod", "team": "growth"}})
    resp = client.get("/v1/tag-keys")
    assert resp.status_code == 200
    keys = [row["key"] for row in resp.json()]
    assert keys == ["env", "team"]


def test_patch_tag_key_404_when_unknown(client: TestClient) -> None:
    resp = client.patch("/v1/tag-keys/never_registered", json={"description": "hi"})
    assert resp.status_code == 404


def test_patch_tag_key_updates_description(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.patch("/v1/tag-keys/env", json={"description": "deployment environment"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "deployment environment"


def test_patch_tag_key_description_too_long_returns_422(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.patch("/v1/tag-keys/env", json={"description": "x" * 501})
    assert resp.status_code == 422


# ----------------------------- Batch endpoints ---------------------------------------


def test_batch_get_returns_map_keyed_by_wpid(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: {"env": "prod"}}


def test_batch_get_returns_empty_dict_for_workflow_with_no_tags(client: TestClient) -> None:
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: {}}


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
    assert body[OTHER_WPID] == {}
    assert body[WPID] == {}


def test_batch_post_accepts_body(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.post("/v1/workflow-tags", json={"workflow_permanent_ids": [WPID]})
    assert resp.status_code == 200
    assert resp.json()["workflow_tags"] == {WPID: {"env": "prod"}}


def test_batch_get_legacy_route_works(client: TestClient) -> None:
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.get(f"/api/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200


# --------------------- Debated-plan fixes (CORR-1, CORR-3, CORR-4, RISK-1, RISK-3, RISK-4) ---------------------


def test_post_tags_list_body_returns_422(client: TestClient) -> None:
    """CORR-1: outer-shape guard. A list body where the schema expects a dict
    must return 422, not 500 (leaking AttributeError) and not 200."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": ["env"]})
    assert resp.status_code == 422


def test_post_tags_non_string_value_returns_422(client: TestClient) -> None:
    """CORR-1: inner-element guard. JSON int values must surface as 422 with
    a clean message instead of crashing inside `value.strip()`."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": 1}})
    assert resp.status_code == 422


def test_post_tags_to_delete_as_string_returns_422(client: TestClient) -> None:
    """CORR-1: critical silent-bug fix. Without the explicit list/tuple guard,
    `tags_to_delete: "env"` would silently iterate to ['e', 'n', 'v']."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags_to_delete": "env"})
    assert resp.status_code == 422


def test_post_tags_to_delete_reserved_namespace_returns_422(client: TestClient) -> None:
    """CORR-3: skyvern.* keys are blocked on SET via normalize_tags. The same
    boundary must apply to the body-DELETE path."""
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags_to_delete": ["skyvern.managed"]})
    assert resp.status_code == 422


def test_post_tags_to_delete_over_cap_returns_422(client: TestClient) -> None:
    """RISK-1: tags_to_delete must be capped at the same scale as `tags`."""
    resp = client.post(
        f"/v1/workflows/{WPID}/tags",
        json={"tags_to_delete": [f"k{i}" for i in range(21)]},
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
    """CORR-4: same namespace boundary on the registry PATCH surface — once
    Phase 4/5 system writers populate skyvern.* keys, the public PATCH API
    must not let callers rewrite their descriptions."""
    resp = client.patch("/v1/tag-keys/skyvern.managed", json={"description": "hi"})
    assert resp.status_code == 400


def test_patch_tag_key_bad_shape_returns_400(client: TestClient) -> None:
    resp = client.patch("/v1/tag-keys/.bad-key", json={"description": "hi"})
    assert resp.status_code == 400


def test_patch_tag_key_description_non_string_returns_422(client: TestClient) -> None:
    """CORR-1: TagKeyUpdate.description type guard for non-str payloads."""
    client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    resp = client.patch("/v1/tag-keys/env", json={"description": 123})
    assert resp.status_code == 422


def test_batch_get_drops_workflow_deleted_between_checks(
    monkeypatch: pytest.MonkeyPatch, app_with_routes: FastAPI, repo: TagsRepository
) -> None:
    """RISK-4: the double-check pattern must drop a wpid that was soft-deleted
    *between* the pre-read existence query and the post-read existence query.
    Simulated by overriding get_existing_permanent_ids to return the wpid on
    the first call and an empty set on the second."""
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
    # Override the get_existing_permanent_ids on the patched workflows mock.
    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.DATABASE.workflows.get_existing_permanent_ids = AsyncMock(side_effect=_existence_check_then_disappear)

    client = TestClient(app_with_routes)
    resp = client.get(f"/v1/workflow-tags?workflow_permanent_ids={WPID}")
    assert resp.status_code == 200
    # Final response must NOT carry the now-deleted workflow's tags.
    assert resp.json()["workflow_tags"] == {WPID: {}}


def test_post_returns_409_when_integrity_error_persists(app_with_routes: FastAPI) -> None:
    """RISK-3: two consecutive IntegrityErrors from apply_tag_changes surface
    as 409 Conflict, not 500. Documented same-workflow/same-key race from
    Phase 2 should never produce an unstructured server error."""
    from sqlalchemy.exc import IntegrityError

    from skyvern.forge.sdk.routes import agent_protocol as ap

    ap.app.DATABASE.tags.apply_tag_changes = AsyncMock(
        side_effect=IntegrityError("statement", {}, Exception("conflict"))
    )

    client = TestClient(app_with_routes)
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
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
    resp = client.post(f"/v1/workflows/{WPID}/tags", json={"tags": {"env": "prod"}})
    assert resp.status_code == 200
    assert attempts["n"] == 2
