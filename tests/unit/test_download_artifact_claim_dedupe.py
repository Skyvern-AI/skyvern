"""Real-repository guard on the run-scoped downloaded-files read dedupe (SKY-14276).

A persistent-session download can be registered twice for one run: session-produced (the watcher
writes a DOWNLOAD row with ``browser_session_id`` set — already run-bound today, or claimed later) and
run-scoped (the local save writes a DOWNLOAD row with ``browser_session_id`` NULL). Both are the same
bytes, so the run-scoped read (``get_downloaded_files``) must surface one entry per distinct download.
These tests drive the real reader on an in-memory DB (schema from the ORM), not mocks.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import Base

_DUMMY_KEYRING_JSON = '{"current_kid": "k1", "keys": {"k1": {"secret": "deadbeef"}}}'
_WINDOW_START = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=1)


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncEngine:
    engine = _build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@pytest_asyncio.fixture
async def sqlite_db(sqlite_engine: AsyncEngine) -> AgentDB:
    return AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)


async def _make(
    db: AgentDB,
    org_id: str,
    aid: str,
    *,
    run_id: str | None,
    browser_session_id: str | None,
    checksum: str | None,
) -> str:
    bucket = "skyvern-artifacts/v1/production" if browser_session_id else "skyvern-uploads/downloads/production"
    await db.artifacts.create_artifact(
        artifact_id=aid,
        artifact_type=ArtifactType.DOWNLOAD,
        uri=f"s3://{bucket}/{org_id}/{aid}.pdf",
        organization_id=org_id,
        run_id=run_id,
        browser_session_id=browser_session_id,
        checksum=checksum,
        file_size=192867,
    )
    return aid


def _install_storage(db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> S3Storage:
    storage = S3Storage()
    storage.async_client = MagicMock()
    monkeypatch.setattr(app, "DATABASE", db)
    monkeypatch.setattr(app, "STORAGE", storage, raising=False)
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager(), raising=False)
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON)
    return storage


async def _run_scoped_ids(db: AgentDB, org_id: str, run_id: str, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    storage = _install_storage(db, monkeypatch)
    files = await storage.get_downloaded_files(organization_id=org_id, run_id=run_id)
    return {f.artifact_id for f in files if f.artifact_id}


async def _session_namespace_ids(
    db: AgentDB, org_id: str, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> set[str]:
    storage = _install_storage(db, monkeypatch)
    files = await storage.get_shared_downloaded_files_in_browser_session(
        organization_id=org_id, browser_session_id=session_id
    )
    return {f.artifact_id for f in files if f.artifact_id}


@pytest.mark.asyncio
async def test_run_scoped_read_collapses_the_session_produced_twin(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production witness shape: a producer-bound session row (run_id set at insert, never claimed),
    # its byte-identical run-scoped save twin, and a distinct-checksum session row that must survive.
    org = await sqlite_db.organizations.create_organization("Test Org witness")
    org_id, session_id, run_id = org.organization_id, "pbs_w", "wr_w"
    await _make(sqlite_db, org_id, "a_session_twin", run_id=run_id, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")
    await _make(sqlite_db, org_id, "a_session_distinct", run_id=run_id, browser_session_id=session_id, checksum="c2")

    visible = await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch)
    assert visible == {"a_run_scoped", "a_session_distinct"}, f"expected canonical + distinct only, got {visible}"


@pytest.mark.asyncio
async def test_run_scoped_read_keeps_session_download_without_run_scoped_twin(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SESSION_DIR suppression / save-failure: only the session representation exists, so it must stay.
    org = await sqlite_db.organizations.create_organization("Test Org no-twin")
    org_id, session_id, run_id = org.organization_id, "pbs_nt", "wr_nt"
    await _make(sqlite_db, org_id, "a_session_only", run_id=run_id, browser_session_id=session_id, checksum="c1")

    assert await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch) == {"a_session_only"}


@pytest.mark.asyncio
async def test_run_scoped_read_never_collapses_null_checksum_rows(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await sqlite_db.organizations.create_organization("Test Org nullsum")
    org_id, session_id, run_id = org.organization_id, "pbs_ns", "wr_ns"
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")
    await _make(sqlite_db, org_id, "a_session_nullsum", run_id=run_id, browser_session_id=session_id, checksum=None)

    assert await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch) == {"a_run_scoped", "a_session_nullsum"}


@pytest.mark.asyncio
async def test_run_scoped_read_keeps_distinct_checksums(sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    org = await sqlite_db.organizations.create_organization("Test Org distinct")
    org_id, session_id, run_id = org.organization_id, "pbs_d", "wr_d"
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")
    await _make(sqlite_db, org_id, "a_session_distinct", run_id=run_id, browser_session_id=session_id, checksum="c2")

    assert await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch) == {"a_run_scoped", "a_session_distinct"}


@pytest.mark.asyncio
async def test_run_scoped_read_keeps_multiple_identical_run_scoped_rows(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only session-produced rows are droppable; identical-content run-scoped rows all survive.
    org = await sqlite_db.organizations.create_organization("Test Org multi")
    org_id, session_id, run_id = org.organization_id, "pbs_m", "wr_m"
    await _make(sqlite_db, org_id, "a_run_1", run_id=run_id, browser_session_id=None, checksum="c1")
    await _make(sqlite_db, org_id, "a_run_2", run_id=run_id, browser_session_id=None, checksum="c1")
    await _make(sqlite_db, org_id, "a_session_twin", run_id=run_id, browser_session_id=session_id, checksum="c1")

    assert await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch) == {"a_run_1", "a_run_2"}


@pytest.mark.asyncio
async def test_run_scoped_read_collapses_a_claimed_session_row_too(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Legacy shape: the session row is unbound at insert and stamped by the finalization claim; after
    # claiming it is run-scoped with browser_session_id still set, so the reader rule collapses it too.
    org = await sqlite_db.organizations.create_organization("Test Org claimed")
    org_id, session_id, run_id = org.organization_id, "pbs_c", "wr_c"
    await _make(sqlite_db, org_id, "a_session_unbound", run_id=None, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")

    await sqlite_db.artifacts.claim_session_download_artifacts_for_run(
        run_id=run_id, browser_session_id=session_id, organization_id=org_id, run_started_at=_WINDOW_START
    )
    assert await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch) == {"a_run_scoped"}


@pytest.mark.asyncio
async def test_session_namespace_listing_is_unchanged(sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> None:
    # The dedupe belongs to the run-scoped read only; the session-namespace listing must still return
    # every session row, even two with the same checksum.
    org = await sqlite_db.organizations.create_organization("Test Org sessionlist")
    org_id, session_id, run_id = org.organization_id, "pbs_s", "wr_s"
    await _make(sqlite_db, org_id, "a_sess_1", run_id=run_id, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_sess_2", run_id=run_id, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")

    assert await _session_namespace_ids(sqlite_db, org_id, session_id, monkeypatch) == {"a_sess_1", "a_sess_2"}


@pytest.mark.asyncio
async def test_run_scoped_read_pairs_same_content_session_rows_one_for_one(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two legitimate same-content session downloads but only one run-scoped materialization (the second
    # local copy was content-skipped by the physical dedupe). Pairing is one-for-one: drop exactly one
    # session row against the single run-scoped twin and keep the other, not collapse both.
    org = await sqlite_db.organizations.create_organization("Test Org one-for-one")
    org_id, session_id, run_id = org.organization_id, "pbs_1f1", "wr_1f1"
    await _make(sqlite_db, org_id, "a_session_a", run_id=run_id, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_session_b", run_id=run_id, browser_session_id=session_id, checksum="c1")
    await _make(sqlite_db, org_id, "a_run_scoped", run_id=run_id, browser_session_id=None, checksum="c1")

    visible = await _run_scoped_ids(sqlite_db, org_id, run_id, monkeypatch)
    assert len(visible) == 2, f"expected the run-scoped row plus exactly one session twin, got {visible}"
    assert "a_run_scoped" in visible, f"run-scoped canonical must survive, got {visible}"
    assert len(visible & {"a_session_a", "a_session_b"}) == 1, f"exactly one session twin must survive, got {visible}"
