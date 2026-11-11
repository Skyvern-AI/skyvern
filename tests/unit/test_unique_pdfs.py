"""A run's automatic outputs list each PDF content once; raw captures, rows and explicit outputs are untouched.

The browser case drives real Chromium through the production context factory; other cases inject the artifact
repository and object store at their boundaries.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import compute_engine
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import AnonymousCredentials
from google.auth.transport import requests as google_auth_requests

from skyvern.config import settings
from skyvern.constants import SKYVERN_UI_USER_AGENT
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api import real_gcp
from skyvern.forge.sdk.api.files import get_download_dir
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage import unique_pdfs
from skyvern.forge.sdk.artifact.storage.gcs import GcsStorage
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.browser_session_artifacts import BrowserSessionArtifactAccess
from skyvern.forge.sdk.workflow.models.block import FileUploadBlock, UploadToS3Block, bind_downloaded_files_to_output
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.response_value_caps import RUN_RESPONSE_MAX_VALUE_BYTES
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.workflows import BlockType
from skyvern.services import run_service, webhook_service
from skyvern.webeye import browser_factory
from skyvern.webeye.actions import handler as handler_module
from tests.unit.conftest import SqliteEngineFactory, requires_full_chromium
from tests.unit.helpers import StreamedDownloadObjects, make_organization, make_pdf, make_step, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio
from tests.unit.shared_route_app import shared_route_app

T0 = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


INVOICE, STATEMENT, RECEIPT = make_pdf("Invoice 1001"), make_pdf("Statement 2002"), make_pdf("Receipt 3003")
CUT_OFF = INVOICE[: len(INVOICE) // 2]
HEADER_ONLY = b"%PDF-1.4\n"
NOT_PDF = b"<!doctype html><title>Error</title>Session expired"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Rows:
    """DOWNLOAD rows plus the stored objects their public URLs serve."""

    def __init__(self) -> None:
        self.artifacts: list[Artifact] = []
        self.objects: dict[str, bytes | None] = {}
        self.store = StreamedDownloadObjects(self.objects)

    @property
    def reads(self) -> list[str]:
        return self.store.reads

    def add(
        self,
        artifact_id: str,
        filename: str,
        stored: bytes | None,
        minutes: int,
        *,
        claimed: bytes | None = None,
        modified_minutes: int | None = None,
        organization_id: str = "o_1",
        run_id: str = "wr_1",
        workflow_run_id: str | None = None,
        with_checksum: bool = True,
        with_size: bool = True,
        configured: bool = False,
    ) -> FileInfo:
        """A row created at ``minutes`` describing ``claimed`` (default: ``stored``); its object holds ``stored``."""
        described = stored if claimed is None else claimed
        assert described is not None
        uri = f"s3://downloads/{organization_id}/{run_id}/{artifact_id}/{filename}"
        created = T0 + timedelta(minutes=minutes)
        modified = T0 + timedelta(minutes=minutes if modified_minutes is None else modified_minutes)
        artifact = Artifact(
            artifact_id=artifact_id,
            artifact_type=ArtifactType.DOWNLOAD,
            uri=uri,
            organization_id=organization_id,
            run_id=run_id,
            workflow_run_id=workflow_run_id,
            checksum=_sha(described) if with_checksum else None,
            file_size=len(described) if with_size else None,
            created_at=created,
            modified_at=modified,
            configured_name_sha256=_sha(described) if configured else None,
        )
        self.artifacts.append(artifact)
        self.objects[uri] = stored
        return FileInfo(
            url=f"https://files.example/{artifact_id}",
            checksum=artifact.checksum,
            filename=filename,
            file_size=artifact.file_size,
            modified_at=modified,
            artifact_id=artifact_id,
        )

    def content(self, file_info: FileInfo | dict[str, Any]) -> bytes | None:
        artifact_id = file_info["artifact_id"] if isinstance(file_info, dict) else file_info.artifact_id
        return next(self.objects[a.uri] for a in self.artifacts if a.artifact_id == artifact_id)

    def uri(self, artifact_id: str) -> str:
        return next(a.uri for a in self.artifacts if a.artifact_id == artifact_id)

    async def get_artifacts_by_ids(self, artifact_ids: list[str], organization_id: str) -> list[Artifact]:
        return [a for a in self.artifacts if a.artifact_id in artifact_ids and a.organization_id == organization_id]

    async def list_artifacts_for_run_by_type(self, run_id: str, organization_id: str, **_: Any) -> list[Artifact]:
        return [a for a in self.artifacts if a.run_id == run_id and a.organization_id == organization_id]


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> _Rows:
    store = _Rows()
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", store.get_artifacts_by_ids)
    monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", store.list_artifacts_for_run_by_type)
    return store


def _use_storage(monkeypatch: pytest.MonkeyPatch, store: _Rows, listing: list[FileInfo]) -> StreamedDownloadObjects:
    store.store.listing = list(listing)
    monkeypatch.setattr(app, "STORAGE", store.store)
    return store.store


async def _run_output(
    monkeypatch: pytest.MonkeyPatch, store: _Rows, listing: list[FileInfo], **attempt: Any
) -> list[FileInfo]:
    storage = _use_storage(monkeypatch, store, listing)
    files, urls = await WorkflowService()._fetch_downloaded_files(
        SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1"), None, **attempt
    )
    assert files is not None
    assert urls == [file_info.url for file_info in files]
    assert await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1") == listing
    return files


@pytest.mark.asyncio
async def test_run_output_lists_each_pdf_content_once_keeping_the_first_copy(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    listing = [
        rows.add("a_1", "invoice.pdf", INVOICE, 0),
        rows.add("b_1", "statement.pdf", STATEMENT, 1),
        rows.add("a_2", "invoice-1001.pdf", INVOICE, 2),
        rows.add("a_3", "invoice (1).pdf", INVOICE, 3),
    ]

    files = await _run_output(monkeypatch, rows, listing)

    assert [f.artifact_id for f in files] == ["a_1", "b_1"]
    assert [rows.content(f) for f in files] == [INVOICE, STATEMENT]
    assert files[0] == listing[0]


@pytest.mark.asyncio
async def test_the_first_created_copy_stays_listed_whatever_the_modified_times_say(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    first = rows.add("a_1", "invoice.pdf", INVOICE, 0, modified_minutes=30)
    assert [f.artifact_id for f in await _run_output(monkeypatch, rows, [first])] == ["a_1"]

    late = rows.add("a_2", "Invoice.pdf", INVOICE, 5, modified_minutes=1)
    assert [f.artifact_id for f in await _run_output(monkeypatch, rows, [late, first])] == ["a_1"]


@pytest.mark.parametrize(
    "second",
    [
        pytest.param({"stored": STATEMENT, "claimed": INVOICE}, id="stored_bytes_differ_from_the_row_checksum"),
        pytest.param({"stored": None, "claimed": INVOICE}, id="stored_object_unreadable"),
        pytest.param({"stored": CUT_OFF, "claimed": INVOICE}, id="stored_object_cut_off"),
        pytest.param({"stored": INVOICE, "with_checksum": False}, id="row_without_checksum"),
        pytest.param({"stored": INVOICE, "with_size": False}, id="row_without_size"),
        pytest.param({"stored": INVOICE, "organization_id": "o_other"}, id="row_of_another_organization"),
        pytest.param({"stored": INVOICE, "run_id": "wr_other"}, id="row_of_another_run"),
    ],
)
@pytest.mark.asyncio
async def test_a_copy_whose_served_bytes_are_not_proven_equal_is_kept(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, second: dict[str, Any]
) -> None:
    listing = [rows.add("a_1", "invoice.pdf", INVOICE, 0), rows.add("a_2", "invoice (1).pdf", minutes=1, **second)]

    files = await _run_output(monkeypatch, rows, listing)

    assert [f.artifact_id for f in files] == ["a_1", "a_2"]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(CUT_OFF, id="cut_off_pdf"),
        pytest.param(HEADER_ONLY, id="header_only"),
        pytest.param(NOT_PDF, id="html_named_pdf"),
    ],
)
@pytest.mark.asyncio
async def test_equal_copies_that_are_not_complete_pdfs_are_all_kept(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, data: bytes
) -> None:
    listing = [
        rows.add("x_1", "report.pdf", data, 0),
        rows.add("x_2", "report (1).pdf", data, 1),
        rows.add("a_1", "invoice.pdf", INVOICE, 2),
        rows.add("b_1", "statement.pdf", STATEMENT, 3),
    ]

    files = await _run_output(monkeypatch, rows, listing)

    assert [f.artifact_id for f in files] == ["x_1", "x_2", "a_1", "b_1"]
    assert [f.checksum for f in files[:2]] == [_sha(data)] * 2
    assert sorted(rows.reads) == ["x_1", "x_2"]


@pytest.mark.parametrize("first_lookup", [RuntimeError("db down"), []], ids=["raises", "finds_nothing"])
@pytest.mark.usefixtures("share_urls")
@pytest.mark.asyncio
async def test_a_failed_content_check_keeps_every_copy(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, first_lookup: Exception | list[Artifact]
) -> None:
    a_1, a_2, b_1 = _code_block_rows(rows)
    listing = [a_1, a_2, rows.add("x_1", "invoice (2).pdf", INVOICE, 3)]
    # The first lookup fails or finds nothing, the refresh then asks for b_1 alone; x_1 is an earlier attempt's copy.
    lookups = AsyncMock(side_effect=[first_lookup, rows.artifacts[2:3]])
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookups)
    attempt_ids = AsyncMock(return_value={"a_1", "a_2", "b_1"})
    monkeypatch.setattr(WorkflowService, "_download_artifact_ids_for_attempt", attempt_ids)
    stored = {"task_screenshot_artifact_ids": [], **bind_downloaded_files_to_output({}, [*listing, b_1])}
    comparison = unique_pdfs.ResponseComparison()

    files = await _run_output(monkeypatch, rows, listing, comparison=comparison)
    for _ in range(2):
        assert await unique_pdfs.unique_pdf_file_infos(listing, "o_1", ["wr_1"], comparison) == listing
        refreshed = await WorkflowService()._refresh_output_urls(
            copy.deepcopy(stored), "o_1", "wr_1", attempt_rows=[_attempt(1, 0)], attempt_number=1, comparison=comparison
        )
        assert [f["artifact_id"] for f in refreshed["downloaded_files"]] == ["a_1", "a_2", "b_1"]
        assert refreshed["downloaded_file_artifact_ids"] == ["a_1", "a_2", "b_1"]
        assert refreshed["downloaded_file_urls"] == [f["url"] for f in refreshed["downloaded_files"]]

    assert files == listing
    assert [sorted(c.args[0]) for c in lookups.await_args_list] == [["a_1", "a_2", "x_1"], ["b_1"]]


@pytest.mark.parametrize("change", ["replaced", "endless"])
@pytest.mark.asyncio
async def test_a_stored_object_that_changes_while_read_is_kept(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, change: str
) -> None:
    listing = [rows.add("a_1", "invoice.pdf", INVOICE, 0), rows.add("a_2", "invoice (1).pdf", INVOICE, 1)]
    getattr(rows.store, change).add(rows.uri("a_2"))

    files = await _run_output(monkeypatch, rows, listing)

    assert [f.artifact_id for f in files] == ["a_1", "a_2"]
    assert sorted(rows.store.closed) == ["a_1", "a_2"]


@pytest.mark.asyncio
async def test_a_backend_that_only_returns_whole_objects_keeps_every_copy(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    listing = [rows.add("a_1", "invoice.pdf", INVOICE, 0), rows.add("a_2", "invoice (1).pdf", INVOICE, 1)]
    retrieve_artifact = AsyncMock(side_effect=lambda artifact: rows.objects[artifact.uri])
    whole_objects = SimpleNamespace(
        get_downloaded_files=AsyncMock(return_value=listing),
        manages_local_file_uri=lambda *_: False,
        retrieve_artifact=retrieve_artifact,
    )
    monkeypatch.setattr(app, "STORAGE", whole_objects)

    files, _ = await WorkflowService()._fetch_downloaded_files(
        SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1"), None
    )

    assert [f.artifact_id for f in files or []] == ["a_1", "a_2"]
    retrieve_artifact.assert_not_awaited()


def _attempt(number: int, minutes: int) -> SimpleNamespace:
    started = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(attempt_number=number, started_at=started, created_at=started, finished_at=None)


@pytest.mark.asyncio
async def test_each_attempt_lists_its_own_pdf_once_and_attempts_never_share_one(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    listing = [
        rows.add("old_1", "invoice.pdf", INVOICE, 0),
        rows.add("new_1", "invoice.pdf", INVOICE, 11),
        rows.add("new_2", "invoice (1).pdf", INVOICE, 12),
    ]
    attempts = [_attempt(1, 0), _attempt(2, 10)]
    owned = {1: {"old_1"}, 2: {"new_1", "new_2"}}
    monkeypatch.setattr(
        WorkflowService,
        "_download_artifact_ids_for_attempt",
        AsyncMock(side_effect=lambda **kw: owned[kw["attempt_number"]]),
    )

    current = await _run_output(monkeypatch, rows, listing, attempt_rows=attempts, attempt_number=2)
    previous = await _run_output(monkeypatch, rows, listing, attempt_rows=attempts, attempt_number=1)

    assert [f.artifact_id for f in current] == ["new_1"]
    assert [f.artifact_id for f in previous] == ["old_1"]


@pytest.mark.parametrize("workflow_run_id", ["wr_1", None])
@pytest.mark.asyncio
async def test_task_response_lists_each_pdf_content_once(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, workflow_run_id: str | None
) -> None:
    run_id = workflow_run_id or "tsk_1"
    listing = _code_block_rows(rows, run_id=run_id)
    _use_storage(monkeypatch, rows, listing)

    response = await _task_response(monkeypatch, workflow_run_id=workflow_run_id)

    assert [f.artifact_id for f in response.downloaded_files or []] == ["a_1", "b_1"]
    assert response.downloaded_file_urls == [f.url for f in response.downloaded_files or []]


@pytest.mark.asyncio
async def test_task_response_compares_pdfs_within_the_download_listing_budget(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    monkeypatch.setattr(unique_pdfs, "GET_DOWNLOADED_FILES_TIMEOUT", 0.3)
    listing = [rows.add(f"a_{n}", f"invoice-{n}.pdf", INVOICE, n, run_id="tsk_1") for n in (1, 2)]
    rows.store.stalled.update(rows.uri(f.artifact_id or "") for f in listing)
    storage = _use_storage(monkeypatch, rows, listing)
    listed_at: list[float] = []

    async def slow_listing(**_: Any) -> list[FileInfo]:
        listed_at.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.1)
        return listing

    monkeypatch.setattr(storage, "get_downloaded_files", slow_listing)
    deadlines: list[float] = []
    verify = unique_pdfs._verified

    async def record_deadline(*args: Any) -> Any:
        deadlines.append(args[3].deadline)
        return await verify(*args)

    monkeypatch.setattr(unique_pdfs, "_verified", record_deadline)

    response = await _task_response(monkeypatch, workflow_run_id=None)

    assert deadlines and deadlines[0] <= listed_at[0] + unique_pdfs.GET_DOWNLOADED_FILES_TIMEOUT, "budget renewed"
    assert [f.artifact_id for f in response.downloaded_files or []] == ["a_1", "a_2"], "an unfinished check keeps both"


class _Attempts:
    """The attempt repositories: two attempts of ``wr_1``, each owning the rows recorded for it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, store: _Rows, owners: dict[str, int]) -> None:
        self.rows = [_attempt(1, 0), _attempt(2, 10)]
        self.store, self.owners = store, owners
        monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
        monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(side_effect=self.get))
        monkeypatch.setattr(app.DATABASE.artifacts, "list_download_artifacts_for_attempt", self.current)
        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_artifacts_for_attempt", self.historical)

    async def get(self, workflow_run_id: str) -> list[SimpleNamespace]:
        return self.rows if workflow_run_id == "wr_1" else []

    def _owned(self, attempt_number: int) -> list[Artifact]:
        return [a for a in self.store.artifacts if self.owners.get(a.artifact_id) == attempt_number]

    async def current(self, *, attempt_number: int, **_: Any) -> list[Artifact]:
        return self._owned(attempt_number)

    async def historical(self, _run_id: str, _org: str, attempt_number: int, *_: Any, **__: Any) -> list[Artifact]:
        return self._owned(attempt_number)


