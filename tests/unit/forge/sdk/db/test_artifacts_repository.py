import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, ArtifactManager, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository
from tests.unit._sql_recording import recorded_statements


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


@pytest.mark.asyncio
async def test_delete_artifacts_by_ids_scopes_to_organization(
    repo: ArtifactsRepository,
    recording_artifact: str,
    sqlite_engine: AsyncEngine,
) -> None:
    created_at = datetime.datetime(2026, 8, 1, 12, 0, 0)
    async with sqlite_engine.begin() as conn:
        await conn.execute(
            ArtifactModel.__table__.insert().values(
                artifact_id="a_other_org",
                organization_id="o_other",
                artifact_type="recording",
                uri="s3://bucket/other.webm",
                created_at=created_at,
                modified_at=created_at,
            )
        )

    await repo.delete_artifacts_by_ids(
        organization_id="o_test",
        artifact_ids=[recording_artifact, "a_other_org"],
    )

    async with sqlite_engine.connect() as conn:
        remaining_ids = set((await conn.scalars(select(ArtifactModel.artifact_id))).all())
    assert remaining_ids == {"a_other_org"}


class _RecordingStorage:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        self.stored[artifact.artifact_id] = data


@pytest.mark.asyncio
async def test_manager_bulk_create_persists_and_uploads_every_row_without_reading_it_back(
    org_scoped_db: tuple[AgentDB, AsyncEngine, str],
) -> None:
    db, engine, organization_id = org_scoped_db
    artifact_ids = [generate_artifact_id() for _ in range(3)]
    request = BulkArtifactCreationRequest(
        artifacts=[
            ArtifactBatchData(
                artifact_model=ArtifactModel(
                    artifact_id=artifact_id,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    uri=f"s3://bucket/{artifact_id}.png",
                    organization_id=organization_id,
                    task_id="tsk_bulk",
                    step_id="stp_bulk",
                ),
                data=artifact_id.encode(),
            )
            for artifact_id in artifact_ids
        ],
        primary_key="tsk_bulk",
    )
    storage = _RecordingStorage()
    manager = ArtifactManager()

    with (
        patch.object(artifact_manager_module, "app", SimpleNamespace(DATABASE=db, STORAGE=storage)),
        recorded_statements(engine) as statements,
    ):
        returned_ids = await manager.bulk_create_artifacts([request])
        await manager.wait_for_upload_aiotasks(["tsk_bulk"])

    assert returned_ids == artifact_ids
    assert [statement for statement in statements if statement.startswith("SELECT")] == []
    assert storage.stored == {artifact_id: artifact_id.encode() for artifact_id in artifact_ids}
    for artifact_id in artifact_ids:
        stored = await db.artifacts.get_artifact_by_id(artifact_id, organization_id)
        assert stored is not None
        assert stored.file_size == len(artifact_id.encode())
