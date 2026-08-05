import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository


@pytest_asyncio.fixture
async def repo(sqlite_engine: AsyncEngine) -> ArtifactsRepository:
    db = BaseAlchemyDB(sqlite_engine)
    return ArtifactsRepository(db.Session, debug_enabled=False)


@pytest_asyncio.fixture
async def recording_artifact(sqlite_engine: AsyncEngine) -> str:
    created_at = datetime.datetime(2026, 8, 1, 12, 0, 0)
    async with sqlite_engine.begin() as conn:
        await conn.execute(
            ArtifactModel.__table__.insert().values(
                artifact_id="a_recording",
                organization_id="o_test",
                workflow_run_id="wr_test",
                artifact_type="recording",
                uri="s3://bucket/recording.webm",
                file_size=100,
                created_at=created_at,
                modified_at=created_at,
            )
        )
    return "a_recording"


@pytest.mark.asyncio
async def test_update_artifact_uri_returns_updated_values(
    repo: ArtifactsRepository,
    recording_artifact: str,
) -> None:
    updated = await repo.update_artifact_uri(
        artifact_id=recording_artifact,
        organization_id="o_test",
        uri="s3://bucket/recording.mp4",
        file_size=42,
    )

    assert updated is not None
    assert updated.artifact_id == recording_artifact
    assert updated.uri == "s3://bucket/recording.mp4"
    assert updated.file_size == 42


@pytest.mark.asyncio
async def test_update_artifact_uri_returns_none_for_other_organization(
    repo: ArtifactsRepository,
    recording_artifact: str,
) -> None:
    assert (
        await repo.update_artifact_uri(
            artifact_id=recording_artifact,
            organization_id="o_other",
            uri="s3://bucket/recording.mp4",
        )
        is None
    )