async def _task_response(monkeypatch: pytest.MonkeyPatch, attempt_number: int | None = None, **task: Any) -> Any:
    now = datetime.now(UTC)
    organization = make_organization(now).model_copy(update={"organization_id": "o_1"})
    task = {"workflow_run_id": "wr_1", **task}
    built = make_task(now, organization, task_id="tsk_1", attempt_number=attempt_number, **task)
    step = make_step(now, built, step_id="stp_1", status=StepStatus.completed, order=0, output=None)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifact", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.artifacts, "get_latest_n_artifacts", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.tasks, "get_first_step", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=built))
    monkeypatch.setattr(app.DATABASE.tasks, "get_latest_step", AsyncMock(return_value=step))
    builder = cast(ForgeAgent, SimpleNamespace())
    monkeypatch.setattr(
        app, "agent", SimpleNamespace(build_task_response=lambda **kw: ForgeAgent.build_task_response(builder, **kw))
    )
    return await ForgeAgent.build_task_response(builder, task=built, last_step=step)


def _retried_run(store: _Rows) -> tuple[dict[str, int], list[FileInfo]]:
    names = {"old_1": ("invoice.pdf", 0), "old_2": ("invoice (1).pdf", 1), "new_1": ("invoice.pdf", 11)}
    names["new_2"] = ("invoice (1).pdf", 12)
    listing = [store.add(artifact_id, name, INVOICE, minutes) for artifact_id, (name, minutes) in names.items()]
    return {"old_1": 1, "old_2": 1, "new_1": 2, "new_2": 2}, listing


@pytest.mark.parametrize(
    ("attempt_number", "expected"),
    [
        pytest.param(2, ["old_1", "old_2", "new_1"], id="current_attempt_task"),
        pytest.param(1, ["old_1", "new_1", "new_2"], id="historical_attempt_task"),
    ],
)
@pytest.mark.asyncio
async def test_task_response_merges_pdf_copies_only_within_the_tasks_own_attempt(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, attempt_number: int, expected: list[str]
) -> None:
    owners, listing = _retried_run(rows)
    _Attempts(monkeypatch, rows, owners)
    _use_storage(monkeypatch, rows, listing)

    response = await _task_response(monkeypatch, attempt_number)

    listed = [f.artifact_id for f in response.downloaded_files or []]
    assert listed == expected
    assert response.downloaded_file_urls == [f.url for f in response.downloaded_files or []]
    own = [artifact_id for artifact_id in listed if owners[artifact_id] == attempt_number]
    assert len(own) == 1, "the task's own attempt lists its PDF once, from its own captures"


@pytest.mark.asyncio
async def test_task_webhook_payload_keeps_the_current_attempts_pdf_beside_an_earlier_attempts_copy(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    owners, listing = _retried_run(rows)
    _Attempts(monkeypatch, rows, owners)
    _use_storage(monkeypatch, rows, listing)
    monkeypatch.setattr(run_service, "get_run_response", AsyncMock(return_value=None))
    await _task_response(monkeypatch, 2, status=TaskStatus.completed)

    payload = await webhook_service._build_task_payload("o_1", "tsk_1", "task_v1", None)

    listed = [f["artifact_id"] for f in payload.payload["downloaded_files"]]
    assert listed == ["old_1", "old_2", "new_1"]
    assert payload.payload["downloaded_file_urls"] == [f["url"] for f in payload.payload["downloaded_files"]]


@pytest.mark.parametrize("unknown", ["attempt_lookup_fails", "row_without_attempt_or_time"])
@pytest.mark.asyncio
async def test_task_response_keeps_copies_whose_attempt_is_unknown(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, unknown: str
) -> None:
    listing = [rows.add("new_1", "invoice.pdf", INVOICE, 11), rows.add("new_2", "invoice (1).pdf", INVOICE, 12)]
    attempts = _Attempts(monkeypatch, rows, {"new_1": 2, "new_2": 2})
    if unknown == "attempt_lookup_fails":
        monkeypatch.setattr(
            app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(side_effect=RuntimeError("db down"))
        )
    else:
        attempts.owners.pop("new_2")
        listing[1] = listing[1].model_copy(update={"modified_at": None})
    _use_storage(monkeypatch, rows, listing)

    response = await _task_response(monkeypatch, 2)

    assert [f.artifact_id for f in response.downloaded_files or []] == ["new_1", "new_2"]


@pytest.fixture
def share_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=60))
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER,
        "resolve_share_url",
        AsyncMock(side_effect=lambda artifact, **_: f"https://files.example/{artifact.artifact_id}"),
    )


