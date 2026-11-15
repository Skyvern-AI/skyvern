"""Per-run scrape budgets survive persistence and apply to every scrape in isolation."""

import asyncio
import importlib.util
import json
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, inspect, text

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from skyvern.config import settings
from skyvern.exceptions import NoElementFound
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base, WorkflowModel
from skyvern.forge.sdk.routes.agent_protocol import (
    _workflow_run_request_from_workflow_request,
    _workflow_run_request_to_legacy_request,
)
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import TaskRequest
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.library.skyvern import Skyvern
from skyvern.schemas.runs import TaskRunRequest, WorkflowRunRequest
from skyvern.services.workflow_service import workflow_request_body_from_existing_run
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.scoped_asyncio import ScopedAsyncio

_REQUESTS = [
    (TaskRunRequest, {"prompt": "Extract the price"}),
    (WorkflowRunRequest, {"workflow_id": "wpid_test"}),
    (WorkflowRequestBody, {}),
    (TaskRequest, {"url": "https://example.com"}),
    (TaskV2Request, {"user_prompt": "Extract the price"}),
]


@pytest.mark.parametrize(("model", "required"), _REQUESTS)
def test_screenshot_budget_is_optional_and_independent_of_post_action_scrolls(
    model: type[BaseModel],
    required: dict,
) -> None:
    assert model(**required).max_scrape_screenshots is None
    request = model(**required, max_scrape_screenshots=1, max_screenshot_scrolls=5)
    assert request.max_scrape_screenshots == 1
    assert request.max_screenshot_scrolls == 5


@pytest.mark.parametrize(("model", "required"), _REQUESTS)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1", 2**31])
def test_screenshot_budget_rejects_invalid_counts(
    model: type[BaseModel],
    required: dict,
    invalid: object,
) -> None:
    with pytest.raises(ValidationError):
        model(**required, max_scrape_screenshots=invalid)


def _scraped_page() -> ScrapedPage:
    return ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        screenshots=[b"image"],
        url="https://example.com",
        html="<p>Price: $12</p>",
        extracted_text="Price: $12",
        _browser_state=MagicMock(),
        _clean_up_func=AsyncMock(),
        _scrape_exclude=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "caller_limit", "expected"),
    [
        (None, 10, 10),
        (1, 10, 1),
        (3, 10, 3),
        (20, 10, 10),
        (3, 1, 1),
    ],
)
async def test_scrapes_keep_server_and_engine_ceilings(
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    caller_limit: int,
    expected: int,
) -> None:
    original_default = settings.MAX_NUM_SCREENSHOTS
    unsafe = AsyncMock(return_value=_scraped_page())
    monkeypatch.setattr(scraper, "scrape_web_unsafe", unsafe)
    with skyvern_context.scoped(SkyvernContext(max_scrape_screenshots=limit)):
        await scraper.scrape_website(
            MagicMock(),
            "https://example.com",
            AsyncMock(),
            max_screenshot_number=caller_limit,
        )
    assert unsafe.call_args.kwargs["max_screenshot_number"] == expected
    assert settings.MAX_NUM_SCREENSHOTS == original_default


@pytest.mark.asyncio
async def test_scrape_without_run_uses_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = AsyncMock(return_value=_scraped_page())
    monkeypatch.setattr(scraper, "scrape_web_unsafe", unsafe)
    monkeypatch.setattr(scraper.skyvern_context, "current", lambda: None)
    await scraper.scrape_website(MagicMock(), "https://example.com", AsyncMock())
    assert unsafe.call_args.kwargs["max_screenshot_number"] == settings.MAX_NUM_SCREENSHOTS


@pytest.mark.asyncio
async def test_parallel_runs_and_extraction_refreshes_keep_their_own_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, list[int]] = {"run_a": [], "run_b": []}

    async def capture(**kwargs: object) -> ScrapedPage:
        # Interleave the runs at the actual shared scraper boundary.
        await asyncio.sleep(0)
        context = skyvern_context.ensure_context()
        observed[context.run_id].append(kwargs["max_screenshot_number"])
        return _scraped_page()

    monkeypatch.setattr(scraper, "scrape_web_unsafe", capture)

    async def run(run_id: str, limit: int) -> None:
        with skyvern_context.scoped(SkyvernContext(run_id=run_id, max_scrape_screenshots=limit)):
            # Exercise BrowserState forwarding, including refresh's default arguments.
            state = MagicMock(spec=RealBrowserState)
            state.scrape_website = partial(RealBrowserState.scrape_website, state)
            state._wait_for_challenge_solver = AsyncMock()
            state.get_working_page = AsyncMock(return_value=None)
            page = await state.scrape_website("https://example.com", AsyncMock())
            page._browser_state = state
            page._clean_up_func = AsyncMock()
            await page.refresh()

    await asyncio.gather(run("run_a", 1), run("run_b", 3))
    assert observed == {"run_a": [1, 1], "run_b": [3, 3]}


