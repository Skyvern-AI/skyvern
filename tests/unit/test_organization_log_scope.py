import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import structlog

from skyvern.config import settings
from skyvern.forge.sdk import log_artifacts
from skyvern.forge.sdk.core.organization_age_cache import cached_org_age
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import Base, OrganizationModel
from skyvern.forge.sdk.forge_log import has_log_organization_fields
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.services import organization_log_scope
from skyvern.services.organization_log_scope import organization_log_scope as scope_organization_logs
from skyvern.services.organization_log_scope import warm_organization_age
from tests.unit.forge_log_capture import capture_runtime_logs

_ORGANIZATION_ID = "o_100000000000000002"
_SCOPED_ORGANIZATION_ID = "o_100000000000000010"


@pytest.mark.asyncio
async def test_scope_adds_log_fields_without_touching_the_run_log_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stuck-run sweep and webhook delivery write run status inside this scope. Under a real SkyvernContext
    # that write would save the scope's few lines over the run's own log artifact.
    now = datetime.now(UTC)
    organization = Organization(
        organization_id=_SCOPED_ORGANIZATION_ID,
        organization_name="Scope",
        created_at=now - timedelta(days=3, hours=1),
        modified_at=now,
    )
    get_organization = AsyncMock(return_value=organization)
    monkeypatch.setattr(
        organization_log_scope.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(get_organization=get_organization)),
    )
    monkeypatch.setattr(settings, "ENABLE_LOG_ARTIFACTS", True)
    save_log_artifacts = AsyncMock()
    monkeypatch.setattr(log_artifacts, "_save_log_artifacts", save_log_artifacts)

    with capture_runtime_logs() as logs:
        async with scope_organization_logs(_SCOPED_ORGANIZATION_ID):
            async with scope_organization_logs(_SCOPED_ORGANIZATION_ID):
                structlog.get_logger().info("Webhook sent successfully", workflow_run_id="wr_1")
                await log_artifacts.save_workflow_run_logs("wr_1")

    sent = [entry for entry in logs if str(entry["msg"]).startswith("Webhook sent successfully")]
    assert [(str(entry["organization_id"]), entry["org_age"]) for entry in sent] == [(_SCOPED_ORGANIZATION_ID, 3)]
    save_log_artifacts.assert_not_awaited()
    get_organization.assert_awaited_once_with(_SCOPED_ORGANIZATION_ID)


@pytest_asyncio.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AgentDB]:
    engine = _build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    database = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=engine)
    async with database.Session() as session:
        session.add(
            OrganizationModel(
                organization_id=_ORGANIZATION_ID,
                organization_name="Warm-up",
                created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5, hours=1),
            )
        )
        await session.commit()
    monkeypatch.setattr(organization_log_scope.app, "DATABASE", database)
    try:
        yield database
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_loading_an_organization_row_caches_its_age(database: AgentDB) -> None:
    # Any code path that loads the org (auth, run setup, a worker lookup) teaches the log processor its age.
    assert cached_org_age(_ORGANIZATION_ID) is None

    await database.organizations.get_organization(_ORGANIZATION_ID)

    assert cached_org_age(_ORGANIZATION_ID) == 5


@pytest.mark.asyncio
async def test_warm_up_reads_each_organization_once_per_process(
    database: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_organization = AsyncMock(wraps=database.organizations.get_organization)
    monkeypatch.setattr(database.organizations, "get_organization", get_organization)

    await warm_organization_age(_ORGANIZATION_ID)
    await warm_organization_age(_ORGANIZATION_ID)
    await warm_organization_age(None)

    assert cached_org_age(_ORGANIZATION_ID) == 5
    get_organization.assert_awaited_once_with(_ORGANIZATION_ID)


@pytest.mark.asyncio
async def test_hung_lookup_still_runs_the_wrapped_work_within_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lookup only adds log fields; a hung one must not hold the sweep or the webhook delivery it wraps.
    monkeypatch.setattr(organization_log_scope, "_ORGANIZATION_READ_TIMEOUT_SECONDS", 0.05)
    never_answers = asyncio.Event()

    async def hung_get_organization(organization_id: str) -> None:
        await never_answers.wait()

    monkeypatch.setattr(
        organization_log_scope.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(get_organization=hung_get_organization)),
    )
    stamped_while_running: list[bool] = []

    async def wrapped_work() -> None:
        async with scope_organization_logs(_SCOPED_ORGANIZATION_ID):
            stamped_while_running.append(has_log_organization_fields())

    await asyncio.wait_for(wrapped_work(), timeout=2)

    assert stamped_while_running == [False]