@pytest.mark.asyncio
async def test_block_output_refresh_lists_each_pdf_content_once_with_matching_ids(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    _code_block_rows(rows)
    _use_storage(monkeypatch, rows, [])
    authored = {"downloaded_file_urls": ["https://x.example/a.pdf", "https://x.example/a.pdf"], "files": ["a", "a"]}
    stored: dict[str, Any] = {
        "task_screenshot_artifact_ids": [],
        "downloaded_file_artifact_ids": ["a_1", "a_2", "b_1"],
        "downloaded_files": [{"artifact_id": [], "url": "stored/a_1"}, {"artifact_id": {}, "url": "stored/b_1"}],
        "authored": authored,
    }

    refreshed = await WorkflowService()._refresh_output_urls(stored, "o_1", "wr_1")

    assert refreshed["downloaded_file_artifact_ids"] == ["a_1", "b_1"]
    assert [f["artifact_id"] for f in refreshed["downloaded_files"]] == ["a_1", "b_1"]
    assert refreshed["downloaded_file_urls"] == ["https://files.example/a_1", "https://files.example/b_1"]
    assert [rows.content(f) for f in refreshed["downloaded_files"]] == [INVOICE, STATEMENT]
    assert refreshed["authored"] == authored


def _code_block_rows(store: _Rows, **row: Any) -> list[FileInfo]:
    return [
        store.add(artifact_id, name, data, minutes, **row)
        for artifact_id, name, data, minutes in [
            ("a_1", "invoice.pdf", INVOICE, 0),
            ("a_2", "invoice (1).pdf", INVOICE, 1),
            ("b_1", "statement.pdf", STATEMENT, 2),
        ]
    ]


@pytest.mark.asyncio
async def test_code_block_download_registration_lists_each_pdf_content_once_in_the_public_output(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    _use_storage(monkeypatch, rows, [])
    stored = bind_downloaded_files_to_output({"rows": 3}, _code_block_rows(rows))
    persisted = copy.deepcopy(stored)

    refreshed = await WorkflowService()._refresh_output_urls(copy.deepcopy(stored), "o_1", "wr_1")

    assert refreshed["downloaded_file_artifact_ids"] == ["a_1", "b_1"]
    assert [f["artifact_id"] for f in refreshed["downloaded_files"]] == ["a_1", "b_1"]
    assert refreshed["downloaded_file_urls"] == [f["url"] for f in refreshed["downloaded_files"]]
    assert [rows.content(f) for f in refreshed["downloaded_files"]] == [INVOICE, STATEMENT]
    assert refreshed["rows"] == 3
    assert stored == persisted


@pytest.mark.parametrize(
    "authored",
    [
        pytest.param(["a_1", "a_2", "unknown"], id="an_id_that_is_not_a_download_of_this_run"),
        pytest.param(["a_1", "a_2"], id="ids_that_do_not_cover_every_listed_file"),
    ],
)
@pytest.mark.asyncio
async def test_registration_shaped_output_without_matching_download_rows_is_left_unchanged(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None, authored: list[str]
) -> None:
    _use_storage(monkeypatch, rows, [])
    output = bind_downloaded_files_to_output({"rows": 3}, _code_block_rows(rows))
    output["downloaded_file_artifact_ids"] = authored
    original = copy.deepcopy(output)

    refreshed = await WorkflowService()._refresh_output_urls(output, "o_1", "wr_1")

    assert refreshed == original
    assert rows.reads == []


def _download_output(artifact_ids: list[str]) -> dict[str, Any]:
    return {"task_screenshot_artifact_ids": [], "downloaded_file_artifact_ids": list(artifact_ids)}


@pytest.mark.asyncio
async def test_a_task_v2_response_looks_up_and_reads_each_download_once_and_keeps_another_runs_copy(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    by_run = {"wr_1": _code_block_rows(rows), "oc_1": [rows.add("o_1", "o.pdf", INVOICE, 3, run_id="oc_1")]}
    storage = _use_storage(monkeypatch, rows, [])
    monkeypatch.setattr(storage, "get_downloaded_files", AsyncMock(side_effect=lambda **kw: by_run[kw["run_id"]]))
    lookups = AsyncMock(side_effect=rows.get_artifacts_by_ids)
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookups)
    comparison, service = unique_pdfs.ResponseComparison(), WorkflowService()
    run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

    files, _ = await service._fetch_downloaded_files(
        run, SimpleNamespace(observer_cruise_id="oc_1"), comparison=comparison
    )
    outputs: dict[str, Any] = {
        "task": _download_output(["a_1", "a_1", "a_2", "b_1"]),
        "loop": [{"code": bind_downloaded_files_to_output({"row": 1}, by_run["wr_1"][:2])}],
        "second_copy_only": _download_output(["a_2"]),
        "mixed_runs": _download_output(["a_1", "o_1"]),
    }
    refreshed = await service._refresh_output_urls(outputs, "o_1", "wr_1", comparison=comparison)

    assert [f.artifact_id for f in files or []] == ["a_1", "b_1"]
    assert refreshed["task"]["downloaded_file_artifact_ids"] == ["a_1", "b_1"]
    assert refreshed["loop"][0]["code"]["downloaded_file_artifact_ids"] == ["a_1"]
    assert [f["artifact_id"] for f in refreshed["second_copy_only"]["downloaded_files"]] == ["a_2"]
    assert refreshed["mixed_runs"]["downloaded_file_artifact_ids"] == ["a_1", "o_1"]
    assert sorted(rows.reads) == ["a_1", "a_2", "o_1"]
    assert [sorted(call.args[0]) for call in lookups.await_args_list] == [["a_1", "a_2", "o_1"], ["b_1"]]


@pytest.mark.asyncio
async def test_an_output_refresh_compares_within_the_run_it_started_with(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    _code_block_rows(rows, run_id="wr_initial")
    _use_storage(monkeypatch, rows, [])
    context = SkyvernContext(run_id="wr_initial")
    read_rows = rows.get_artifacts_by_ids

    async def read_rows_then_switch_run(artifact_ids: list[str], organization_id: str) -> list[Artifact]:
        context.run_id = "wr_changed"
        return await read_rows(artifact_ids, organization_id)

    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", read_rows_then_switch_run)
    skyvern_context.set(context)
    try:
        refreshed = await WorkflowService()._refresh_output_urls(_download_output(["a_1", "a_2", "b_1"]), "o_1", "wr_1")
    finally:
        skyvern_context.reset()

    assert refreshed["downloaded_file_artifact_ids"] == ["a_1", "b_1"]


@pytest.mark.asyncio
async def test_an_unverifiable_copy_is_read_once_per_response_and_again_on_the_next(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    rows.add("a_1", "invoice.pdf", INVOICE, 0)
    rows.add("a_2", "invoice (1).pdf", INVOICE, 1)
    rows.store.replaced.add(rows.uri("a_2"))
    _use_storage(monkeypatch, rows, [])

    for response in (1, 2):
        outputs = {"first": _download_output(["a_1", "a_2"]), "again": _download_output(["a_1", "a_2"])}
        refreshed = await WorkflowService()._refresh_output_urls(outputs, "o_1", "wr_1")
        assert [refreshed[key]["downloaded_file_artifact_ids"] for key in outputs] == [["a_1", "a_2"]] * 2
        assert sorted(rows.reads) == sorted(["a_1", "a_2"] * response)


@pytest.mark.asyncio
async def test_one_response_shares_one_comparison_deadline_and_closes_every_stream(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    monkeypatch.setattr(unique_pdfs, "GET_DOWNLOADED_FILES_TIMEOUT", 0.3)
    outputs: dict[str, Any] = {}
    for block in ("x", "y", "z"):
        for copy_ in (1, 2):
            rows.add(f"{block}_{copy_}", f"{block}-{copy_}.pdf", INVOICE, copy_)
            rows.store.stalled.add(rows.uri(f"{block}_{copy_}"))
        outputs[block] = _download_output([f"{block}_1", f"{block}_2"])
    _use_storage(monkeypatch, rows, [])
    comparisons: list[unique_pdfs.ResponseComparison] = []
    original_deadlines: list[float] = []
    construct = unique_pdfs.ResponseComparison.__init__

    def observe_construction(comparison: unique_pdfs.ResponseComparison) -> None:
        construct(comparison)
        comparisons.append(comparison)
        original_deadlines.append(comparison.deadline)  # Frozen now: the object stays mutable during the response.

    timeouts: list[asyncio.Timeout] = []

    def record_timeout(when: float | None) -> asyncio.Timeout:
        timeout = asyncio.timeout_at(when)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(unique_pdfs.ResponseComparison, "__init__", observe_construction)
    monkeypatch.setattr(unique_pdfs, "asyncio", ScopedAsyncio(timeout_at=record_timeout))

    response = asyncio.ensure_future(WorkflowService()._refresh_output_urls(outputs, "o_1", "wr_1"))
    # Generous hang guard only: the response must finish on its own; cancelling it here would fake a timed-out result.
    done, _ = await asyncio.wait({response}, timeout=10)
    try:
        assert response in done, "the comparison never timed out, so the stalled reads never ended"
    finally:
        if not response.done():
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)
    refreshed = response.result()

    # The contract is one budget per response, enforced by real timers; time spent outside it is not asserted here.
    assert len(comparisons) == 1 and math.isfinite(deadline := original_deadlines[0])
    assert len(timeouts) == len(outputs)
    assert [timeout.when() for timeout in timeouts] == [deadline] * len(outputs)
    assert all(timeout.expired() for timeout in timeouts)
    assert [refreshed[block]["downloaded_file_artifact_ids"] for block in "xyz"] == [
        [f"{block}_1", f"{block}_2"] for block in "xyz"
    ]
    assert [[entry["artifact_id"] for entry in refreshed[block]["downloaded_files"]] for block in "xyz"] == [
        [f"{block}_1", f"{block}_2"] for block in "xyz"
    ]
    assert [refreshed[block]["downloaded_file_urls"] for block in "xyz"] == [
        [f"https://files.example/{block}_1", f"https://files.example/{block}_2"] for block in "xyz"
    ]
    assert rows.reads, "no stalled stream was opened, so closing every stream proves nothing"
    assert sorted(rows.store.closed) == sorted(rows.reads)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("path", ["local_read", "stoppable_comparison"])
@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_the_comparison_thread(
    monkeypatch: pytest.MonkeyPatch, path: str, fails: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking(*_: Any) -> None:
        entered.set()
        try:
            assert release.wait(5), "never released"
            if fails:
                raise ValueError("comparison failed")
        finally:
            finished.set()

    if path == "local_read":
        monkeypatch.setattr(unique_pdfs, "stable_pdf_digest", blocking)
        task = asyncio.ensure_future(unique_pdfs.ResponseComparison().read_local("invoice.pdf", 1))
    else:
        monkeypatch.setattr(unique_pdfs, "GET_DOWNLOADED_FILES_TIMEOUT", 0.05)
        task = asyncio.ensure_future(unique_pdfs.run_stoppable_comparison(blocking))
    try:
        await asyncio.to_thread(entered.wait, 5)
        if path == "stoppable_comparison":
            await asyncio.sleep(0.2)  # past the comparison budget, now settling the thread
        for _ in range(2):
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "the comparison thread is still running"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancelling_a_response_closes_its_comparison_streams(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    rows.add("a_1", "invoice.pdf", INVOICE, 0)
    rows.add("a_2", "invoice (1).pdf", INVOICE, 1)
    rows.store.stalled.update({rows.uri("a_1"), rows.uri("a_2")})
    _use_storage(monkeypatch, rows, [])
    refresh = asyncio.ensure_future(
        WorkflowService()._refresh_output_urls({"first": _download_output(["a_1", "a_2"])}, "o_1", "wr_1")
    )
    for _ in range(200):
        if len(rows.reads) == 2:
            break
        await asyncio.sleep(0)

    refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert sorted(rows.store.closed) == ["a_1", "a_2"]


@pytest.mark.asyncio
async def test_legacy_block_output_refresh_by_filename_lists_each_pdf_content_once(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, share_urls: None
) -> None:
    _code_block_rows(rows)
    rows.add("c_1", "other-block.pdf", RECEIPT, 3)
    _use_storage(monkeypatch, rows, [])
    monkeypatch.setattr(
        app.DATABASE.artifacts, "list_artifacts_for_run_by_type", AsyncMock(return_value=rows.artifacts)
    )
    # A task_v2 block output carries screenshot ids but persists its downloads by filename only.
    stored: dict[str, Any] = {
        "task_id": "oc_1",
        "task_screenshot_artifact_ids": [],
        "downloaded_files": [{"filename": name} for name in ("invoice.pdf", "invoice (1).pdf", "statement.pdf")],
    }

    refreshed = await WorkflowService()._refresh_output_urls(stored, "o_1", "wr_1")

    assert [f["artifact_id"] for f in refreshed["downloaded_files"]] == ["a_1", "b_1"]
    assert refreshed["downloaded_file_urls"] == ["https://files.example/a_1", "https://files.example/b_1"]
    assert [rows.content(f) for f in refreshed["downloaded_files"]] == [INVOICE, STATEMENT]


def _task_block_output(files: list[FileInfo]) -> dict[str, Any]:
    """A task block's output as persisted: its screenshots and every download it registered."""
    return {
        "task_screenshot_artifact_ids": ["shot_1"],
        "task_screenshots": ["https://stored.example/shot_1"],
        "downloaded_files": [f.model_dump(mode="json") for f in files],
        "downloaded_file_urls": [f.url for f in files],
        "downloaded_file_artifact_ids": [f.artifact_id for f in files],
    }


def _run_block(block_id: str, output: Any, minutes: int, run_id: str = "wr_1", **fields: Any) -> WorkflowRunBlock:
    stamp = T0 + timedelta(minutes=minutes)
    listed = output.get("downloaded_files") if isinstance(output, dict) else None
    return WorkflowRunBlock(
        workflow_run_block_id=block_id,
        workflow_run_id=run_id,
        organization_id="o_1",
        output=output,
        downloaded_file_count=len(listed) if isinstance(listed, list) else None,
        created_at=stamp,
        modified_at=stamp,
        **{"attempt_number": 1, "block_type": BlockType.TASK, **fields},
    )


class _Timeline:
    """The run timeline endpoint over in-memory block rows; ``served`` keeps every row object handed to it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, blocks: dict[str, list[WorkflowRunBlock]]) -> None:
        self.blocks = blocks
        self.served: list[WorkflowRunBlock] = []
        self.items: list[dict[str, Any]] = []
        self.prefer_capped: bool | None = None
        monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
        monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", self.read)
        monkeypatch.setattr(app.DATABASE.tasks, "get_tasks_actions", AsyncMock(return_value=[]))
        monkeypatch.setattr(app.DATABASE.observer, "get_task_v2_by_workflow_run_id", AsyncMock(return_value=None))
        monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            app.DATABASE.workflow_runs,
            "get_workflow_run",
            AsyncMock(
                side_effect=lambda workflow_run_id, **_: SimpleNamespace(
                    workflow_run_id=workflow_run_id, organization_id="o_1", parent_workflow_run_id=None
                )
            ),
        )
        run = SimpleNamespace(task_run_type=RunType.workflow_run)
        monkeypatch.setattr(app.DATABASE.tasks, "get_run", AsyncMock(return_value=run))
        monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *_args, **_kwargs: None)
        # Kept entries carry their stored URLs, so the listing never depends on signing one.
        self.signing = AsyncMock(side_effect=RuntimeError("signing unavailable"))
        monkeypatch.setattr(app.ARTIFACT_MANAGER, "resolve_share_url", self.signing)
        monkeypatch.setattr(app.ARTIFACT_MANAGER, "resolve_artifact_url_expiry_seconds", self.signing)
        self.api = shared_route_app(monkeypatch)
        self.api.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(organization_id="o_1")

    async def read(self, workflow_run_id: str, prefer_capped: bool = False, **_: Any) -> list[WorkflowRunBlock]:
        assert self.prefer_capped in (None, prefer_capped)
        self.prefer_capped = prefer_capped
        served = [block.model_copy(deep=True) for block in self.blocks.get(workflow_run_id, [])]
        self.served.extend(served)
        return served

    async def get(self, user_agent: str | None = None) -> dict[str, dict[str, Any]]:
        headers = {"x-user-agent": user_agent} if user_agent else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url="http://test") as client:
            response = await client.get("/v1/runs/wr_1/timeline", headers=headers)
        assert response.status_code == 200, response.text
        self.items = response.json()
        return {item["block"]["workflow_run_block_id"]: item["block"] for item in self.items if item["block"]}

    def assert_rows_untouched(self) -> None:
        stored = {block.workflow_run_block_id: block for blocks in self.blocks.values() for block in blocks}
        assert self.served and all(row == stored[row.workflow_run_block_id] for row in self.served)


def _listed(block: dict[str, Any]) -> list[str]:
    """The block's download ids after checking its ids, files and URLs still describe the same entries."""
    output = block["output"]
    ids = output["downloaded_file_artifact_ids"]
    assert [entry["artifact_id"] for entry in output["downloaded_files"]] == ids
    assert output["downloaded_file_urls"] == [entry["url"] for entry in output["downloaded_files"]]
    assert block["downloaded_file_count"] == len(ids)
    return ids


@pytest.mark.asyncio
async def test_run_timeline_lists_each_pdf_content_once_per_block_and_keeps_entries_it_cannot_prove(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    captures = _code_block_rows(rows)
    cold = [rows.add("c_1", "receipt.pdf", RECEIPT, 3), rows.add("c_2", "receipt (1).pdf", RECEIPT, 4)]
    _use_storage(monkeypatch, rows, [*captures, *cold])
    lagging, lookup = {"c_2"}, rows.get_artifacts_by_ids

    async def lookup_behind_by_one_row(artifact_ids: list[str], organization_id: str) -> list[Artifact]:
        missed = lagging.intersection(artifact_ids)
        lagging.difference_update(missed)
        return [row for row in await lookup(artifact_ids, organization_id) if row.artifact_id not in missed]

    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookup_behind_by_one_row)
    code = bind_downloaded_files_to_output({"rows": 3}, captures)
    timeline = _Timeline(
        monkeypatch,
        {
            "wr_1": [
                _run_block("wrb_task", _task_block_output(captures), 0),
                _run_block("wrb_second_copy", _task_block_output(captures[1:2]), 1),
                _run_block("wrb_cold", _task_block_output(cold), 2),
                _run_block("wrb_code", code, 3, block_type=BlockType.CODE),
            ]
        },
    )

    blocks = await timeline.get()

    assert _listed(blocks["wrb_task"]) == ["a_1", "b_1"]
    assert blocks["wrb_task"]["output"]["downloaded_file_urls"] == [captures[0].url, captures[2].url]
    assert blocks["wrb_task"]["output"]["task_screenshots"] == ["https://stored.example/shot_1"]
    assert _listed(blocks["wrb_code"]) == ["a_1", "b_1"] and blocks["wrb_code"]["output"]["rows"] == 3
    assert _listed(blocks["wrb_second_copy"]) == ["a_1"], "the copy Outputs keeps stands in for this block's copy"
    assert _listed(blocks["wrb_cold"]) == ["c_1", "c_2"], "an entry whose row was not found stays listed"
    timeline.assert_rows_untouched()


def _shown(block: dict[str, Any]) -> list[dict[str, Any]]:
    return block["output"]["downloaded_files"]


@pytest.mark.parametrize(
    ("created", "listed_order", "subsets"),
    [
        pytest.param({"A": 0, "B": 1, "C": 2}, "ABC", ["BC", "B"], id="three_copies_subset_and_singleton"),
        pytest.param({"A": 0, "B": 1}, "AB", ["B"], id="two_copies_singleton"),
        pytest.param({"A": 1, "B": 1, "C": 2}, "BAC", ["AC", "A"], id="tied_creation_listing_order_decides"),
        pytest.param({"A": 1, "B": 1, "C": 2}, "ABC", ["CB", "B"], id="tied_creation_permuted_block_order"),
    ],
)
@pytest.mark.asyncio
async def test_timeline_blocks_show_the_copy_outputs_keeps_for_their_content(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, created: dict[str, int], listed_order: str, subsets: list[str]
) -> None:
    names = {"A": "download-1a2b.pdf", "B": "download.pdf", "C": "download (1).pdf"}
    captures = {key: rows.add(f"a_{key.lower()}", names[key], INVOICE, minutes) for key, minutes in created.items()}
    blocks_by_subset = [
        _run_block(f"wrb_{subset}", _task_block_output([captures[key] for key in subset]), index)
        for index, subset in enumerate(subsets)
    ]
    timeline = _Timeline(monkeypatch, {"wr_1": blocks_by_subset})

    outputs = await _run_output(monkeypatch, rows, [captures[key] for key in listed_order])
    blocks = await timeline.get()

    assert len(outputs) == 1
    for subset in subsets:
        assert _listed(blocks[f"wrb_{subset}"]) == [outputs[0].artifact_id]
        assert _shown(blocks[f"wrb_{subset}"]) == [outputs[0].model_dump(mode="json")]
    timeline.assert_rows_untouched()


_KEYRING = '{"current_kid": "k1", "keys": {"k1": {"secret": "deadbeef"}}}'


async def _install_s3_producer(
    monkeypatch: pytest.MonkeyPatch, store: _Rows, sqlite_engine_factory: SqliteEngineFactory
) -> tuple[S3Storage, AgentDB]:
    """The real S3 download producer over a database holding ``store``'s rows; only object bytes come from ``store``."""
    database = await _attempt_query_database(sqlite_engine_factory, store)
    storage = S3Storage()
    storage.async_client = MagicMock()
    monkeypatch.setattr(storage, "stat_artifact", store.store.stat_artifact)
    monkeypatch.setattr(storage, "open_artifact_stream", store.store.open_artifact_stream)
    monkeypatch.setattr(storage, "can_verify_artifact", lambda _artifact: True)
    monkeypatch.setattr(app, "STORAGE", storage)
    monkeypatch.setattr(app.DATABASE, "artifacts", database.artifacts)
    monkeypatch.setattr(app.DATABASE, "organizations", database.organizations)
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING)
    return storage, database


async def _write_again(database: AgentDB, artifact_id: str) -> None:
    """Delete and re-insert an unchanged row, so a tie on created_at can come back in the other order."""
    async with database.Session() as session:
        row = await session.get(ArtifactModel, artifact_id)
        assert row is not None
        values = {column.name: getattr(row, column.name) for column in ArtifactModel.__table__.columns}
        await session.delete(row)
        await session.flush()
        session.add(ArtifactModel(**values))
        await session.commit()


def _without_signature(file_info: dict[str, Any]) -> dict[str, Any]:
    return {**file_info, "url": file_info["url"].split("?")[0]}


async def _status_response(
    monkeypatch: pytest.MonkeyPatch, timeline: _Timeline, labels: dict[str, Any]
) -> dict[str, Any]:
    """The run status endpoint's whole response, with ``labels`` as the stored block outputs."""
    now = datetime(2024, 1, 1, tzinfo=UTC)
    run = WorkflowRun(
        workflow_run_id="wr_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        status=WorkflowRunStatus.completed,
        created_at=now,
        modified_at=now,
    )
    workflow = SimpleNamespace(workflow_permanent_id="wpid_1", title="Invoices")
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_for_workflow_run", AsyncMock(return_value=workflow))
    monkeypatch.setattr(app.DATABASE.tasks, "get_tasks_for_workflow_run_status", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_block_errors", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_retried_by", AsyncMock(return_value=None))
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", SimpleNamespace(workflow_run_contexts={}))
    service = app.WORKFLOW_SERVICE
    monkeypatch.setattr(service, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "get_output_parameter_workflow_run_output_parameter_tuples",
        AsyncMock(
            return_value=[
                (SimpleNamespace(key=key), SimpleNamespace(value=copy.deepcopy(v))) for key, v in labels.items()
            ]
        ),
    )
    monkeypatch.setattr(service, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=timeline.api), base_url="http://test") as client:
        response = await client.get("/api/v1/workflows/runs/wr_1/")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("copies", "winner", "between"),
    [
        pytest.param(
            {"invoice.pdf": (0, 0), "report.custom": (1, 1)}, "report.custom", None, id="configured_custom_ext"
        ),
        pytest.param(
            {"invoice.pdf": (0, 0), "report.custom": (1, 1), "report.pdf": (2, 1)},
            "report.pdf",
            None,
            id="configured_tie",
        ),
        pytest.param({"invoice.pdf": (0, 0), "invoice.custom": (1, 0)}, "invoice.pdf", None, id="unconfigured_pdf_ext"),
        pytest.param({"download-1a2b.pdf": (0, 0), "download.pdf": (0, 0)}, "download-1a2b.pdf", "rewrite", id="tie"),
        pytest.param(
            {"download.pdf": (0, 0), "download-1a2b.pdf": (1, 0)}, "download-1a2b.pdf", "session", id="session"
        ),
    ],
)
@pytest.mark.asyncio
async def test_outputs_labels_and_timeline_show_one_canonical_copy_of_equal_pdfs(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    copies: dict[str, tuple[int, int]],
    winner: str,
    between: str | None,
) -> None:
    captures = {
        name: rows.add(f"a_{i}", name, INVOICE, minutes, configured=bool(c))
        for i, (name, (minutes, c)) in enumerate(copies.items())
    }
    listed = [[c] for c in captures.values()]
    if between == "session":
        listed[1].insert(0, captures["download.pdf"])
    outputs = {f"wrb_{i}": _task_block_output(files) for i, files in enumerate(listed)}
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block(b, o, i) for i, (b, o) in enumerate(outputs.items())]})
    _, database = await _install_s3_producer(monkeypatch, rows, sqlite_engine_factory)
    if between == "session":
        async with database.Session() as session:
            row = await session.get(ArtifactModel, "a_0")
            assert row is not None
            row.browser_session_id = "pbs_1"
            await session.commit()

    status = await _status_response(monkeypatch, timeline, {f"{b}_output": o for b, o in outputs.items()})
    if between == "rewrite":
        await _write_again(database, "a_0")
    blocks = await timeline.get()

    assert [file_info["artifact_id"] for file_info in status["downloaded_files"]] == [captures[winner].artifact_id]
    # Each response signs its own URLs, so only the signature may differ between them.
    expected = _without_signature(status["downloaded_files"][0])
    for block in outputs:
        label = status["outputs"][f"{block}_output"]
        assert label["downloaded_file_artifact_ids"] == [expected["artifact_id"]], block
        assert [_without_signature(entry) for entry in label["downloaded_files"]] == [expected]
        assert [_without_signature(entry) for entry in _shown(blocks[block])] == [expected]
    timeline.assert_rows_untouched()