@pytest.mark.asyncio
async def test_empty_page_retries_keep_the_run_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = AsyncMock(side_effect=[NoElementFound(), _scraped_page()])
    monkeypatch.setattr(scraper, "scrape_web_unsafe", unsafe)
    monkeypatch.setattr(scraper, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    with skyvern_context.scoped(SkyvernContext(max_scrape_screenshots=1)):
        await scraper.scrape_website(MagicMock(), "https://example.com", AsyncMock())
    assert [call.kwargs["max_screenshot_number"] for call in unsafe.call_args_list] == [1, 1]


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[AgentDB]:
    db = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'scrape-limits.db'}")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await db.organizations.create_organization(organization_id="org_test", organization_name="Test")
    async with db.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_test",
                workflow_permanent_id="wpid_test",
                organization_id="org_test",
                title="Test",
                workflow_definition={"blocks": [], "parameters": []},
                version=1,
            )
        )
        await session.commit()
    try:
        yield db
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1, 3])
async def test_persisted_tasks_and_runs_retain_budget_for_reload_and_retry(
    database: AgentDB,
    limit: int | None,
) -> None:
    run = await database.workflow_runs.create_workflow_run(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        max_scrape_screenshots=limit,
    )
    task = await database.tasks.create_task(
        url="https://example.com",
        title="Test",
        navigation_goal="Extract",
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id="org_test",
        max_scrape_screenshots=limit,
        workflow_run_id=run.workflow_run_id,
    )
    task_v2 = await database.observer.create_task_v2(
        organization_id="org_test",
        max_scrape_screenshots=limit,
    )
    restored_run = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "org_test")
    restored_task = await database.tasks.get_task(task.task_id, "org_test")
    restored_v2 = await database.observer.get_task_v2(task_v2.observer_cruise_id, "org_test")
    assert restored_run.max_scrape_screenshots == limit
    assert restored_task.max_scrape_screenshots == limit
    assert restored_task.to_task_response().max_scrape_screenshots == limit
    assert restored_v2.max_scrape_screenshots == limit

    retry = workflow_request_body_from_existing_run(restored_run)
    public = _workflow_run_request_from_workflow_request(
        workflow_id="wpid_test",
        title=None,
        workflow_request=retry,
    )
    assert public.max_scrape_screenshots == limit
    assert _workflow_run_request_to_legacy_request(public).max_scrape_screenshots == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "workflow"])
async def test_python_sdk_serializes_the_per_run_budget(kind: str) -> None:
    sent: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "run_id": "run_test",
                "run_type": "task_v1" if kind == "task" else "workflow_run",
                "status": "created",
                "created_at": "2026-11-02T12:00:00Z",
                "modified_at": "2026-11-02T12:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = Skyvern(base_url="https://example.test", api_key="test", httpx_client=http_client)
        if kind == "task":
            await client.run_task(prompt="Extract the price", max_scrape_screenshots=1)
        else:
            await client.run_workflow(workflow_id="wpid_test", max_scrape_screenshots=1)
    assert sent[0]["max_scrape_screenshots"] == 1


def test_migration_preserves_existing_rows_on_upgrade_and_downgrade() -> None:
    versions = Path(__file__).resolve().parents[2] / "alembic/versions"
    migration_path = next(versions.glob("*_add_per_run_scrape_screenshot_limit.py"))
    spec = importlib.util.spec_from_file_location("scrape_limit_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    tables = ("tasks", "observer_cruises", "workflow_runs")
    try:
        with engine.begin() as connection:
            for table in tables:
                connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                connection.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                for table in tables:
                    statement = text(f"SELECT id, max_scrape_screenshots FROM {table}")
                    row = connection.execute(statement).one()
                    assert tuple(row) == (1, None)
                migration.downgrade()
            for table in tables:
                columns = inspect(connection).get_columns(table)
                assert [column["name"] for column in columns] == ["id"]
                assert connection.execute(text(f"SELECT id FROM {table}")).scalar_one() == 1
    finally:
        engine.dispose()