@pytest.mark.asyncio
async def test_concurrent_warm_ups_share_one_read(database: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh worker can start many activities for one org at once; they must not each read the org.
    real_get_organization = database.organizations.get_organization
    release = asyncio.Event()
    reads: list[str] = []

    async def gated_get_organization(organization_id: str) -> object:
        reads.append(organization_id)
        await release.wait()
        return await real_get_organization(organization_id)

    monkeypatch.setattr(database.organizations, "get_organization", gated_get_organization)

    warm_ups = [asyncio.create_task(warm_organization_age(_ORGANIZATION_ID)) for _ in range(20)]
    for _ in range(5):
        await asyncio.sleep(0)
    reads_while_every_caller_waits = list(reads)
    release.set()
    await asyncio.gather(*warm_ups)

    assert reads_while_every_caller_waits == [_ORGANIZATION_ID]
    assert reads == [_ORGANIZATION_ID]
    assert cached_org_age(_ORGANIZATION_ID) == 5
    assert organization_log_scope._warmups_in_flight == {}


@pytest.mark.asyncio
async def test_hung_read_releases_the_caller_and_backs_off(database: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # The read only adds a log field: a hung one must not hold the caller, and during a slowdown the same org pays
    # that wait once per backoff rather than at every activity start.
    monkeypatch.setattr(organization_log_scope, "_ORGANIZATION_READ_TIMEOUT_SECONDS", 0.05)
    clock = [1_000.0]
    monkeypatch.setattr(organization_log_scope, "monotonic", lambda: clock[0])
    never_answers = asyncio.Event()
    reads: list[str] = []

    async def hung_get_organization(organization_id: str) -> None:
        reads.append(organization_id)
        await never_answers.wait()

    monkeypatch.setattr(database.organizations, "get_organization", hung_get_organization)

    await asyncio.wait_for(warm_organization_age(_ORGANIZATION_ID), timeout=2)
    clock[0] += organization_log_scope._FAILED_READ_BACKOFF_SECONDS - 1
    await asyncio.wait_for(warm_organization_age(_ORGANIZATION_ID), timeout=2)
    reads_within_backoff = list(reads)
    clock[0] += 2
    await asyncio.wait_for(warm_organization_age(_ORGANIZATION_ID), timeout=2)

    assert reads_within_backoff == [_ORGANIZATION_ID]
    assert reads == [_ORGANIZATION_ID, _ORGANIZATION_ID]
    assert cached_org_age(_ORGANIZATION_ID) is None
    assert organization_log_scope._warmups_in_flight == {}


@pytest.mark.asyncio
async def test_missing_organization_is_read_once_per_ttl(database: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # A deleted or unknown org id reaches every activity start through the interceptor.
    clock = [1_000.0]
    monkeypatch.setattr(organization_log_scope, "monotonic", lambda: clock[0])
    get_organization = AsyncMock(wraps=database.organizations.get_organization)
    monkeypatch.setattr(database.organizations, "get_organization", get_organization)
    missing_organization_id = "o_100000000000000099"

    await warm_organization_age(missing_organization_id)
    clock[0] += organization_log_scope._MISSING_ORGANIZATION_TTL_SECONDS - 1
    await warm_organization_age(missing_organization_id)
    reads_within_ttl = get_organization.await_count
    clock[0] += 2
    await warm_organization_age(missing_organization_id)

    assert reads_within_ttl == 1
    assert get_organization.await_count == 2


@pytest.mark.asyncio
async def test_failed_warm_up_never_fails_the_caller(database: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # A DB error backs off like a timeout instead of failing the activity; nothing is cached.
    get_organization = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(database.organizations, "get_organization", get_organization)

    await warm_organization_age(_ORGANIZATION_ID)
    await warm_organization_age(_ORGANIZATION_ID)

    assert cached_org_age(_ORGANIZATION_ID) is None
    assert get_organization.await_count == 1