@pytest.mark.parametrize(
    ("producer", "registration", "shape"),
    [
        pytest.param("fallback_refetch", "with_receipt", "task", id="fallback_saved"),
        pytest.param("fallback_refetch", "after_reset", "task", id="fallback_saved_context_reset_first"),
        pytest.param("fallback_refetch", "unchanged_skip", "host", id="fallback_saved_then_unchanged_upload"),
        pytest.param("fallback_empty", "with_receipt", "task", id="fallback_refetch_empty"),
        pytest.param("native_finalize", "with_receipt", "host", id="native_finalize_rename"),
    ],
)
@pytest.mark.asyncio
async def test_a_configured_download_keeps_its_name_from_completion_to_every_view(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    tmp_path: Path,
    producer: str,
    registration: str,
    shape: str,
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    run_dir = Path(get_download_dir("wr_1"))
    ordinary = _write(run_dir / "invoice.pdf", INVOICE, 0)
    timeline = _Timeline(monkeypatch, blocks := {})
    storage, database = await _install_s3_producer(monkeypatch, rows, sqlite_engine_factory)
    # Only the object transport is faked: the uploaded bytes become what the stored object serves.
    storage.async_client.upload_file_from_path = AsyncMock(
        side_effect=lambda uri, file_path, **_: rows.objects.__setitem__(uri, Path(file_path).read_bytes())
    )
    producing = SkyvernContext(organization_id="o_1", run_id="wr_1", workflow_run_id="wr_1", download_suffix="stmt")
    skyvern_context.set(producing)
    try:
        await storage.save_downloaded_files("o_1", "wr_1")
        if producer == "native_finalize":
            _write(run_dir / "download-1a2b.pdf", INVOICE, 1)
            with patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=run_dir):
                await ForgeAgent()._finalize_downloaded_files_for_task(
                    cast(Any, SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1", browser_session_id=None)),
                    organization_id="o_1",
                    download_suffix="stmt",
                    list_files_before=[str(ordinary)],
                    randomize_if_missing=False,
                )
        else:
            body = INVOICE if producer == "fallback_refetch" else b""
            monkeypatch.setattr(handler_module, "fetch_file_bytes", AsyncMock(return_value=SimpleNamespace(body=body)))
            download = SimpleNamespace(
                url="https://files.example/invoice",
                suggested_filename="invoice.pdf",
                failure=AsyncMock(return_value=None),
                save_as=AsyncMock(side_effect=RuntimeError("target closed")),
            )
            await handler_module._save_adopted_session_download(
                download, MagicMock(), run_dir, authorize_request_hop=MagicMock(), request_headers={}
            )
        named = [p.name for p in run_dir.iterdir() if p != ordinary]
        selected = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
            download_files_path=str(run_dir), max_file_count=300
        )
        if registration != "with_receipt":
            skyvern_context.set(SkyvernContext(organization_id="o_1", run_id="wr_1", workflow_run_id="wr_1"))
        await storage.save_downloaded_files("o_1", "wr_1")
        if registration == "unchanged_skip":
            skyvern_context.set(producing)
            await storage.save_downloaded_files("o_1", "wr_1")
    finally:
        skyvern_context.reset()
    # A later cleanup in a fresh context finds the file unchanged and keeps what the earlier one proved.
    await storage.save_downloaded_files("o_1", "wr_1")
    # Delivery after the reset has no receipt: it sends the earliest .pdf copy, and the views below still read the rows.
    unknown = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
        download_files_path=str(run_dir), max_file_count=300
    )

    expect_named = bool(named) and registration != "after_reset"
    assert len(named) == (producer != "fallback_empty") and all(name.startswith("stmt") for name in named)
    assert [Path(p).name for p in selected] == (named or ["invoice.pdf"])
    assert [Path(p).name for p in unknown] == ["invoice.pdf"]
    assert sorted(p.name for p in run_dir.iterdir()) == sorted(["invoice.pdf", *named])
    saved = await database.artifacts.list_artifacts_for_run_by_type("wr_1", "o_1", ArtifactType.DOWNLOAD)
    saved.sort(key=lambda row: row.created_at)
    assert {Path(row.uri).name: row.configured_name_sha256 for row in saved} == {
        "invoice.pdf": None,
        **{name: _sha(INVOICE) if expect_named else None for name in named},
    }
    # The marker is internal: responses built from these rows and the published schema never carry it.
    assert not any("configured_name_sha256" in row.model_dump(mode="json") for row in saved)
    assert "configured_name_sha256" not in Artifact.model_json_schema(mode="serialization")["properties"]
    for index, row in enumerate(saved):
        stored = FileInfo(
            url=f"https://files.example/{row.artifact_id}",
            checksum=row.checksum,
            filename=Path(row.uri).name,
            file_size=row.file_size,
            modified_at=row.modified_at,
            artifact_id=row.artifact_id,
        )
        host = {"block_type": BlockType.CODE} if shape == "host" else {}
        output = bind_downloaded_files_to_output({"rows": 1}, [stored]) if host else _task_block_output([stored])
        blocks.setdefault("wr_1", []).append(_run_block(f"wrb_{index}", output, index, **host))

    status = await _status_response(
        monkeypatch, timeline, {f"{b.workflow_run_block_id}_output": b.output for b in blocks["wr_1"]}
    )
    shown = await timeline.get()

    winner = next(row for row in saved if Path(row.uri).name == (named[0] if expect_named else "invoice.pdf"))
    assert [file_info["artifact_id"] for file_info in status["downloaded_files"]] == [winner.artifact_id]
    expected = _without_signature(status["downloaded_files"][0])
    for block in blocks["wr_1"]:
        label = status["outputs"][f"{block.workflow_run_block_id}_output"]
        assert [_without_signature(entry) for entry in label["downloaded_files"]] == [expected]
        # A copy with no competitor is admitted without an Outputs lookup, so the timeline keeps its stored entry.
        kept = block.output["downloaded_files"] if len(saved) == 1 else [expected]
        assert [_without_signature(entry) for entry in _shown(shown[block.workflow_run_block_id])] == kept


@pytest.mark.parametrize("shape", ["task", "host"])
@pytest.mark.asyncio
async def test_labels_keep_each_unregistered_canonical_copy_while_the_timeline_keeps_the_registered_originals(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    tmp_path: Path,
    shape: str,
) -> None:
    # Registered .bin snapshots of two different PDFs, beside unregistered .pdf twins still in the run directory.
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    storage = LocalStorage(str(tmp_path / "artifacts"))
    snapshots = Path(storage.artifact_path) / "downloads" / settings.ENV / "o_1" / "wr_1"
    snapshots.mkdir(parents=True)
    run_dir = Path(get_download_dir("wr_1"))
    database = await _attempt_query_database(sqlite_engine_factory, rows)
    originals = []
    for index, (name, data) in enumerate([("first", INVOICE), ("second", STATEMENT)]):
        _write(run_dir / f"{name}.pdf", data, index + 5)
        snapshot = _write(snapshots / f"{name}.bin", data, index)
        row = await database.artifacts.create_artifact(
            artifact_id=f"a_{name}",
            artifact_type=ArtifactType.DOWNLOAD,
            uri=f"file://{snapshot}",
            organization_id="o_1",
            run_id="wr_1",
            checksum=_sha(data),
            file_size=len(data),
        )
        originals.append(
            FileInfo(
                url=row.uri,
                checksum=row.checksum,
                filename=snapshot.name,
                file_size=row.file_size,
                modified_at=row.modified_at,
                artifact_id=row.artifact_id,
            )
        )
    host = {"block_type": BlockType.CODE} if shape == "host" else {}
    output = bind_downloaded_files_to_output({"rows": 1}, originals) if host else _task_block_output(originals)
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_both", output, 0, **host)]})
    monkeypatch.setattr(app, "STORAGE", storage)
    monkeypatch.setattr(app.DATABASE, "artifacts", database.artifacts)
    monkeypatch.setattr(app.DATABASE, "organizations", database.organizations)
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _KEYRING)

    status = await _status_response(monkeypatch, timeline, {"wrb_both_output": output})
    blocks = await timeline.get()

    live = sorted(str(run_dir / f"{name}.pdf") for name in ("first", "second"))
    assert sorted(f["url"].removeprefix("file://") for f in status["downloaded_files"]) == live
    label = status["outputs"]["wrb_both_output"]
    by_url = sorted(map(_without_signature, status["downloaded_files"]), key=lambda f: f["url"])
    shown = sorted(map(_without_signature, label["downloaded_files"]), key=lambda f: f["url"])
    assert shown == by_url, "each distinct content keeps its own canonical copy"
    assert label["downloaded_file_urls"] == [f["url"] for f in label["downloaded_files"]]
    assert label["downloaded_file_artifact_ids"] == [], "the id list names registered copies only"
    assert _listed(blocks["wrb_both"]) == ["a_first", "a_second"], "the timeline admits only registered copies"


@pytest.mark.parametrize("case", ["stale", "unchanged", "other_organization"])
def test_concurrent_receipt_reads_return_one_answer_and_never_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    path = _write(tmp_path / "invoice.custom", INVOICE, 0)
    context = SkyvernContext(organization_id="o_1", run_id="wr_1", workflow_run_id="wr_1")
    asyncio.run(context.record_configured_download(str(path), "wr_1"))
    if case == "stale":
        _write(path, INVOICE, 9)
    # Both readers hold the same receipt before either acts on it.
    both_read = threading.Barrier(2, timeout=5)
    identity = skyvern_context._file_identity

    def read_identity(resolved: str) -> tuple[int, int, int, int]:
        current = identity(resolved)
        both_read.wait()
        return current

    organization = "o_2" if case == "other_organization" else "o_1"
    if case != "other_organization":
        # The other organization's read is refused before any stat, so there is nothing to synchronise.
        monkeypatch.setattr(skyvern_context, "_file_identity", read_identity)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = [pool.submit(context.configured_download_digest, str(path), organization, "wr_1") for _ in range(2)]
        results = [answer.result() for answer in answers]

    assert results == ([_sha(INVOICE)] * 2 if case == "unchanged" else [None, None])
    assert bool(context.configured_downloads) is (case != "stale"), "only a stale receipt is consumed"


