"""Regression tests for ``WorkflowsRepository.update_workflow_and_reconcile_definition_params``.

Covers the 11 workflow-level fields that were newly threaded through
``WorkflowService.update_workflow_definition`` in the copilot-v2 stack:

- ``_UNSET``-guarded (6): ``proxy_location``, ``webhook_callback_url``,
  ``model``, ``max_screenshot_scrolling_times``, ``extra_http_headers``,
  ``sequential_key``.  Omitting the kwarg must leave the persisted value
  unchanged; passing explicit ``None`` clears the column.
- bare-``None`` (5): ``persist_browser_session``, ``run_with``,
  ``ai_fallback``, ``cache_key``, ``run_sequentially``.  Both omitting
  the kwarg and passing ``None`` must leave the persisted value
  unchanged (matches the existing ``update_workflow`` semantics).

These tests are the merge gate for PR 2 of the copilot-v2 stack.  If
either semantic regresses, non-copilot workflow-update call sites
(Workflows UI save path, CLI imports) will silently lose or clobber
workflow-level settings.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base
from skyvern.schemas.runs import ProxyLocation

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def agent_db(db_engine: Any) -> AsyncGenerator[AgentDB]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=db_engine)


@pytest_asyncio.fixture
async def seeded_workflow(agent_db: AgentDB) -> dict[str, str]:
    """Create a workflow with every one of the 11 threaded fields pre-set.

    Distinct, non-default values are chosen so a silent clobber to ``None``
    or the column default is observable in assertions.
    """
    org = await agent_db.organizations.create_organization(
        organization_name="Fields Regression Org",
        domain="update-fields.test",
    )
    workflow = await agent_db.workflows.create_workflow(
        title="seed-title",
        description="seed description",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
        proxy_location=ProxyLocation.RESIDENTIAL,
        webhook_callback_url="https://example.com/webhook",
        max_screenshot_scrolling_times=7,
        extra_http_headers={"X-Seed": "yes"},
        persist_browser_session=True,
        model={"model_name": "seed-model"},
        run_with="agent",
        ai_fallback=False,
        cache_key="seed-cache-key",
        run_sequentially=True,
        sequential_key="seed-sequential-key",
    )
    return {"organization_id": org.organization_id, "workflow_id": workflow.workflow_id}


async def _get(agent_db: AgentDB, ids: dict[str, str]) -> Any:
    workflow = await agent_db.workflows.get_workflow(
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
    )
    assert workflow is not None
    return workflow


async def test_omitting_all_workflow_level_fields_preserves_seed(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """Omitting every kwarg except title must leave all 11 fields intact."""
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=seeded_workflow["workflow_id"],
        organization_id=seeded_workflow["organization_id"],
        title="renamed-title",
    )
    workflow = await _get(agent_db, seeded_workflow)

    assert workflow.title == "renamed-title"
    # Every threaded field survives the omit-kwargs call:
    assert workflow.proxy_location == ProxyLocation.RESIDENTIAL
    assert workflow.webhook_callback_url == "https://example.com/webhook"
    assert workflow.max_screenshot_scrolls == 7
    assert workflow.extra_http_headers == {"X-Seed": "yes"}
    assert workflow.persist_browser_session is True
    assert workflow.model == {"model_name": "seed-model"}
    assert workflow.run_with == "agent"
    assert workflow.ai_fallback is False
    assert workflow.cache_key == "seed-cache-key"
    assert workflow.run_sequentially is True
    assert workflow.sequential_key == "seed-sequential-key"


async def test_passing_none_to_bare_none_fields_does_not_clobber(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """Bare-``None`` fields rely on ``if x is not None`` guards in the repo.

    Passing explicit ``None`` must be a no-op, matching the existing
    ``update_workflow`` semantics that non-copilot callers already depend on.
    """
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=seeded_workflow["workflow_id"],
        organization_id=seeded_workflow["organization_id"],
        persist_browser_session=None,
        run_with=None,
        ai_fallback=None,
        cache_key=None,
        run_sequentially=None,
    )
    workflow = await _get(agent_db, seeded_workflow)

    assert workflow.persist_browser_session is True
    assert workflow.run_with == "agent"
    assert workflow.ai_fallback is False
    assert workflow.cache_key == "seed-cache-key"
    assert workflow.run_sequentially is True


async def test_passing_none_to_unset_guarded_fields_clears_them(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """``_UNSET``-guarded fields distinguish omit (no change) from None (clear).

    Passing explicit ``None`` must write NULL so callers who need to clear
    a previously set value have a way to do it.
    """
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=seeded_workflow["workflow_id"],
        organization_id=seeded_workflow["organization_id"],
        proxy_location=None,
        webhook_callback_url=None,
        model=None,
        max_screenshot_scrolling_times=None,
        extra_http_headers=None,
        sequential_key=None,
    )
    workflow = await _get(agent_db, seeded_workflow)

    assert workflow.proxy_location is None
    assert workflow.webhook_callback_url is None
    assert workflow.model is None
    assert workflow.max_screenshot_scrolls is None
    assert workflow.extra_http_headers is None
    assert workflow.sequential_key is None


async def test_setting_new_values_persists_across_all_fields(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """Round-trip: passing a new value for each field writes the new value."""
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=seeded_workflow["workflow_id"],
        organization_id=seeded_workflow["organization_id"],
        proxy_location=ProxyLocation.US_CA,
        webhook_callback_url="https://example.com/webhook/new",
        max_screenshot_scrolling_times=12,
        extra_http_headers={"X-Updated": "yes"},
        persist_browser_session=False,
        model={"model_name": "new-model"},
        run_with="code",
        ai_fallback=True,
        cache_key="new-cache-key",
        run_sequentially=False,
        sequential_key="new-sequential-key",
    )
    workflow = await _get(agent_db, seeded_workflow)

    assert workflow.proxy_location == ProxyLocation.US_CA
    assert workflow.webhook_callback_url == "https://example.com/webhook/new"
    assert workflow.max_screenshot_scrolls == 12
    assert workflow.extra_http_headers == {"X-Updated": "yes"}
    assert workflow.persist_browser_session is False
    assert workflow.model == {"model_name": "new-model"}
    assert workflow.run_with == "code"
    assert workflow.ai_fallback is True
    assert workflow.cache_key == "new-cache-key"
    assert workflow.run_sequentially is False
    assert workflow.sequential_key == "new-sequential-key"
