import asyncio
import datetime
import hashlib
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, ArtifactManager, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context as context_module
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository
from tests.unit._sql_recording import recorded_statements
from tests.unit.helpers import register_download


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


_OLD, _NEW = "a" * 64, "b" * 64


async def _download(repo: ArtifactsRepository, **fields: object) -> str:
    artifact = await repo.create_artifact(
        artifact_id=generate_artifact_id(),
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://uploads/o_test/wr_test/invoice.custom",
        organization_id="o_test",
        run_id="wr_test",
        file_size=10,
        **fields,
    )
    return artifact.artifact_id


@pytest.mark.asyncio
async def test_a_configured_name_stamp_vouches_only_for_the_content_and_object_it_was_earned_for(
    repo: ArtifactsRepository,
) -> None:
    refreshed, moved, kept = [await _download(repo, checksum=_OLD, configured_name_sha256=_OLD) for _ in range(3)]
    promoted = await _download(repo, checksum=_OLD)

    async def stamp(artifact_id: str) -> str | None:
        return (await repo.get_artifact_by_id(artifact_id, "o_test")).configured_name_sha256

    await repo.refresh_download_artifact_content(refreshed, "o_test", checksum=_NEW, file_size=11)
    assert await stamp(refreshed) is None
    await repo.refresh_download_artifact_content(refreshed, "o_test", checksum=_OLD, file_size=10)
    assert await stamp(refreshed) is None
    await repo.refresh_download_artifact_content(
        refreshed, "o_test", checksum=_NEW, file_size=11, configured_name_sha256=_NEW
    )
    assert await stamp(refreshed) == _NEW
    await repo.refresh_download_artifact_content(refreshed, "o_test", checksum=_NEW, file_size=11)
    assert await stamp(refreshed) == _NEW, "unchanged content refreshed without a grant keeps its stamp"

    await repo.update_artifact_uri(moved, "o_test", uri="s3://uploads/o_test/wr_test/renamed.pdf")
    await repo.update_artifact_uri(kept, "o_test", uri="s3://uploads/o_test/wr_test/invoice.custom")
    assert await stamp(moved) is None and await stamp(kept) == _OLD

    await repo.promote_configured_name(promoted, "o_test", configured_name_sha256=_NEW)
    assert await stamp(promoted) is None
    await repo.promote_configured_name(promoted, "o_test", configured_name_sha256=_OLD)
    assert await stamp(promoted) == _OLD


@pytest.mark.asyncio
async def test_registration_credits_no_configured_name_to_rewritten_bytes_or_another_org(
    agent_db: AgentDB, tmp_path: Path
) -> None:
    mutated = tmp_path / "statement.custom"
    mutated.write_bytes(b"statement")
    context = SkyvernContext(organization_id="o_test", run_id="wr_test", workflow_run_id="wr_test")
    await context.record_configured_download(str(mutated), "wr_test")
    mutated.write_bytes(b"rewritten after the rename")
    other_org = SkyvernContext(organization_id="o_other", run_id="wr_test", workflow_run_id="wr_test")
    await other_org.record_configured_download(str(mutated), "wr_test")

    for producing in (context, other_org):
        registered = await register_download(agent_db, mutated, producing, "o_test", f"{producing.organization_id}/")
        assert registered.configured_name_sha256 is None, producing.organization_id


@pytest.mark.parametrize("change", ["old_bytes_restored", "file_replaced_with_equal_bytes"])
@pytest.mark.asyncio
async def test_a_configured_name_is_not_regranted_once_its_file_changed(
    agent_db: AgentDB, tmp_path: Path, change: str
) -> None:
    path = tmp_path / "invoice.custom"
    path.write_bytes(b"A")
    context = SkyvernContext(organization_id="o_test", run_id="wr_test", workflow_run_id="wr_test")
    await context.record_configured_download(str(path), "wr_test")

    named = await register_download(agent_db, path, context)
    if change == "old_bytes_restored":
        path.write_bytes(b"B")
        assert (await register_download(agent_db, path, context)).configured_name_sha256 is None
        path.write_bytes(b"A")
    else:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"A")
        os.replace(replacement, path)
    # The stored row keeps its stamp for these unchanged bytes; a new registration earns none.
    later = await register_download(agent_db, path, context, prefix="attempts/2/")

    assert named.configured_name_sha256 == named.checksum
    assert later.checksum == named.checksum and later.configured_name_sha256 is None
    assert context.configured_download_digest(str(path), "o_test", "wr_test") is None


@pytest.mark.parametrize("case", ["completed", "cancelled", "replaced", "organization_changed"])
@pytest.mark.asyncio
async def test_a_receipt_hashes_off_the_event_loop_and_publishes_only_for_the_file_it_started_on(
    agent_db: AgentDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path = tmp_path / "invoice.custom"
    path.write_bytes(b"A" * (3 << 20))
    reached, release = threading.Event(), threading.Event()
    sha256 = hashlib.sha256

    class HeldFirstChunk:
        """The real hash, whose first chunk waits until the event loop releases it."""

        def __init__(self) -> None:
            self.digest, self.held = sha256(), not reached.is_set()

        def update(self, chunk: bytes) -> None:
            if self.held:
                reached.set()
                assert release.wait(3), "the receipt hash held the event loop"
                self.held = False
            self.digest.update(chunk)

        def hexdigest(self) -> str:
            return self.digest.hexdigest()

    monkeypatch.setattr(context_module, "hashlib", SimpleNamespace(sha256=HeldFirstChunk))
    context = SkyvernContext(organization_id="o_test", run_id="wr_test", workflow_run_id="wr_test")
    recording = asyncio.ensure_future(context.record_configured_download(str(path), "wr_test"))
    while not reached.is_set():
        await asyncio.sleep(0)
    if case == "cancelled":
        recording.cancel()
        await asyncio.sleep(0)
        recording.cancel()
        await asyncio.sleep(0)
        assert not recording.done(), "a cancelled recording settles its read before it ends"
    elif case == "replaced":
        (replacement := tmp_path / "replacement").write_bytes(b"B")
        os.replace(replacement, path)
        await context.record_configured_download(str(path), "wr_test")
    elif case == "organization_changed":
        context.organization_id = "o_other"
    release.set()
    if case == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await recording
    else:
        await recording
    for _ in range(3):
        await asyncio.sleep(0)

    named = {"completed": b"A" * (3 << 20), "replaced": b"B"}.get(case)
    registered = await register_download(agent_db, path, context, "o_test")
    assert registered.configured_name_sha256 == (sha256(named).hexdigest() if named else None)