@pytest.mark.parametrize("failure", ["lookup", "promotion", "cancellation"])
@pytest.mark.asyncio
async def test_a_failed_name_credit_for_an_unchanged_download_leaves_the_rest_of_the_save_running(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    tmp_path: Path,
    failure: str,
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    run_dir = Path(get_download_dir("wr_1"))
    named = _write(run_dir / "a.pdf", INVOICE, 0)
    storage, database = await _install_s3_producer(monkeypatch, rows, sqlite_engine_factory)
    uploaded: list[str] = []
    storage.async_client.upload_file_from_path = AsyncMock(side_effect=lambda uri, **_: uploaded.append(Path(uri).name))
    listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: sorted(listdir(path)))
    await storage.save_downloaded_files("o_1", "wr_1")
    _write(run_dir / "b.pdf", STATEMENT, 1)
    method = "promote_configured_name" if failure == "promotion" else "find_download_artifact"
    original = getattr(database.artifacts, method)

    async def failing(*args: Any, **kwargs: Any) -> Any:
        if failure == "promotion" or kwargs.get("uri", "").endswith("/a.pdf"):
            raise asyncio.CancelledError() if failure == "cancellation" else RuntimeError("database unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(database.artifacts, method, failing)
    uploaded.clear()
    context = SkyvernContext(organization_id="o_1", run_id="wr_1", workflow_run_id="wr_1")
    await context.record_configured_download(str(named), "wr_1")
    skyvern_context.set(context)
    try:
        if failure == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await storage.save_downloaded_files("o_1", "wr_1")
        else:
            await storage.save_downloaded_files("o_1", "wr_1")
    finally:
        skyvern_context.reset()

    saved = await database.artifacts.list_artifacts_for_run_by_type("wr_1", "o_1", ArtifactType.DOWNLOAD)
    stamps = {Path(row.uri).name: row.configured_name_sha256 for row in saved}
    assert uploaded == ([] if failure == "cancellation" else ["b.pdf"]), "the unchanged a.pdf is never sent again"
    assert stamps == ({"a.pdf": None} if failure == "cancellation" else {"a.pdf": None, "b.pdf": None})


@pytest.mark.parametrize(
    ("copies", "discovery", "polls"),
    [(1, True, 1), (2, True, 1), (3, True, 2), (1, False, 1)],
    ids=["singleton", "equal_pair", "loop_of_three_polled_twice", "failed_discovery"],
)
@pytest.mark.asyncio
async def test_timeline_lists_and_reads_storage_only_for_a_competing_copy(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    copies: int,
    discovery: bool,
    polls: int,
) -> None:
    captures = [rows.add(f"a_{index}", f"invoice-{index}.pdf", INVOICE, index) for index in range(copies)]
    timeline = _Timeline(
        monkeypatch, {"wr_1": [_run_block(f"wrb_{i}", _task_block_output([c]), i) for i, c in enumerate(captures)]}
    )
    storage, _ = await _install_s3_producer(monkeypatch, rows, sqlite_engine_factory)
    calls: Counter[str] = Counter()
    watched = [(storage, n) for n in ("get_downloaded_files", "stat_artifact", "open_artifact_stream")]
    for owner, name in [*watched, (app.DATABASE.artifacts, "list_artifacts_for_run_by_type")]:
        original = getattr(owner, name)

        def spy(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)
    if not discovery:
        listing = AsyncMock(side_effect=RuntimeError("rows unavailable"))
        monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", listing)

    for _ in range(polls):
        blocks = await timeline.get()

    assert calls["get_downloaded_files"] == (polls if copies > 1 else 0)
    assert calls["list_artifacts_for_run_by_type"] == polls * (1 + (copies > 1)) * discovery
    assert calls["stat_artifact"] <= (copies if copies > 1 else 0) * polls
    assert calls["open_artifact_stream"] <= (copies if copies > 1 else 0) * polls
    assert {_listed(blocks[f"wrb_{i}"])[0] for i in range(copies)} == {"a_0"}


async def _stall(record: list[str], **_: Any) -> list[Any]:
    record.append("started")
    try:
        await asyncio.Event().wait()
    finally:
        record.append("settled")
    return []


@pytest.mark.parametrize("seam", ["outputs_listing", "admission_rows"])
@pytest.mark.asyncio
async def test_timeline_starts_no_outputs_listing_after_the_response_deadline_and_keeps_stored_entries(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, seam: str
) -> None:
    monkeypatch.setattr(unique_pdfs, "GET_DOWNLOADED_FILES_TIMEOUT", 0.3)
    owners, captures = _retried_run(rows)
    storage = _use_storage(monkeypatch, rows, captures)
    started: list[str] = []
    stalled = partial(_stall, started)
    rows_stall = seam == "admission_rows"
    monkeypatch.setattr(storage, "get_downloaded_files", AsyncMock(return_value=[]) if rows_stall else stalled)
    if rows_stall:
        monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", stalled)
    stored = {
        "wrb_first": _run_block("wrb_first", _task_block_output(captures[:2]), 0, attempt_number=1),
        "wrb_retry": _run_block("wrb_retry", _task_block_output(captures[2:]), 11, attempt_number=2),
    }
    timeline = _Timeline(monkeypatch, {"wr_1": list(stored.values())})
    _Attempts(monkeypatch, rows, owners)

    begun = asyncio.get_running_loop().time()
    # Generous hang guard only: the response must end on its own once the shared budget is spent.
    blocks = await asyncio.wait_for(timeline.get(), timeout=10)

    assert asyncio.get_running_loop().time() - begun < 5
    assert started == ["started", "settled"], "an admission or Outputs listing began after the response deadline"
    assert not rows_stall or cast(AsyncMock, storage.get_downloaded_files).await_count == 0
    for block_id, block in stored.items():
        assert isinstance(block.output, dict)
        assert _listed(blocks[block_id]) == block.output["downloaded_file_artifact_ids"]
    timeline.assert_rows_untouched()


class _GcsSdk:
    """SDK-edge fake standing in for ``google.cloud.storage.Client``, with no GCS or network behind it.

    Each listed page holds one blob; every page, metadata read and signature takes ``delays[kind]`` seconds, or fails
    once its request timeout ends sooner, and is recorded with its request options, start and end."""

    def __init__(self, keys: list[str], delays: dict[str, float]) -> None:
        self.keys, self.delays = keys, delays
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def work(self, kind: str, options: dict[str, Any]) -> None:
        call: dict[str, Any] = {"kind": kind, "options": options, "start": time.monotonic(), "end": None}
        with self.lock:
            self.calls.append(call)
        delay, timeout = self.delays.get(kind, 0.0), options.get("timeout")
        time.sleep(delay if timeout is None else min(delay, timeout[1]))
        call["end"] = time.monotonic()
        if timeout is not None and timeout[1] < delay:
            raise TimeoutError(f"fake {kind} request timed out")

    def list_blobs(self, bucket: str, prefix: str, page_token: str | None = None, **options: Any) -> _GcsPages:
        return _GcsPages(self, int(page_token or 0), options)

    def bucket(self, name: str) -> SimpleNamespace:
        def get_blob(path: str, **options: Any) -> SimpleNamespace:
            self.work("metadata", options)
            return SimpleNamespace(metadata={}, updated=None, size=1, generation=1)

        def blob(path: str) -> SimpleNamespace:
            def generate_signed_url(**options: Any) -> str:
                self.work("sign", options)
                return f"https://signed.example/{path}"

            return SimpleNamespace(generate_signed_url=generate_signed_url)

        return SimpleNamespace(get_blob=get_blob, blob=blob)


class _GcsPages:
    def __init__(self, sdk: _GcsSdk, start: int, options: dict[str, Any]) -> None:
        self.sdk, self.index, self.options = sdk, start, options
        self.next_page_token: str | None = None

    @property
    def pages(self) -> Iterator[list[SimpleNamespace]]:
        while self.index < len(self.sdk.keys):
            self.sdk.work("page", self.options)
            self.index += 1
            self.next_page_token = str(self.index) if self.index < len(self.sdk.keys) else None
            yield [SimpleNamespace(name=self.sdk.keys[self.index - 1])]

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for page in self.pages:
            yield from page


def _install_gcs_fallback(monkeypatch: pytest.MonkeyPatch, sdk: _GcsSdk) -> GcsStorage:
    """The real GCS storage and client over ``sdk``; with no artifact keyring the listing reads blobs."""
    storage = GcsStorage()
    monkeypatch.setattr(storage.async_client, "_client", sdk)
    # Deadline calls build their own client; these cases only time the SDK calls, so they get ``sdk`` too.
    monkeypatch.setattr(
        storage.async_client, "_client_within", lambda stop, until: (real_gcp._within(stop, until), (sdk, None))[1]
    )
    monkeypatch.setattr(app, "STORAGE", storage)
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None)
    monkeypatch.setattr(settings, "GCS_BUCKET_UPLOADS", "uploads")
    monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", None)
    return storage


@pytest.mark.parametrize(
    ("slow", "keys", "delay"),
    [
        pytest.param("page", 8, 0.3, id="paging"),
        pytest.param("metadata", 2, 0.8, id="metadata"),
        pytest.param("sign", 2, 0.8, id="signing"),
    ],
)
@pytest.mark.asyncio
async def test_timeline_stops_and_settles_gcs_blob_listing_work_at_the_response_deadline(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, slow: str, keys: int, delay: float
) -> None:
    monkeypatch.setattr(unique_pdfs, "GET_DOWNLOADED_FILES_TIMEOUT", 0.5)
    captures = [rows.add("a_1", "invoice.pdf", INVOICE, 0), rows.add("a_2", "invoice (1).pdf", INVOICE, 1)]
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_1", _task_block_output(captures), 0)]})
    sdk = _GcsSdk([f"downloads/o_1/wr_1/copy_{index}.pdf" for index in range(keys)], {slow: delay})
    _install_gcs_fallback(monkeypatch, sdk)

    # The 0.5 s response deadline falls inside the slow call, never at a boundary between calls.
    begun = time.monotonic()
    blocks = await asyncio.wait_for(timeline.get(), timeout=10)
    answered = time.monotonic()
    # Long enough for any GCS work the response left running to show itself.
    await asyncio.sleep(1.5)

    assert _listed(blocks["wrb_1"]) == ["a_1", "a_2"], "the block keeps its stored entries"
    assert any(call["kind"] == slow for call in sdk.calls)
    assert all(call["start"] < begun + 0.7 for call in sdk.calls), "a GCS request started after the deadline"
    outlived = [call["kind"] for call in sdk.calls if call["end"] is None or call["end"] > answered]
    assert not outlived, f"GCS {outlived} work ran on after the response"


