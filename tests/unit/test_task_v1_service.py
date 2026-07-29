from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge import app
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import TaskGenerationModel
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.services.task_v1_service import generate_task


@pytest_asyncio.fixture
async def repository(sqlite_engine: AsyncEngine) -> WorkflowParametersRepository:
    db = BaseAlchemyDB(sqlite_engine)
    return WorkflowParametersRepository(db.Session, debug_enabled=False)


async def _insert_task_generation(
    repository: WorkflowParametersRepository,
    *,
    task_generation_id: str,
    organization_id: str,
    user_prompt_hash: str,
    created_at: datetime,
    llm: str | None = "test-llm",
) -> None:
    async with repository.Session() as session:
        session.add(
            TaskGenerationModel(
                task_generation_id=task_generation_id,
                organization_id=organization_id,
                user_prompt="test prompt",
                user_prompt_hash=user_prompt_hash,
                llm=llm,
                created_at=created_at,
                modified_at=created_at,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_is_scoped_to_organization(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prompt_hash = "shared-prompt-hash"
    await _insert_task_generation(
        repository,
        task_generation_id="tg_organization_a",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=2),
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_organization_b",
        organization_id="org_b",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=1),
    )

    organization_a_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash=prompt_hash
    )
    organization_b_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_b", user_prompt_hash=prompt_hash
    )
    organization_c_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_c", user_prompt_hash=prompt_hash
    )

    assert organization_a_result is not None
    assert organization_a_result.task_generation_id == "tg_organization_a"
    assert organization_a_result.organization_id == "org_a"
    assert organization_b_result is not None
    assert organization_b_result.task_generation_id == "tg_organization_b"
    assert organization_b_result.organization_id == "org_b"
    assert organization_c_result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_does_not_return_cross_tenant_match(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _insert_task_generation(
        repository,
        task_generation_id="tg_other_organization",
        organization_id="org_other",
        user_prompt_hash="cross-organization-hash",
        created_at=now,
    )

    result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_requesting", user_prompt_hash="cross-organization-hash"
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_returns_newest_matching_row(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prompt_hash = "repeated-prompt-hash"
    await _insert_task_generation(
        repository,
        task_generation_id="tg_older",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=2),
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_newer",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=1),
    )

    result = await repository.get_task_generation_by_prompt_hash(organization_id="org_a", user_prompt_hash=prompt_hash)

    assert result is not None
    assert result.task_generation_id == "tg_newer"
    assert result.organization_id == "org_a"


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_preserves_llm_and_window_filters(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _insert_task_generation(
        repository,
        task_generation_id="tg_without_llm",
        organization_id="org_a",
        user_prompt_hash="without-llm-hash",
        created_at=now,
        llm=None,
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_outside_window",
        organization_id="org_a",
        user_prompt_hash="outside-window-hash",
        created_at=now - timedelta(hours=2),
    )

    without_llm_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash="without-llm-hash"
    )
    outside_window_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash="outside-window-hash", query_window_hours=1
    )

    assert without_llm_result is None
    assert outside_window_result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_requires_organization_id(
    repository: WorkflowParametersRepository,
) -> None:
    with pytest.raises(TypeError):
        await repository.get_task_generation_by_prompt_hash(user_prompt_hash="prompt-hash")


@pytest.mark.asyncio
async def test_generate_task_forwards_organization_id_to_cache_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    organization = Organization(
        organization_id="org_requesting",
        organization_name="test",
        created_at=now,
        modified_at=now,
    )
    cached_task_generation = TaskGeneration(
        task_generation_id="tg_cached",
        organization_id=organization.organization_id,
        user_prompt="test prompt",
        user_prompt_hash="cached-prompt-hash",
        llm="test-llm",
        created_at=now,
        modified_at=now,
    )
    lookup_kwargs: dict[str, object] = {}

    async def get_task_generation_by_prompt_hash(**kwargs: object) -> TaskGeneration:
        lookup_kwargs.update(kwargs)
        return cached_task_generation

    async def create_task_generation(**kwargs: object) -> TaskGeneration:
        return cached_task_generation

    database = SimpleNamespace(
        workflow_params=SimpleNamespace(
            get_task_generation_by_prompt_hash=get_task_generation_by_prompt_hash,
            create_task_generation=create_task_generation,
        )
    )
    monkeypatch.setattr(app, "DATABASE", database)

    await generate_task("test prompt", organization)

    assert lookup_kwargs["organization_id"] == organization.organization_id
