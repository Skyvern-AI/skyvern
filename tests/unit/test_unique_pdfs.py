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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.files import get_download_dir
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage import unique_pdfs
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.block import FileUploadBlock, UploadToS3Block, bind_downloaded_files_to_output
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.services import run_service, webhook_service
from skyvern.webeye import browser_factory
from tests.unit.conftest import requires_full_chromium
from tests.unit.helpers import StreamedDownloadObjects, make_organization, make_pdf, make_step, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio

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
        with_checksum: bool = True,
        with_size: bool = True,
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
            checksum=_sha(described) if with_checksum else None,
            file_size=len(described) if with_size else None,
            created_at=created,
            modified_at=modified,
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


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> _Rows:
    store = _Rows()
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifacts_by_ids", store.get_artifacts_by_ids)
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