@pytest.mark.asyncio
async def test_gcs_listing_outside_the_timeline_keeps_the_client_default_request_options(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    sdk = _GcsSdk(["downloads/o_1/wr_1/invoice.pdf"], {})
    storage = _install_gcs_fallback(monkeypatch, sdk)
    run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

    files, _ = await WorkflowService()._fetch_downloaded_files(run, None)
    await storage.async_client.get_object_info("gs://uploads/downloads/o_1/wr_1/invoice.pdf", bounded=True)

    assert [file_info.url for file_info in files or []] == ["https://signed.example/downloads/o_1/wr_1/invoice.pdf"]
    assert [call["kind"] for call in sdk.calls] == ["page", "metadata", "sign", "metadata"]
    request_options = [{k: v for k, v in call["options"].items() if k in ("timeout", "retry")} for call in sdk.calls]
    assert request_options == [{}, {}, {}, real_gcp._BOUNDED]


_SIGNER = "signer@synthetic.iam.gserviceaccount.com"
_SIGNED_URI = "gs://uploads/downloads/o_1/wr_1/invoice.pdf"


class _AuthTransport:
    """Synthetic stand-in for google-auth's HTTP transport, with no network behind it.

    It answers the Compute metadata and IAM signBlob endpoints, honours each request's timeout like the real transport,
    and records which transport sent every request, with its timeout, start and end."""

    def __init__(self, delays: dict[str, float] | None = None, statuses: dict[str, list[int]] | None = None) -> None:
        self.delays, self.statuses = delays or {}, statuses or {}
        self.calls: list[dict[str, Any]] = []

    def built_by(self, origin: str) -> type:
        transport = self

        class _Request:
            def __init__(self, *_: Any) -> None:
                pass

            def __call__(
                self,
                url: str,
                method: str = "GET",
                body: Any = None,
                headers: Any = None,
                timeout: Any = None,
                **_: Any,
            ) -> SimpleNamespace:
                return transport.send(origin, url, timeout)

        return _Request

    def send(self, origin: str, url: str, timeout: Any) -> SimpleNamespace:
        kind = "signblob" if ":signBlob" in url else "token" if "/token" in url else "metadata"
        call: dict[str, Any] = {
            "origin": origin,
            "kind": kind,
            "url": url,
            "timeout": timeout,
            "start": time.monotonic(),
            "end": None,
        }
        self.calls.append(call)
        delay = self.delays.get(kind, 0.0)
        limit = delay if timeout is None else min(delay, timeout[1] if isinstance(timeout, tuple) else timeout)
        time.sleep(limit)
        call["end"] = time.monotonic()
        if limit < delay:
            raise google_auth_exceptions.TransportError("synthetic request timed out")
        status = (self.statuses.get(kind) or [200]).pop(0)
        if kind == "signblob":
            body: Any = {"keyId": "k1", "signedBlob": base64.b64encode(b"synthetic-signature").decode()}
        elif kind == "token":
            body = {"access_token": "synthetic-token", "expires_in": 3600, "token_type": "Bearer"}
        elif "recursive=true" in url:
            body = {
                "email": _SIGNER,
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                "aliases": ["default"],
            }
        else:
            headers = {"metadata-flavor": "Google", "content-type": "text/plain"}
            text = b"googleapis.com" if "universe-domain" in url else b"synthetic-project"
            return SimpleNamespace(status=status, headers=headers, data=text)
        headers = {"metadata-flavor": "Google", "content-type": "application/json"}
        return SimpleNamespace(status=status, headers=headers, data=json.dumps(body).encode())


def _signing_client(monkeypatch: pytest.MonkeyPatch, transport: _AuthTransport) -> real_gcp.RealAsyncGcsStorageClient:
    """The real client, warm, signing as a Workload Identity service account over ``transport`` only."""
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    monkeypatch.setattr(real_gcp, "GoogleAuthRequest", transport.built_by("real_gcp"))
    monkeypatch.setattr(google_auth_requests, "Request", transport.built_by("library"))
    monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", _SIGNER)
    client = real_gcp.RealAsyncGcsStorageClient(project_id="synthetic", credentials=AnonymousCredentials())
    client._get_client()
    return client


@pytest.mark.parametrize(
    ("delays", "statuses", "budget"),
    [
        pytest.param({}, {}, 5.0, id="within_budget"),
        pytest.param({"token": 3.0}, {}, 0.5, id="token_outlasts_budget"),
        pytest.param({}, {"signblob": [503, 200]}, 5.0, id="retryable_signblob_status"),
    ],
)
@pytest.mark.asyncio
async def test_gcs_signing_under_a_deadline_sends_discovery_refresh_and_signblob_within_its_budget(
    monkeypatch: pytest.MonkeyPatch, delays: dict[str, float], statuses: dict[str, list[int]], budget: float
) -> None:
    transport = _AuthTransport(delays, statuses)
    client = _signing_client(monkeypatch, transport)
    begun = time.monotonic()

    signing = asyncio.ensure_future(
        client.create_signed_urls([_SIGNED_URI], deadline=asyncio.get_running_loop().time() + budget)
    )
    await asyncio.wait({signing}, timeout=20)
    ended = time.monotonic()

    assert transport.calls and {call["origin"] for call in transport.calls} == {"real_gcp"}, (
        "a request bypassed the budget"
    )
    for call in transport.calls:
        timeout = call["timeout"][1] if isinstance(call["timeout"], tuple) else call["timeout"]
        assert timeout is not None and call["start"] + timeout <= begun + budget + 0.05, call
        assert call["end"] is not None and call["end"] <= ended, call
    kinds = [call["kind"] for call in transport.calls]
    assert kinds.count("token") <= 1 and kinds.count("signblob") <= 1, "a request was retried"
    if delays or statuses:
        assert isinstance(signing.exception(), TimeoutError) and ended - begun < min(budget, 1.0) + 0.5
    else:
        assert "X-Goog-Signature=" + b"synthetic-signature".hex() in signing.result()[0]


def _cold_client(
    monkeypatch: pytest.MonkeyPatch, transport: _AuthTransport, data_statuses: list[int] | None = None
) -> real_gcp.RealAsyncGcsStorageClient:
    """The real client as GcsStorage builds it: no credentials and no Storage client yet.

    Discovery, refresh and universe lookups reach ``transport``. Storage's data requests run the real AuthorizedSession
    logic down to the HTTP adapter, which records them on the same timeline instead of sending them."""
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    monkeypatch.setattr(real_gcp, "GoogleAuthRequest", transport.built_by("real_gcp"))
    monkeypatch.setattr(google_auth_requests, "Request", transport.built_by("library"))
    # A bucket-named call otherwise queues the SDK's bucket-metadata lookup on a thread that outlives the test and
    # reaches the real Storage endpoint from whichever test runs next.
    monkeypatch.setenv("DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA", "true")
    statuses = list(data_statuses or [])

    def send(adapter: Any, request: Any, *_: Any, timeout: Any = None, **__: Any) -> requests.Response:
        call = {
            "origin": "data_plane",
            "kind": "list",
            "url": request.url,
            "timeout": timeout,
            "start": time.monotonic(),
        }
        transport.calls.append(call | {"authorization": request.headers.get("Authorization")})
        response = requests.Response()
        response.status_code = statuses.pop(0) if statuses else 200
        response.headers["content-type"] = "application/json"
        response._content = json.dumps({"items": [{"name": "downloads/o_1/wr_1/invoice.pdf"}]}).encode()
        response.request, response.url = request, request.url
        transport.calls[-1]["end"] = time.monotonic()
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return real_gcp.RealAsyncGcsStorageClient(project_id="synthetic")


@pytest.mark.parametrize(
    ("delays", "statuses", "budget"),
    [
        pytest.param({}, {}, 5.0, id="within_budget"),
        pytest.param({}, {}, -0.1, id="expired_before_the_worker_ran"),
        pytest.param({"token": 3.0}, {}, 0.5, id="token_outlasts_budget"),
        pytest.param({}, {"token": [503, 200]}, 5.0, id="retryable_token_status"),
    ],
)
@pytest.mark.asyncio
async def test_cold_gcs_listing_under_a_deadline_builds_its_client_within_the_budget(
    monkeypatch: pytest.MonkeyPatch, delays: dict[str, float], statuses: dict[str, list[int]], budget: float
) -> None:
    transport = _AuthTransport(delays, statuses)
    client = _cold_client(monkeypatch, transport)
    begun = time.monotonic()

    listing = asyncio.ensure_future(
        client.list_files("gs://uploads/downloads/o_1/wr_1", deadline=asyncio.get_running_loop().time() + budget)
    )
    await asyncio.wait({listing}, timeout=20)
    ended = time.monotonic()
    # Long enough for any background request the listing left behind to show itself.
    await asyncio.sleep(0.3)

    assert {call["origin"] for call in transport.calls} <= {"real_gcp", "data_plane"}, "a request bypassed the budget"
    for call in transport.calls:
        timeout = call["timeout"][1] if isinstance(call["timeout"], tuple) else call["timeout"]
        assert timeout is not None and call["start"] + timeout <= begun + max(budget, 0) + 0.05, call
        assert call["end"] is not None and call["end"] <= ended, call
    assert [call["kind"] for call in transport.calls].count("token") <= 1, "a request was retried"
    assert client._client is None, "the shared client kept a client built for one deadline"
    if budget <= 0:
        assert isinstance(listing.exception(), TimeoutError) and not transport.calls
    elif delays or statuses:
        assert isinstance(listing.exception(), TimeoutError) and ended - begun < min(budget, 1.0) + 0.5
        assert not any(call["origin"] == "data_plane" for call in transport.calls)
    else:
        assert listing.result() == ["downloads/o_1/wr_1/invoice.pdf"]
        data = [call["authorization"] for call in transport.calls if call["origin"] == "data_plane"]
        assert data == ["Bearer synthetic-token"]


@pytest.mark.parametrize("operation", ["signing", "cold_listing"])
@pytest.mark.asyncio
async def test_cancelled_gcs_work_starts_no_later_request_and_settles_its_inflight_one(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    signing = operation == "signing"
    kind = "signblob" if signing else "token"
    transport = _AuthTransport({kind: 3.0})
    client = (_signing_client if signing else _cold_client)(monkeypatch, transport)
    begun = time.monotonic()
    deadline = asyncio.get_running_loop().time() + 1.0
    work = asyncio.ensure_future(
        client.create_signed_urls([_SIGNED_URI], deadline=deadline)
        if signing
        else client.list_files("gs://uploads/downloads/o_1/wr_1", deadline=deadline)
    )
    while not any(call["kind"] == kind for call in transport.calls) and time.monotonic() - begun < 5:
        await asyncio.sleep(0.01)

    work.cancel()
    cancelled_at = time.monotonic()
    await asyncio.wait({work}, timeout=20)
    settled = time.monotonic()

    assert work.cancelled()
    assert {call["origin"] for call in transport.calls} == {"real_gcp"}, "a request bypassed the budget"
    assert all(call["start"] <= cancelled_at for call in transport.calls), "a request started after cancellation"
    assert all(call["end"] is not None and call["end"] <= settled for call in transport.calls), "a request outlived it"
    assert settled - begun < 1.5, "the in-flight request outran the remaining budget"
    assert signing or client._client is None


@pytest.mark.parametrize(
    "data_statuses", [pytest.param([], id="expired_token"), pytest.param([401], id="unauthorized")]
)
@pytest.mark.asyncio
async def test_warm_gcs_listing_under_a_deadline_leaves_the_shared_clients_auth_and_cache_alone(
    monkeypatch: pytest.MonkeyPatch, data_statuses: list[int]
) -> None:
    transport = _AuthTransport()
    client = _cold_client(monkeypatch, transport, data_statuses)
    shared = real_gcp.storage.Client(project="synthetic", credentials=compute_engine.Credentials())
    client._client = shared
    before = len(transport.calls)

    listing = asyncio.ensure_future(
        client.list_files("gs://uploads/downloads/o_1/wr_1", deadline=asyncio.get_running_loop().time() + 5.0)
    )
    await asyncio.wait({listing}, timeout=20)
    await asyncio.sleep(0.3)

    calls = transport.calls[before:]
    assert {call["origin"] for call in calls} <= {"real_gcp", "data_plane"}, "a request bypassed the budget"
    assert all("/o?" in call["url"] for call in calls if call["origin"] == "data_plane"), "a bucket fetch ran"
    assert client._client is shared and shared._credentials.token is None, "the shared client was used or refreshed"
    if data_statuses:
        assert listing.exception() is not None and [c["origin"] for c in calls].count("data_plane") == 1
    else:
        assert listing.result() == ["downloads/o_1/wr_1/invoice.pdf"]


@pytest.mark.asyncio
async def test_cold_gcs_signing_under_a_deadline_signs_locally_with_a_discovered_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    account = {
        "type": "service_account",
        "project_id": "synthetic",
        "private_key_id": "k1",
        "private_key": pem.decode(),
        "client_email": _SIGNER,
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    (tmp_path / "account.json").write_text(json.dumps(account))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "account.json"))
    monkeypatch.setattr(settings, "GCS_SIGNER_SA_EMAIL", None)
    transport = _AuthTransport()
    client = _cold_client(monkeypatch, transport)

    urls = await client.create_signed_urls([_SIGNED_URI], deadline=asyncio.get_running_loop().time() + 5.0)

    assert urls and "X-Goog-Signature=" in urls[0] and quote(_SIGNER, safe="") in urls[0]
    assert {call["origin"] for call in transport.calls} == {"real_gcp"}, "a request bypassed the budget"
    assert client._client is None


@pytest.mark.asyncio
async def test_ordinary_gcs_listing_after_a_deadline_one_still_builds_the_shared_client_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _AuthTransport()
    client = _cold_client(monkeypatch, transport)
    deadline = asyncio.get_running_loop().time() + 5.0
    await client.list_files("gs://uploads/downloads/o_1/wr_1", deadline=deadline)
    before = len(transport.calls)

    names = await client.list_files("gs://uploads/downloads/o_1/wr_1")

    ordinary = transport.calls[before:]
    assert names == ["downloads/o_1/wr_1/invoice.pdf"] and client._client is not None
    assert {call["origin"] for call in ordinary if call["origin"] != "data_plane"} == {"library"}
    assert [call["timeout"] for call in ordinary if call["origin"] == "data_plane" and "/o?" in call["url"]] == [60]


@pytest.mark.asyncio
async def test_gcs_signing_without_a_deadline_keeps_the_sdks_own_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _AuthTransport()
    client = _signing_client(monkeypatch, transport)

    urls = await client.create_signed_urls([_SIGNED_URI])

    assert urls and "X-Goog-Signature=" + b"synthetic-signature".hex() in urls[0]
    signblob = [call for call in transport.calls if call["kind"] == "signblob"]
    assert [(call["origin"], call["timeout"]) for call in signblob] == [("library", None)]
    assert signblob[0]["url"].endswith(":signBlob?alt=json"), "the default path signs through Storage's own IAM call"
    assert {call["origin"] for call in transport.calls if call["kind"] == "token"} == {"real_gcp"}


@pytest.mark.asyncio
async def test_local_download_listing_hashes_off_the_event_loop_and_a_cancelled_one_stops_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = asyncio.get_running_loop()
    # One worker thread, so a hash still running after the listing ended would hold up the next job.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "invoice.pdf").write_bytes(INVOICE)
    monkeypatch.setattr("skyvern.forge.sdk.artifact.storage.local.get_download_dir", lambda run_id: str(download_dir))
    monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", AsyncMock(return_value=[]))
    storage = LocalStorage(str(tmp_path / "artifacts"))

    files = await storage.get_downloaded_files(organization_id="o_1", run_id="wr_1")
    assert [file_info.checksum for file_info in files] == [_sha(INVOICE)]

    with open(download_dir / "export.bin", "wb") as export:
        export.truncate(4 << 30)  # sparse: gigabytes to hash without the disk holding them
    listing = asyncio.ensure_future(storage.get_downloaded_files(organization_id="o_1", run_id="wr_1"))
    await asyncio.sleep(0.3)
    assert not listing.done(), "hashing held the event loop"
    cancelled_at = loop.time()
    listing.cancel()
    await asyncio.wait({listing}, timeout=60)

    assert listing.cancelled()
    assert loop.time() - cancelled_at < 1, "the cancelled listing kept hashing to the end of the file"
    await asyncio.wait_for(loop.run_in_executor(None, lambda: None), timeout=1)


@pytest.mark.parametrize(
    "failure", [None, "row_lookup", "attempt_read", "attempt_scope", "attempt_scope_finds_nothing", "content_read"]
)
@pytest.mark.asyncio
async def test_run_timeline_keeps_every_copy_when_a_listing_read_fails(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, failure: str | None
) -> None:
    owners, captures = _retried_run(rows)
    _use_storage(monkeypatch, rows, captures[2:])
    retried = _run_block("wrb_retry", _task_block_output(captures[2:]), 11, attempt_number=2)
    timeline = _Timeline(monkeypatch, {"wr_1": [retried]})
    attempts = _Attempts(monkeypatch, rows, owners)
    failing = AsyncMock(side_effect=RuntimeError("read failed"))
    if failure == "row_lookup":
        monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", failing)
    elif failure == "attempt_read":
        monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", failing)
    elif failure == "attempt_scope":
        monkeypatch.setattr(app.DATABASE.workflow_runs, "get_artifacts_for_attempt", failing)
    elif failure == "attempt_scope_finds_nothing":
        attempts.owners.clear()
    elif failure == "content_read":
        rows.store.replaced.update(rows.uri(artifact_id) for artifact_id in ("new_1", "new_2"))

    blocks = await timeline.get()

    assert _listed(blocks["wrb_retry"]) == (["new_1"] if failure is None else ["new_1", "new_2"])
    timeline.signing.assert_not_awaited()
    timeline.assert_rows_untouched()


@pytest.mark.parametrize(
    "foreign", [None, "legacy_attempt", "organization", "run", "attempt", "changed_content", "not_a_download"]
)
@pytest.mark.asyncio
async def test_run_timeline_merges_only_copies_the_block_proves_it_owns(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, foreign: str | None
) -> None:
    # "run" is another run's copy stored under the same download key, as a child run's captures are.
    elsewhere = {"organization": {"organization_id": "o_2"}, "run": {"workflow_run_id": "wr_other"}}.get(
        foreign or "", {}
    )
    captures = [
        rows.add("new_1", "invoice.pdf", INVOICE, 11),
        rows.add("new_2", "invoice (1).pdf", INVOICE, 12, **elsewhere),
    ]
    if foreign == "changed_content":
        rows.artifacts[-1] = rows.artifacts[-1].model_copy(
            update={"checksum": _sha(RECEIPT), "file_size": len(RECEIPT)}
        )
        rows.objects[rows.uri("new_2")] = RECEIPT
    if foreign == "not_a_download":
        rows.artifacts[-1] = rows.artifacts[-1].model_copy(update={"artifact_type": ArtifactType.SCREENSHOT_FINAL})
    _use_storage(monkeypatch, rows, captures)
    attempt_number = None if foreign == "legacy_attempt" else 2
    block = _run_block("wrb_task", _task_block_output(captures), 11, attempt_number=attempt_number)
    timeline = _Timeline(monkeypatch, {"wr_1": [block]})
    owner = attempt_number or 1
    _Attempts(monkeypatch, rows, {"new_1": owner, "new_2": owner - 1 if foreign == "attempt" else owner})

    blocks = await timeline.get()

    assert _listed(blocks["wrb_task"]) == (["new_1"] if foreign in (None, "legacy_attempt") else ["new_1", "new_2"])
    if foreign not in (None, "legacy_attempt"):
        assert rows.reads == [], "an entry is proven the block's own before any content is read"


async def _attempt_query_database(sqlite_engine_factory: SqliteEngineFactory, store: _Rows) -> AgentDB:
    """A database holding ``store``'s rows, so attempt membership comes from the repositories' own queries."""
    engine = sqlite_engine_factory(build_engine=_build_engine)
    database = AgentDB(str(engine.url), db_engine=engine)
    await database.organizations.create_organization("Org", organization_id="o_1")
    async with database.Session() as session:
        for row in store.artifacts:
            session.add(
                ArtifactModel(
                    artifact_id=row.artifact_id,
                    organization_id=row.organization_id,
                    run_id=row.run_id,
                    workflow_run_id=row.workflow_run_id,
                    artifact_type=row.artifact_type,
                    uri=row.uri,
                    checksum=row.checksum,
                    file_size=row.file_size,
                    created_at=row.created_at.replace(tzinfo=None),
                    modified_at=row.modified_at.replace(tzinfo=None),
                    configured_name_sha256=row.configured_name_sha256,
                )
            )
        await session.commit()
    return database


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        pytest.param("touched_earlier_copy", ["old_1", "new_1"], id="earlier_attempt_copy_touched_during_retry"),
        pytest.param("retry_without_attempt_rows", ["new_1", "new_2"], id="retry_without_attempt_rows"),
        pytest.param("retry_copies", ["new_1"], id="proven_retry_copies"),
        pytest.param("legacy_without_attempt_rows", ["new_1"], id="null_legacy_attempt"),
        pytest.param("singleton_beside_touched_earlier_copy", ["new_1"], id="retry_capture_never_shown_as_earlier"),
    ],
)
@pytest.mark.asyncio
async def test_run_timeline_merges_only_copies_the_strict_attempt_query_attributes(
    monkeypatch: pytest.MonkeyPatch,
    rows: _Rows,
    sqlite_engine_factory: SqliteEngineFactory,
    case: str,
    expected: list[str],
) -> None:
    # Attempt 2 starts at minute 10; the earlier attempt's copy is refreshed at minute 12, which the live
    # download listing admits as attempt 2's.
    old = rows.add("old_1", "invoice.pdf", INVOICE, 1, modified_minutes=12)
    retry = [rows.add("new_1", "invoice (1).pdf", INVOICE, 11), rows.add("new_2", "invoice (2).pdf", INVOICE, 12)]
    listed = {"touched_earlier_copy": [old, retry[0]], "singleton_beside_touched_earlier_copy": [retry[0]]}.get(
        case, retry
    )
    _use_storage(monkeypatch, rows, [old, retry[0]] if case.endswith("touched_earlier_copy") else retry)
    database = await _attempt_query_database(sqlite_engine_factory, rows)
    attempt_number = None if case.startswith("legacy") else 2
    block = _run_block("wrb_retry", _task_block_output(listed), 11, attempt_number=attempt_number)
    timeline = _Timeline(monkeypatch, {"wr_1": [block]})
    recorded = [] if case.endswith("without_attempt_rows") else [_attempt(1, 0), _attempt(2, 10)]
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=recorded))
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_download_artifacts_for_attempt",
        database.artifacts.list_download_artifacts_for_attempt,
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "get_artifacts_for_attempt", database.workflow_runs.get_artifacts_for_attempt
    )

    blocks = await timeline.get()

    assert _listed(blocks["wrb_retry"]) == expected


@pytest.mark.asyncio
async def test_task_v2_run_timeline_lists_child_downloads_by_the_child_runs_own_attempt_in_one_comparison(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    owners, captures = _retried_run(rows)
    # The child run's downloads are stored under the parent's download key and attributed to the child run.
    child = [
        rows.add(f"ch_{n}", f"child-{n}.pdf", data, 20 + n, workflow_run_id="wr_child")
        for n, data in enumerate([INVOICE, INVOICE, STATEMENT, RECEIPT], 1)
    ]
    receipt = rows.add("r_1", "receipt.pdf", RECEIPT, 13)
    gone = receipt.model_copy(update={"artifact_id": "gone", "url": "https://files.example/gone"})
    storage = _use_storage(monkeypatch, rows, [])
    listings = {"wr_1": [*captures[2:], receipt, *child]}
    monkeypatch.setattr(
        storage, "get_downloaded_files", AsyncMock(side_effect=lambda **kw: listings.get(kw["run_id"], []))
    )
    # Rows without a run attribution under the parent's key read as the parent's own, yet another run reported them.
    embedded = _task_block_output(captures[2:])
    parent_rows = [
        _run_block("wrb_parent", _task_block_output(captures[2:]), 11, attempt_number=2),
        _run_block("wrb_parent_gone", _task_block_output([receipt, gone]), 12, attempt_number=2),
        _run_block("wrb_trigger", embedded, 13, attempt_number=2, block_type=BlockType.WORKFLOW_TRIGGER).model_copy(
            update={"block_workflow_run_id": "wr_triggered"}
        ),
        _run_block("wrb_task_v2", embedded, 14, attempt_number=2, block_type=BlockType.TaskV2).model_copy(
            update={"block_workflow_run_id": "wr_child"}
        ),
    ]
    child_rows = [
        _run_block("wrb_child", _task_block_output(child[:3]), 21, "wr_child", attempt_number=None),
        _run_block("wrb_child_gone", _task_block_output([child[3], gone]), 22, "wr_child", attempt_number=None),
    ]
    timeline = _Timeline(monkeypatch, {"wr_1": parent_rows, "wr_child": child_rows})
    _Attempts(monkeypatch, rows, owners | {"r_1": 2})
    read_attempts = AsyncMock(side_effect=lambda run_id: {"wr_1": [_attempt(1, 0), _attempt(2, 10)]}.get(run_id, []))
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", read_attempts)
    task_v2 = SimpleNamespace(observer_cruise_id="oc_child")
    monkeypatch.setattr(
        app.DATABASE.observer,
        "get_task_v2_by_workflow_run_id",
        AsyncMock(side_effect=lambda workflow_run_id, **_: task_v2 if workflow_run_id == "wr_child" else None),
    )
    monkeypatch.setattr(agent_protocol.task_v2_service, "get_thought_timelines", AsyncMock(return_value=[]))
    lookup = AsyncMock(side_effect=rows.get_artifacts_by_ids)
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookup)
    comparisons: list[unique_pdfs.ResponseComparison] = []
    construct = unique_pdfs.ResponseComparison.__init__

    def observe_construction(comparison: unique_pdfs.ResponseComparison) -> None:
        construct(comparison)
        comparisons.append(comparison)

    monkeypatch.setattr(unique_pdfs.ResponseComparison, "__init__", observe_construction)

    blocks = await timeline.get()

    assert _listed(blocks["wrb_parent"]) == ["new_1"]
    assert _listed(blocks["wrb_child"]) == ["ch_1", "ch_2", "ch_3"], "the child run's Outputs list none of them"
    assert _listed(blocks["wrb_parent_gone"]) == ["r_1", "gone"]
    assert _listed(blocks["wrb_child_gone"]) == ["ch_4", "gone"]
    assert blocks["wrb_trigger"]["output"] == embedded, "a payload embedded from another run is left as stored"
    replaced = next(item for item in timeline.items if item["task_v2_blocks"])
    assert replaced["attempt"] == 2 and [block["output"] for block in replaced["task_v2_blocks"]] == [embedded]
    assert len(comparisons) == 1, "one comparison and deadline for the whole response"
    assert sorted(call.args[0] for call in read_attempts.await_args_list) == ["wr_1", "wr_child"]
    assert len([call for call in lookup.await_args_list if "gone" in call.args[0]]) == 1
    timeline.assert_rows_untouched()


@pytest.mark.parametrize(
    ("case", "malformed"),
    [
        *((case, None) for case in ["denied", "no_downloads", "screenshots_only", "authored_list", "capped"]),
        pytest.param("malformed", {"checksum": {"truncated": True}}, id="malformed_checksum_dict"),
        pytest.param("malformed", {"file_size": []}, id="malformed_size_list"),
        pytest.param("malformed", {"file_size": float(len(STATEMENT))}, id="malformed_size_float"),
        pytest.param(
            "malformed",
            {"filename": {"truncated": True, "reason": "exceeded_max_run_response_value_size"}},
            id="malformed_marker",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_timeline_reads_nothing_more_for_a_block_without_a_mergeable_download_list(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, case: str, malformed: dict[str, Any] | None
) -> None:
    captures = _code_block_rows(rows)
    output: Any = {
        "no_downloads": {"extracted": {"total": 3}},
        "screenshots_only": _task_block_output([]),
        "authored_list": {
            "files": [captures[0].model_dump(mode="json")] * 2,
            "downloaded_file_urls": [captures[0].url] * 2,
        },
    }.get(case, _task_block_output(captures))
    if case == "capped":
        output["downloaded_files"][1] = {"truncated": True, "reason": "exceeded_max_run_response_value_size"}
    if malformed:
        output["downloaded_files"][2].update(malformed)
    _use_storage(monkeypatch, rows, [])
    lookup = AsyncMock(side_effect=rows.get_artifacts_by_ids)
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookup)
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_task", output, 0)]})
    if case == "denied":
        denied = AsyncMock(return_value=BrowserSessionArtifactAccess(False))
        monkeypatch.setattr("skyvern.forge.sdk.workflow.service.browser_session_artifact_access", denied)
    constructed = MagicMock(side_effect=unique_pdfs.ResponseComparison.__init__)
    monkeypatch.setattr(unique_pdfs.ResponseComparison, "__init__", lambda comparison: constructed(comparison))

    blocks = await timeline.get()

    lookup.assert_not_awaited()
    app.DATABASE.workflow_run_attempts.get_attempts.assert_not_awaited()
    timeline.signing.assert_not_awaited()
    assert rows.reads == [] and (malformed is not None or not constructed.called)
    if case != "denied":
        assert blocks["wrb_task"]["output"] == output
    timeline.assert_rows_untouched()


@pytest.mark.asyncio
async def test_capped_run_timeline_lists_each_pdf_once_then_still_caps_the_output(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    captures = _code_block_rows(rows)
    _use_storage(monkeypatch, rows, captures)
    notes = {"notes": "n" * (RUN_RESPONSE_MAX_VALUE_BYTES + 1)}
    oversized = _task_block_output(captures) | notes
    legacy = _run_block("wrb_legacy", _task_block_output(captures), 1).model_copy(
        update={"downloaded_file_count": None}
    )
    plain = _run_block("wrb_plain", notes, 2)
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_oversized", oversized, 0), legacy, plain]})

    # The service on its own serves one response too, as the evaluation route uses it.
    served = await WorkflowService().get_workflow_run_timeline("wr_1", "o_1", cap_output_values=True)

    blocks = {item.block.workflow_run_block_id: item.block for item in served if item.block}
    capped = blocks["wrb_oversized"].output
    assert isinstance(capped, dict) and capped["downloaded_file_artifact_ids"] == ["a_1", "b_1"]
    assert [entry["artifact_id"] for entry in capped["downloaded_files"]] == ["a_1", "b_1"]
    assert capped["notes"] != oversized["notes"] and len(json.dumps(capped)) <= RUN_RESPONSE_MAX_VALUE_BYTES
    assert blocks["wrb_oversized"].downloaded_file_count == 2
    assert isinstance(legacy_output := blocks["wrb_legacy"].output, dict)
    assert legacy_output["downloaded_file_artifact_ids"] == ["a_1", "b_1"]
    assert blocks["wrb_legacy"].downloaded_file_count is None, "a count the row never stored is not inferred"
    assert blocks["wrb_plain"].output != notes
    assert timeline.prefer_capped is True
    timeline.assert_rows_untouched()


@pytest.mark.asyncio
async def test_capped_run_timeline_bounds_a_legacy_download_group_before_reading_its_entries(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows
) -> None:
    padding = "x" * (RUN_RESPONSE_MAX_VALUE_BYTES // 2)
    captures = [
        rows.add(artifact_id, f"{artifact_id}-{padding}.pdf", data, minutes)
        for artifact_id, data, minutes in [("a_1", INVOICE, 0), ("a_2", INVOICE, 1), ("b_1", STATEMENT, 2)]
    ]
    _use_storage(monkeypatch, rows, [])
    lookup = AsyncMock(side_effect=rows.get_artifacts_by_ids)
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", lookup)
    # A row written before capped copies existed: the capped read hands back its raw output.
    timeline = _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_legacy", _task_block_output(captures), 0)]})
    projected_sizes: list[int] = []
    project = WorkflowService._project_block_downloads

    async def observe(service: WorkflowService, block: WorkflowRunBlock, *args: Any) -> WorkflowRunBlock:
        projected_sizes.append(len(json.dumps(block.output)))
        return await project(service, block, *args)

    monkeypatch.setattr(WorkflowService, "_project_block_downloads", observe)

    blocks = await timeline.get(SKYVERN_UI_USER_AGENT)

    assert projected_sizes and max(projected_sizes) <= RUN_RESPONSE_MAX_VALUE_BYTES
    lookup.assert_not_awaited()
    assert rows.reads == []
    assert len(json.dumps(blocks["wrb_legacy"]["output"])) <= RUN_RESPONSE_MAX_VALUE_BYTES
    assert timeline.prefer_capped is True
    timeline.assert_rows_untouched()


@pytest.mark.parametrize("seam", ["comparison_streams", "admission_rows"])
@pytest.mark.asyncio
async def test_cancelling_a_run_timeline_settles_the_work_it_started(
    monkeypatch: pytest.MonkeyPatch, rows: _Rows, seam: str
) -> None:
    captures = [rows.add("a_1", "invoice.pdf", INVOICE, 0), rows.add("a_2", "invoice (1).pdf", INVOICE, 1)]
    rows.store.stalled.update({rows.uri("a_1"), rows.uri("a_2")})
    storage = _use_storage(monkeypatch, rows, captures)
    admission: list[str] = []
    if seam == "admission_rows":
        monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", partial(_stall, admission))
        monkeypatch.setattr(storage, "get_downloaded_files", AsyncMock(return_value=captures))
    _Timeline(monkeypatch, {"wr_1": [_run_block("wrb_task", _task_block_output(captures), 0)]})
    response = asyncio.ensure_future(WorkflowService().get_workflow_run_timeline("wr_1", "o_1"))
    for _ in range(200):
        if len(rows.reads) == 2 or admission:
            break
        await asyncio.sleep(0)

    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response
    for _ in range(20):
        await asyncio.sleep(0)
    if seam == "admission_rows":
        assert admission == ["started", "settled"]
        assert cast(AsyncMock, storage.get_downloaded_files).await_count == 0 and rows.reads == []
    else:
        assert sorted(rows.store.closed) == ["a_1", "a_2"]


async def _upload_to_s3(path: str, parameters: dict[str, str] | None = None) -> list[str]:
    client = SimpleNamespace(upload_file_from_path=AsyncMock())
    workflow_run_context = MagicMock()
    workflow_run_context.has_parameter.side_effect = lambda key: key in (parameters or {})
    workflow_run_context.get_value.side_effect = lambda key: (parameters or {})[key]
    with (
        patch.object(UploadToS3Block, "get_workflow_run_context", return_value=workflow_run_context),
        patch.object(UploadToS3Block, "format_potential_template_parameters", return_value=None),
        patch.object(UploadToS3Block, "record_output_parameter_value", AsyncMock()),
        patch.object(UploadToS3Block, "build_block_result", AsyncMock()),
        patch.object(UploadToS3Block, "get_async_aws_client", return_value=client),
        patch("skyvern.forge.sdk.workflow.models.block.get_download_retry_started_at", AsyncMock(return_value=None)),
        patch("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", return_value=None),
    ):
        await UploadToS3Block.model_construct(label="upload", path=path).execute(
            workflow_run_id="wr_1", workflow_run_block_id="", organization_id="o_1"
        )
    return sorted(Path(call.kwargs["file_path"]).name for call in client.upload_file_from_path.await_args_list)


@pytest.mark.parametrize("chosen", ["download_directory_key", "parameter_naming_the_directory", "directory_path"])
@pytest.mark.asyncio
async def test_upload_of_the_run_download_directory_sends_each_pdf_content_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chosen: str
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    run_dir = Path(get_download_dir("wr_1"))
    _write(run_dir / "invoice.pdf", INVOICE, 0)
    _write(run_dir / "invoice (1).pdf", INVOICE, 1)
    _write(run_dir / "statement.pdf", STATEMENT, 2)
    path, parameters = {
        "download_directory_key": (settings.WORKFLOW_DOWNLOAD_DIRECTORY_PARAMETER_KEY, None),
        "parameter_naming_the_directory": ("downloads", {"downloads": str(run_dir)}),
        "directory_path": (str(run_dir), None),
    }[chosen]

    assert await _upload_to_s3(path, parameters) == ["invoice.pdf", "statement.pdf"]
    assert sorted(p.name for p in run_dir.iterdir()) == ["invoice (1).pdf", "invoice.pdf", "statement.pdf"]


@pytest.mark.asyncio
async def test_upload_of_one_chosen_copy_or_another_directory_sends_every_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    run_dir = Path(get_download_dir("wr_1"))
    other_dir = run_dir / "kept"
    other_dir.mkdir(parents=True)
    copies = [_write(run_dir / "invoice.pdf", INVOICE, 0), _write(run_dir / "invoice (1).pdf", INVOICE, 1)]
    for copy_ in copies:
        _write(other_dir / copy_.name, INVOICE, 0)

    assert await _upload_to_s3(str(copies[1])) == ["invoice (1).pdf"]
    assert await _upload_to_s3(str(other_dir)) == ["invoice (1).pdf", "invoice.pdf"]


@contextmanager
def _receipts(*paths: Path, run_id: str | None = "wr_1") -> Iterator[None]:
    """Run wr_1's context, current while the block runs, holding configured-name receipts for ``paths``."""
    context = SkyvernContext(organization_id="o_1", run_id="wr_1", workflow_run_id="wr_1")
    for path in paths if run_id else ():
        asyncio.run(context.record_configured_download(str(path), run_id))
    skyvern_context.set(context)
    try:
        yield
    finally:
        skyvern_context.reset()


def _write(path: Path, data: bytes, seconds: int) -> Path:
    path.write_bytes(data)
    os.utime(path, (T0.timestamp() + seconds,) * 2)
    return path


def test_upload_scan_sends_each_pdf_content_once_and_leaves_the_files(tmp_path: Path) -> None:
    late = _write(tmp_path / "a-invoice-late.pdf", INVOICE, 9)
    first = _write(tmp_path / "invoice.pdf", INVOICE, 1)
    statement = _write(tmp_path / "statement.pdf", STATEMENT, 2)
    kept_pairs = [
        _write(tmp_path / f"{name}{i}.pdf", data, 3 + i)
        for name, data in [("cut", CUT_OFF), ("header", HEADER_ONLY), ("report", NOT_PDF), ("empty", b"")]
        for i in range(2)
    ]
    in_progress = _write(tmp_path / "invoice.pdf.crdownload", INVOICE, 0)
    text = [_write(tmp_path / f"notes{i}.txt", b"same notes", 6 + i) for i in range(2)]
    link = tmp_path / "invoice-link.pdf"
    link.symlink_to(first)

    files = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
        download_files_path=str(tmp_path), max_file_count=300
    )

    assert sorted(files) == sorted(str(p) for p in [first, statement, *kept_pairs, in_progress, *text, link])
    assert late.read_bytes() == INVOICE


@pytest.mark.parametrize(
    ("custom", "receipt_run", "rewrite", "kept"),
    [
        pytest.param("stmt.custom", "wr_1", False, ["stmt.custom"], id="named_bytes"),
        pytest.param("stmt.custom", "wr_1", True, ["invoice.pdf"], id="rewritten_after_naming"),
        pytest.param("stmt.custom", "wr_x", False, ["invoice.pdf"], id="receipt_of_another_run"),
        pytest.param("invoice.custom", "wr_1", False, ["invoice.pdf"], id="configured_tie_this_run"),
        pytest.param("invoice.custom", None, False, ["invoice.pdf"], id="unknown_pdf_over_earlier_custom"),
        pytest.param("friendly.pdf", None, False, ["friendly.pdf"], id="unknown_all_pdf_earliest"),
    ],
)
def test_upload_scan_keeps_the_configured_copy_then_a_pdf_then_the_earliest_of_equal_files(
    tmp_path: Path, custom: str, receipt_run: str | None, rewrite: bool, kept: list[str]
) -> None:
    tie = custom != "stmt.custom"
    paths = {
        "invoice.pdf": _write(tmp_path / "invoice.pdf", INVOICE, 5 if tie else 1),
        custom: _write(tmp_path / custom, STATEMENT if rewrite else INVOICE, 1 if tie else 5),
    }
    with _receipts(*(paths.values() if tie else [paths[custom]]), run_id=receipt_run):
        if rewrite:
            _write(paths[custom], INVOICE, 5)
        listed = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
            download_files_path=str(tmp_path), max_file_count=300
        )

    assert sorted(listed) == sorted(str(paths[name]) for name in kept)
    assert all(path.read_bytes() == INVOICE for path in paths.values())


@pytest.mark.asyncio
async def test_a_local_run_file_that_changed_after_listing_or_lies_outside_the_run_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    run_dir = Path(get_download_dir("wr_1"))
    paths = [
        _write(run_dir / "invoice.pdf", INVOICE, 0),
        _write(run_dir / "invoice (1).pdf", INVOICE, 1),
        _write(tmp_path / "invoice-elsewhere.pdf", INVOICE, 2),
    ]
    listing = [
        FileInfo(url=f"file://{p}", checksum=_sha(INVOICE), filename=p.name, file_size=len(INVOICE)) for p in paths
    ]
    paths[1].write_bytes(INVOICE + b"% appended after listing\n")
    monkeypatch.setattr(app, "STORAGE", StreamedDownloadObjects({}, listing))

    files, _ = await WorkflowService()._fetch_downloaded_files(
        SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1"), None
    )

    assert files == listing


# The page reproduces a common site PDF helper: it opens the PDF Blob in a new tab, and if that tab has closed a second
# later (a browser that downloads PDFs closes it) it saves the same Blob again through a download anchor.
_PAGE = """<!doctype html><html><body>
<button id="invoice">invoice</button><button id="statement">statement</button><button id="again">again</button>
<button id="not-pdf">not pdf</button><button id="save-fails">save fails</button><a id="broken" href="/broken.pdf">x</a>
<script>
const blob = (b64) => new Blob([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))], {type: "application/pdf"});
const saveAs = (data, name) => {
  if (window.failSave) throw new Error("save failed");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(data);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
};
const openFileInNewPage = (data, name) => {
  const tab = window.open(URL.createObjectURL(data), "_blank");
  setTimeout(() => { if (!tab || tab.closed) { try { saveAs(data, name); } catch (e) {} } }, 1000);
};
const invoice = blob(%(invoice)s), statement = blob(%(statement)s), receipt = blob(%(receipt)s);
const notPdf = blob(%(not_pdf)s);
const on = (id, handler) => document.getElementById(id).addEventListener("click", handler);
on("invoice", () => openFileInNewPage(invoice, "invoice.pdf"));
on("again", () => openFileInNewPage(invoice, "invoice.pdf"));
on("statement", () => saveAs(statement, "statement.pdf"));
on("not-pdf", () => { saveAs(notPdf, "report.pdf"); saveAs(notPdf, "report.pdf"); });
on("save-fails", () => { window.failSave = true; openFileInNewPage(receipt, "receipt.pdf"); });
</script></body></html>"""


def _page() -> bytes:
    sources = {"invoice": INVOICE, "statement": STATEMENT, "receipt": RECEIPT, "not_pdf": NOT_PDF}
    return (_PAGE % {name: json.dumps(base64.b64encode(data).decode()) for name, data in sources.items()}).encode()


class _Site(ThreadingHTTPServer):
    def __init__(self) -> None:
        page = _page()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.startswith("/broken.pdf"):
                    # Announces a whole PDF, sends half, then drops the connection: the browser fails the download.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition", 'attachment; filename="broken.pdf"')
                    self.send_header("Content-Length", str(len(INVOICE)))
                    self.end_headers()
                    self.wfile.write(CUT_OFF)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *_args: Any) -> None:
                return

        super().__init__(("127.0.0.1", 0), Handler)


@pytest.fixture
def site() -> Iterator[str]:
    server = _Site()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _captures(download_dir: Path) -> list[Path]:
    return sorted(p for p in download_dir.iterdir() if p.is_file())


async def _wait_for_captures(download_dir: Path, count: int) -> list[Path]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        files = _captures(download_dir)
        if len(files) >= count and all(p.suffix == ".pdf" for p in files):
            await asyncio.sleep(0.5)
            if _captures(download_dir) == files:
                return files
        await asyncio.sleep(0.1)
    raise AssertionError(f"expected {count} completed .pdf captures, found {[p.name for p in _captures(download_dir)]}")


def _bytes_of(urls: list[str]) -> list[bytes]:
    return sorted(Path(url.removeprefix("file://")).read_bytes() for url in urls)


@requires_full_chromium
@pytest.mark.asyncio
async def test_real_browser_popup_download_and_late_fallback_save_reach_every_output_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: str
) -> None:
    from playwright.async_api import async_playwright

    run_id = "wr_browser"
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setattr(app, "STORAGE", LocalStorage(artifact_path=str(tmp_path / "artifacts")))
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
    monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", AsyncMock(return_value=[]))
    # The factory's own app is replaced only for the cloud hooks it calls; launch, preferences and listener are real.
    monkeypatch.setattr(
        browser_factory,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                should_apply_banked_cookies=AsyncMock(return_value=False),
                setup_browser_context_extensions=AsyncMock(),
                browser_context_route_handlers_allowed=AsyncMock(return_value=None),
            )
        ),
    )
    monkeypatch.setattr(browser_factory, "restore_session_cookies", AsyncMock())
    monkeypatch.setattr(browser_factory, "restore_signin_cookies", AsyncMock())
    monkeypatch.setattr(browser_factory, "set_browser_console_log", lambda **_: None)
    monkeypatch.setattr(browser_factory, "set_popup_video_listener", lambda **_: None)
    monkeypatch.setattr(browser_factory.settings, "BROWSER_TYPE", "chromium-headless")
    monkeypatch.setattr(browser_factory.settings, "BROWSER_ADDITIONAL_ARGS", ["--proxy-server=direct://"])
    monkeypatch.setattr(browser_factory.settings, "EXTENSIONS", [])
    monkeypatch.setattr(browser_factory, "_get_cdp_port", lambda _: None)
    # The headless shell keeps a downloaded PDF's tab open, so the site never falls back; the full Chromium build in
    # headless mode closes it like a desktop browser. Only the binary channel differs from the factory's arguments.
    factory_args = browser_factory.BrowserContextFactory.build_browser_args
    monkeypatch.setattr(
        browser_factory.BrowserContextFactory,
        "build_browser_args",
        staticmethod(lambda **kwargs: {**factory_args(**kwargs), "channel": "chromium"}),
    )
    monkeypatch.setitem(
        browser_factory.BrowserContextFactory._creators, "chromium-headless", browser_factory._create_headless_chromium
    )
    skyvern_context.set(SkyvernContext(organization_id="o_1", run_id=run_id, workflow_run_id=run_id))
    download_dir = Path(get_download_dir(run_id))
    try:
        async with async_playwright() as playwright:
            context, _, cleanup = await browser_factory.BrowserContextFactory.create_browser_context(playwright)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(f"{site}/bills")
                await page.click("#invoice")
                await _wait_for_captures(download_dir, 2)
                await page.click("#statement")
                await _wait_for_captures(download_dir, 3)
                await page.click("#again")
                await _wait_for_captures(download_dir, 5)
                await page.click("#not-pdf")
                await _wait_for_captures(download_dir, 7)
                await page.click("#save-fails")
                await _wait_for_captures(download_dir, 8)
                async with page.expect_download() as broken:
                    await page.click("#broken")
                assert await (await broken.value).failure() is not None
                await asyncio.sleep(1)
                captures = _captures(download_dir)
            finally:
                await context.close()
                if cleanup:
                    await cleanup()

        captured = {p.name: p.read_bytes() for p in captures}
        assert sorted(captured.values()) == sorted([INVOICE] * 4 + [STATEMENT, RECEIPT, NOT_PDF, NOT_PDF])
        first_invoice = min((p for p in captures if p.read_bytes() == INVOICE), key=lambda p: p.stat().st_mtime)
        public = sorted([INVOICE, STATEMENT, RECEIPT, NOT_PDF, NOT_PDF])

        raw = await app.STORAGE.get_downloaded_files(organization_id="o_1", run_id=run_id)
        assert len(raw) == 8

        files, urls = await WorkflowService()._fetch_downloaded_files(
            SimpleNamespace(workflow_run_id=run_id, organization_id="o_1"), None
        )
        assert files is not None and urls == [f.url for f in files]
        assert _bytes_of(urls) == public
        assert f"file://{first_invoice}" in urls

        response = await _task_response(monkeypatch, organization_id="o_1", workflow_run_id=run_id)
        task_urls = [f.url for f in response.downloaded_files or []]
        assert response.downloaded_file_urls == task_urls
        assert _bytes_of(task_urls) == public

        upload = FileUploadBlock.model_construct()._get_files_to_upload_from_download_dir(
            download_files_path=str(download_dir), max_file_count=300
        )
        assert _bytes_of(upload) == public
        assert str(first_invoice) in upload
        assert len(_captures(download_dir)) == 8
        print(
            "PDF_EVIDENCE "
            + json.dumps(
                {
                    "captures": {name: {"sha256": _sha(data), "bytes": len(data)} for name, data in captured.items()},
                    "run_output": [{"url": f.url, "sha256": f.checksum} for f in files],
                    "task_output": task_urls,
                    "upload": upload,
                    "first_invoice": str(first_invoice),
                }
            )
        )
    finally:
        skyvern_context.reset()
