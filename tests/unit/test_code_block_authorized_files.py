from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Download
from playwright.async_api import Error as PlaywrightError
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage import base as storage_base_module
from skyvern.forge.sdk.artifact.storage import local as local_storage_module
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import PendingFileChooserListener, SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow import code_block_authorized_files
from skyvern.forge.sdk.workflow.code_block_authorized_files import (
    AUTHORIZED_FILE_CHOOSER_UNAVAILABLE_ERROR,
    AuthorizedFileAccessError,
    AuthorizedFileChooserError,
    AuthorizedFileMaterializationFailure,
    BlockDownloadLog,
    FileChooserHandle,
    InlineAttach,
    MaterializedAuthorizedFile,
    RegisteredDownloadIdentity,
    RegisteredDownloadSource,
    authorized_file_attach_modes,
    bind_inline_attach_authorized_file,
    capture_authorized_file,
    capture_claimed_download,
    pin_file_chooser,
    read_authorized_file,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlock, _registered_download_source
from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.browser_artifacts import DownloadBinding
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from tests.unit._file_chooser_fakes import FakeChooserPage, FakeRawCDPChooserPage
from tests.unit.conftest import SESSION_DOWNLOAD_BYTES as _SESSION_BYTES
from tests.unit.conftest import make_claimed_download_mock
from tests.unit.conftest import registered_download_row as _registered_row


@pytest.fixture
def authorized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "downloads"
    run_dir = root / "wr_authorized"
    run_dir.mkdir(parents=True)
    path = run_dir / "resume.pdf"
    path.write_bytes(b"retained authorized bytes")
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(root))
    return path, capture_authorized_file(
        path,
        download_root=root,
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024 * 1024 * 10,
    )


def test_verified_read_rejects_cross_scope_replacement_mutation_and_traversal(authorized_file, tmp_path: Path) -> None:
    path, materialized = authorized_file
    assert (
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        ).content
        == b"retained authorized bytes"
    )

    # The upload name may differ from the stored name after a collision, but it is only ever sent as a plain name.
    path_as_name = replace(materialized, filename="../resume.pdf")
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            path_as_name,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_other",
            organization_id="org_authorized",
            max_bytes=1024,
        )
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_other",
            max_bytes=1024,
        )

    path.write_bytes(b"x" * materialized.size)
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    replacement = path.with_suffix(".new")
    replacement.write_bytes(b"replacement bytes")
    os.replace(replacement, path)
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    escaped = replace(materialized, relative_path="../neighbor.pdf")
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            escaped,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    absolute = replace(materialized, relative_path=str(path))
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            absolute,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )


def test_verified_read_rejects_symlink_missing_and_upload_ceiling(authorized_file) -> None:
    path, materialized = authorized_file
    escaped_target = path.parents[1] / "neighbor.pdf"
    escaped_target.write_bytes(b"neighboring file")
    path.unlink()
    path.symlink_to(escaped_target)
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    path.unlink()
    with pytest.raises(AuthorizedFileAccessError):
        read_authorized_file(
            materialized,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=1024,
        )

    path.write_bytes(b"retained authorized bytes")
    refreshed = capture_authorized_file(
        path,
        download_root=path.parents[1],
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024 * 1024 * 10,
    )
    with pytest.raises(AuthorizedFileAccessError, match="upload limit"):
        read_authorized_file(
            refreshed,
            download_root=path.parents[1],
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=4,
        )


@pytest.mark.asyncio
async def test_inline_helper_distinguishes_a_failed_file_input_from_an_ordinary_string(authorized_file) -> None:
    path, materialized = authorized_file
    page = object()
    attach = bind_inline_attach_authorized_file(
        page,
        {"resume": materialized, "cover_letter": AuthorizedFileMaterializationFailure()},
        {"resume": str(path), "cover_letter": "https://invalid.example/cover.pdf", "note": "just text"},
        download_root=path.parents[1],
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024,
    )

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, "https://invalid.example/cover.pdf", "#file")
    with pytest.raises(AuthorizedFileAccessError, match="accepts only a materialized file_url"):
        await attach(page, "just text", "#file")
    with pytest.raises(AuthorizedFileAccessError, match="accepts only a materialized file_url"):
        await attach(page, {"not": "a string"}, "#file")  # type: ignore[arg-type]


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page, self._selector = page, selector

    async def set_input_files(self, files: dict[str, str | bytes]) -> None:
        self._page.attached.append((self._selector, files))


class _FakePage:
    def __init__(self) -> None:
        self.attached: list[tuple[str, dict[str, str | bytes]]] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


@pytest.mark.asyncio
async def test_inline_helper_never_hands_bytes_to_an_object_authored_code_supplied(authorized_file) -> None:
    """Only the page may resolve the target: a duck-typed locator from authored code would receive the buffer."""
    path, materialized = authorized_file
    page = _FakePage()
    attach = bind_inline_attach_authorized_file(
        page,
        {"resume": materialized},
        {"resume": str(path)},
        download_root=path.parents[1],
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024,
    )

    class Exfiltrator:
        received: list[dict[str, str | bytes]] = []

        async def set_input_files(self, files: dict[str, str | bytes]) -> None:
            self.received.append(files)

    with pytest.raises(AuthorizedFileAccessError, match="selector string"):
        await attach(page, str(path), Exfiltrator())  # type: ignore[arg-type]
    with pytest.raises(AuthorizedFileAccessError, match="selector string"):
        await attach(page, str(path), "")
    assert Exfiltrator.received == []

    receipt = await attach(page, str(path), "#file")
    assert receipt == {"filename": "resume.pdf", "size": len(b"retained authorized bytes")}
    assert page.attached[0][0] == "#file"


@pytest.mark.asyncio
async def test_rebinding_page_locator_after_bind_cannot_capture_the_file_bytes(authorized_file) -> None:
    """Inline code may reassign page.locator; the helper must still upload through the genuine page."""
    path, materialized = authorized_file
    page = _FakePage()
    attach = bind_inline_attach_authorized_file(
        page,
        {"resume": materialized},
        {"resume": str(path)},
        download_root=path.parents[1],
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024,
    )
    stolen: list[dict[str, str | bytes]] = []

    class Exfiltrator:
        async def set_input_files(self, files: dict[str, str | bytes]) -> None:
            stolen.append(files)

    page.locator = lambda selector: Exfiltrator()  # type: ignore[method-assign]

    receipt = await attach(page, str(path), "#file")

    assert stolen == []
    assert receipt == {"filename": "resume.pdf", "size": len(b"retained authorized bytes")}
    assert page.attached == [("#file", page.attached[0][1])]
    assert page.attached[0][1]["buffer"] == b"retained authorized bytes"


@pytest.mark.asyncio
async def test_inline_helper_accepts_a_claimed_download_but_not_its_path_or_a_lookalike(
    authorized_file, tmp_path: Path
) -> None:
    """A download this run claimed is a handle; the worker path it happens to sit at stays refused."""
    path, materialized = authorized_file
    run_dir = path.parent
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"not this run")
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {"resume": materialized},
        {"resume": str(path)},
        download_root=path.parents[1],
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    delivered = run_dir / "1f0c9b2e-4a7d-guid"
    delivered.write_bytes(b"certificate bytes")

    receipt = await attach(
        page,
        started(make_claimed_download_mock(path=delivered, suggested_filename="../Vendor Certificate.pdf")),
        "#file",
    )

    assert receipt == {"filename": "Vendor Certificate.pdf", "size": len(b"certificate bytes")}
    assert page.attached == [("#file", page.attached[0][1])]
    assert page.attached[0][1]["name"] == "Vendor Certificate.pdf"
    assert page.attached[0][1]["buffer"] == b"certificate bytes"

    class LooksLikeADownload:
        suggested_filename = "certificate.pdf"

        async def path(self) -> str:
            return str(delivered)

    with pytest.raises(AuthorizedFileAccessError, match="accepts only a materialized file"):
        await attach(page, str(delivered), "#file")
    with pytest.raises(AuthorizedFileAccessError, match="accepts only a materialized file"):
        await attach(page, LooksLikeADownload(), "#file")  # type: ignore[arg-type]
    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(
            page, started(make_claimed_download_mock(path=outside, suggested_filename="elsewhere.pdf")), "#file"
        )
    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, started(make_claimed_download_mock(path=None, suggested_filename="cancelled.pdf")), "#file")
    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(
            page,
            started(
                make_claimed_download_mock(
                    path=delivered, suggested_filename="x.pdf", path_error=RuntimeError("cancelled")
                )
            ),
            "#file",
        )
    assert len(page.attached) == 1


@pytest.mark.asyncio
async def test_a_claimed_download_is_contained_by_the_directory_the_run_downloads_into(tmp_path: Path) -> None:
    """A run whose downloads land under its own download id must be read there, and only there."""
    root = tmp_path / "downloads"
    download_dir = root / "dl_run"
    download_dir.mkdir(parents=True)
    workflow_dir = root / "wr_workflow"
    workflow_dir.mkdir()
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=root,
        workflow_run_id="wr_workflow",
        organization_id="org_authorized",
        max_bytes=1024,
        download_run_id="dl_run",
        download_log=log,
    )
    delivered = download_dir / "guid-file"
    delivered.write_bytes(b"claimed bytes")
    neighbor = workflow_dir / "guid-file"
    neighbor.write_bytes(b"another run's bytes")

    receipt = await attach(
        page, started(make_claimed_download_mock(path=delivered, suggested_filename="certificate.pdf")), "#file"
    )

    assert receipt == {"filename": "certificate.pdf", "size": len(b"claimed bytes")}
    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(
            page, started(make_claimed_download_mock(path=neighbor, suggested_filename="certificate.pdf")), "#file"
        )
    assert len(page.attached) == 1


class _RawLocator:
    def __init__(self, page: _RawPage, selector: str) -> None:
        self.page, self._selector = page, selector

    async def set_input_files(self, files: dict[str, str | bytes]) -> None:
        self.page.attached.append((self._selector, files))


class _RawPage:
    def __init__(self) -> None:
        self.attached: list[tuple[str, dict[str, str | bytes]]] = []

    def locator(self, selector: str) -> _RawLocator:
        return _RawLocator(self, selector)


@pytest.mark.asyncio
async def test_authored_code_cannot_redirect_an_inline_attach_through_the_page_behind_the_recording(
    authorized_file,
) -> None:
    """A recorded locator hands authored code the raw page, where it can shadow `locator`; the upload must not follow."""
    path, materialized = authorized_file
    raw_page = _RawPage()
    recording_page = RecordingPage(raw_page)
    now = datetime.now(UTC)
    block = CodeBlock(
        label="attach_block",
        code="""
stolen = []
class Sink:
    async def set_input_files(self, files):
        stolen.append(files)
raw_page = page.locator("#upload").page
raw_page.locator = lambda selector: Sink()
receipt = await attach_authorized_file(page, resume, "#upload")
""",
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="attach_output",
            description="test output",
            output_parameter_id="op_attach",
            workflow_id="w_test",
            created_at=now,
            modified_at=now,
        ),
    )

    result = await block.generate_async_user_function(
        block.code,
        recording_page,  # type: ignore[arg-type]
        {"resume": str(path)},
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        authorized_file_materializations={"resume": materialized},
    )()

    assert result["stolen"] == []
    assert result["receipt"] == {"filename": "resume.pdf", "size": len(b"retained authorized bytes")}
    assert [(selector, files["buffer"]) for selector, files in raw_page.attached] == [
        ("#upload", b"retained authorized bytes")
    ]
    recorded = recording_page.recorded_actions()
    assert [action.action_type for action in recorded] == [ActionType.UPLOAD_FILE]
    assert "retained authorized bytes" not in repr(recorded)


def test_an_oversized_file_is_materialized_but_its_capability_is_refused_on_size(tmp_path: Path) -> None:
    """Capture skips hashing a file above the ceiling, so redemption must refuse it before comparing digests."""
    run_dir = tmp_path / "wr_authorized"
    run_dir.mkdir()
    path = run_dir / "large.pdf"
    path.write_bytes(b"more than four bytes")

    materialized = capture_authorized_file(
        path,
        download_root=tmp_path,
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        max_bytes=4,
    )

    assert materialized.size == len(b"more than four bytes")
    with pytest.raises(AuthorizedFileAccessError, match="upload limit"):
        read_authorized_file(
            materialized,
            download_root=tmp_path,
            workflow_run_id="wr_authorized",
            organization_id="org_authorized",
            max_bytes=4,
        )


@asynccontextmanager
async def _quiet_settle() -> AsyncIterator[None]:
    yield


class _SettlingInterceptor:
    """Stands in for the interceptor whose drain finishes writing the file the claim is about."""

    def __init__(self, target: Path, payload: bytes) -> None:
        self._target, self._payload = target, payload

    @asynccontextmanager
    async def settle_browser_downloads(self) -> AsyncIterator[None]:
        self._target.write_bytes(self._payload)
        yield


def _context_with(interceptor: object) -> SimpleNamespace:
    return SimpleNamespace(_skyvern_cdp_download_interceptor=interceptor)


def _browser_download_log() -> tuple[BlockDownloadLog, Callable[[Any], Any]]:
    """A log over a fake tab, and a hook that emits a download on it the way the browser does."""
    tab = _EventSource()
    log = BlockDownloadLog(_EventContext(tab))

    def started(download: Any) -> Any:
        tab.emit("download", download)
        return download

    return log, started


def _bind_claimed_download_attach(page: _FakePage, run_dir: Path) -> Callable[..., Awaitable[dict[str, str | int]]]:
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )

    async def attach_started(target: Any, file: Any, selector: str) -> dict[str, str | int]:
        return await attach(target, started(file), selector)

    return attach_started


@pytest.mark.asyncio
async def test_a_claimed_download_is_found_where_the_browser_settled_it(tmp_path: Path) -> None:
    """Production binds downloads with setDownloadBehavior, so Chrome writes the suggested name into the
    run directory itself and Playwright's own path points at a launch-directory guid that never exists."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    settled = run_dir / "vendor-certificate.pdf"
    settled.write_bytes(b"settled certificate bytes")

    absent_guid = await attach(
        page,
        make_claimed_download_mock(
            path=tmp_path / "launch-dir" / "1f0c9b2e-4a7d-guid",
            suggested_filename="vendor-certificate.pdf",
        ),
        "#file",
    )
    no_path = await attach(
        page,
        make_claimed_download_mock(path=None, suggested_filename="vendor-certificate.pdf"),
        "#file",
    )

    assert absent_guid == no_path == {"filename": settled.name, "size": len(b"settled certificate bytes")}
    assert [files["buffer"] for _, files in page.attached] == [b"settled certificate bytes"] * 2
    assert [files["name"] for _, files in page.attached] == [settled.name] * 2


@pytest.mark.asyncio
async def test_a_claimed_download_is_captured_once_the_interceptor_drain_has_written_it(tmp_path: Path) -> None:
    """The file can still be in flight when the block claims it; the capture settles the context first."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    settled = run_dir / "certificate.pdf"

    receipt = await attach(
        page,
        make_claimed_download_mock(
            path=None,
            suggested_filename="certificate.pdf",
            context=_context_with(_SettlingInterceptor(settled, b"drained bytes")),
        ),
        "#file",
    )

    assert receipt == {"filename": "certificate.pdf", "size": len(b"drained bytes")}
    assert page.attached[0][1]["buffer"] == b"drained bytes"


@pytest.mark.asyncio
async def test_a_run_directory_file_is_only_taken_for_a_download_the_browser_completed(tmp_path: Path) -> None:
    """Without Playwright's own path the file is located by name, so every other signal has to hold."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    stale = run_dir / "earlier.pdf"
    stale.write_bytes(b"from before this block")
    an_hour_ago = time.time_ns() - 3_600_000_000_000
    os.utime(stale, ns=(an_hour_ago, an_hour_ago))
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    fresh = run_dir / "certificate.pdf"
    fresh.write_bytes(b"settled certificate bytes")
    monitor = MagicMock(spec=CDPDownloadInterceptor)
    monitor.is_monitoring_browser_downloads.return_value = True
    monitor.settle_browser_downloads.side_effect = _quiet_settle

    for download in (
        make_claimed_download_mock(path=None, suggested_filename="certificate.pdf", failure="canceled"),
        make_claimed_download_mock(path=None, suggested_filename="certificate.pdf", failure_error=RuntimeError("gone")),
        make_claimed_download_mock(path=None, suggested_filename="earlier.pdf"),
        make_claimed_download_mock(path=None, suggested_filename="unrelated.pdf"),
        make_claimed_download_mock(path=None, suggested_filename=""),
        make_claimed_download_mock(path=None, suggested_filename="certificate.pdf", context=_context_with(monitor)),
    ):
        with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
            await attach(page, download, "#file")

    assert page.attached == []
    assert fresh.is_file()


@pytest.mark.asyncio
async def test_a_collision_suffixed_copy_of_the_suggested_name_is_the_one_that_uploads(tmp_path: Path) -> None:
    """A binding that uniquifies a collision writes "name (1).ext" beside an earlier block's file; only
    the fresh copy is this block's, so the stale original is not a second candidate."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    stale = run_dir / "certificate.pdf"
    stale.write_bytes(b"an earlier block's copy")
    before_block = stale.stat().st_mtime_ns - 1_000_000
    os.utime(stale, ns=(before_block, before_block))
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    uniquified = run_dir / "certificate (1).pdf"
    uniquified.write_bytes(b"this block's copy")

    receipt = await attach(page, make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"), "#file")

    assert receipt == {"filename": "certificate.pdf", "size": len(b"this block's copy")}
    assert page.attached[0][1]["buffer"] == b"this block's copy"


@pytest.mark.asyncio
async def test_a_download_stamped_before_the_block_started_is_still_this_block_s_file(tmp_path: Path) -> None:
    """Linux stamps files from a coarse clock, so a download that settles moments after the block
    starts can carry an earlier mtime. Freshness is decided by what the run directory already held."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    settled = run_dir / "certificate.pdf"
    settled.write_bytes(b"settled after the block began")
    backdated = time.time_ns() - 5_000_000
    os.utime(settled, ns=(backdated, backdated))

    receipt = await attach(page, make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"), "#file")

    assert receipt == {"filename": "certificate.pdf", "size": len(b"settled after the block began")}


@pytest.mark.asyncio
async def test_a_stalled_download_is_refused_when_the_settlement_window_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transfer that fires its download event and then stalls must not hold the runner past the
    settlement window: path() and failure() never return here, and the attach still refuses in time."""
    monkeypatch.setattr(code_block_authorized_files, "SAVE_DOWNLOADED_FILES_TIMEOUT", 0.2)
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    stalled = make_claimed_download_mock(path=None, suggested_filename="certificate.pdf")
    never = asyncio.Event()

    async def hang(*_: object) -> None:
        await never.wait()

    stalled.path.side_effect = hang
    stalled.failure.side_effect = hang

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await asyncio.wait_for(attach(page, stalled, "#file"), timeout=5)
    assert page.attached == []


@pytest.mark.asyncio
async def test_a_lookalike_download_cannot_upload_an_earlier_block_s_file(tmp_path: Path) -> None:
    """Authored code can build an object with every download attribute. Only an object the browser
    itself emitted is proof of a claim, and a file the run already held is never this block's."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    earlier = run_dir / "payroll.pdf"
    earlier.write_bytes(b"an earlier block's download")
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )

    fresh = run_dir / "this-block.pdf"
    fresh.write_bytes(b"a file this block holds")

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, make_claimed_download_mock(path=fresh, suggested_filename="this-block.pdf"), "#file")
    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, started(make_claimed_download_mock(path=earlier, suggested_filename="payroll.pdf")), "#file")
    assert page.attached == []


@pytest.mark.asyncio
async def test_a_same_name_download_that_starts_during_capture_refuses_the_earlier_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supersession check before the scan leaves a window: a second same-name download can start and
    overwrite the file while it is being captured. The check runs again once the bytes are held."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    first = started(make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"))
    (run_dir / "certificate.pdf").write_bytes(b"first copy")
    real_capture = code_block_authorized_files.capture_authorized_file

    def capture_while_a_second_download_lands(path: Path, **kwargs: Any) -> Any:
        started(make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"))
        (run_dir / "certificate.pdf").write_bytes(b"second copy")
        return real_capture(path, **kwargs)

    monkeypatch.setattr(code_block_authorized_files, "capture_authorized_file", capture_while_a_second_download_lands)

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, first, "#file")
    assert page.attached == []


@pytest.mark.asyncio
async def test_closing_the_log_releases_a_stalled_download_s_tracker() -> None:
    """A stalled transfer never resolves failure(). Closing the log must end its tracker, which otherwise
    holds the log and its browser context past the block, on a persistent context for the session."""
    log, started = _browser_download_log()
    never = asyncio.Event()
    stalled = make_claimed_download_mock(path=None, suggested_filename="report.pdf")
    stalled.failure.side_effect = never.wait
    started(stalled)
    await asyncio.sleep(0.01)
    assert not any(completion.done() for completion in log._completions)

    log.close()
    await asyncio.sleep(0.01)

    assert all(completion.done() for completion in log._completions)


class _EventSource:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[..., None]]] = {}

    def on(self, event: str, handler: Callable[..., None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[..., None]) -> None:
        self.handlers[event].remove(handler)

    def emit(self, event: str, value: object) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(value)


class _EventContext(_EventSource):
    def __init__(self, *pages: _EventSource) -> None:
        super().__init__()
        self.pages = list(pages)


@pytest.mark.asyncio
async def test_a_download_overwritten_by_a_later_same_name_download_is_refused(tmp_path: Path) -> None:
    """Chrome overwrites a same-name download in place under the run-scoped binding. When the first has
    finished before the second starts, the file holds the second's bytes: only the second may attach."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    tab = _EventSource()
    context = _EventContext(tab)
    log = BlockDownloadLog(context)
    page = _FakePage()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    # A finished Playwright download reports its launch-directory guid, which never exists here.
    first = make_claimed_download_mock(path=tmp_path / "launch-dir" / "guid-1", suggested_filename="certificate.pdf")
    second = make_claimed_download_mock(path=tmp_path / "launch-dir" / "guid-2", suggested_filename="certificate.pdf")
    tab.emit("download", first)
    await asyncio.sleep(0.01)
    tab.emit("download", second)
    await asyncio.sleep(0.01)
    (run_dir / "certificate.pdf").write_bytes(b"second copy")

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, first, "#file")
    receipt = await attach(page, second, "#file")

    assert receipt == {"filename": "certificate.pdf", "size": len(b"second copy")}
    assert [files["buffer"] for _, files in page.attached] == [b"second copy"]
    log.close()
    assert tab.handlers["download"] == [] and context.handlers["page"] == []


@pytest.mark.asyncio
async def test_a_download_whose_wait_timed_out_still_counts_as_in_flight(tmp_path: Path) -> None:
    """The raw-CDP engine's failure() gives up after a fixed wait and reports no failure while the transfer
    keeps running. Such a download is not finished, so a same-name download started meanwhile overlaps it."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    # failure() and path() both come back empty, exactly as they do once the engine's wait expires.
    started(make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"))
    await asyncio.sleep(0.01)
    later = started(
        make_claimed_download_mock(path=tmp_path / "launch-dir" / "guid", suggested_filename="certificate.pdf")
    )
    await asyncio.sleep(0.01)
    (run_dir / "certificate.pdf").write_bytes(b"the slow download's bytes")

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, later, "#file")
    assert page.attached == []
    log.close()


@pytest.mark.asyncio
async def test_a_lookalike_is_refused_at_the_proof_of_claim_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability check is structural, so authored code can build an object that satisfies it. Only the
    log proves the browser emitted this one, and that refusal lands before the object is read: the
    supersession rules also reject an unknown object today, and must not be the only thing holding this."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    started(make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"))
    await asyncio.sleep(0.01)
    (run_dir / "certificate.pdf").write_bytes(b"this block's download")
    lookalike = make_claimed_download_mock(path=run_dir / "certificate.pdf", suggested_filename="certificate.pdf")
    decisions: list[tuple[str, object]] = []
    monkeypatch.setattr(
        code_block_authorized_files.LOG,
        "info",
        lambda event, **fields: decisions.append((event, fields.get("outcome"))),
    )

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, lookalike, "#file")

    assert ("codeblock.download_authorize_decision", "refused_unclaimed_download_object") in decisions
    assert lookalike.path.await_count == 0
    assert page.attached == []


@pytest.mark.asyncio
async def test_rebinding_path_to_a_sibling_download_cannot_swap_its_bytes(tmp_path: Path) -> None:
    """In-process code can rebind path() on the genuine object it was handed. The log keeps the path()
    the browser's object had when emitted, so the claim still resolves to its own download."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    (run_dir / "a.pdf").write_bytes(b"a's own bytes")
    (run_dir / "b.pdf").write_bytes(b"b's bytes")
    a = started(make_claimed_download_mock(path=tmp_path / "launch-dir" / "guid-a", suggested_filename="a.pdf"))
    b = started(make_claimed_download_mock(path=run_dir / "b.pdf", suggested_filename="b.pdf"))
    await asyncio.sleep(0.01)
    a.path = b.path

    receipt = await attach(page, a, "#file")

    assert receipt == {"filename": "a.pdf", "size": len(b"a's own bytes")}
    assert page.attached[0][1]["buffer"] == b"a's own bytes"


@pytest.mark.asyncio
async def test_a_redirected_path_cannot_reach_an_inherited_file_in_a_subdirectory(tmp_path: Path) -> None:
    """In-process code can reassign path() on the genuine download object, which passes the log check.
    An earlier block's file nested under the run directory must still read as inherited."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    (run_dir / "earlier").mkdir(parents=True)
    nested = run_dir / "earlier" / "payroll.pdf"
    nested.write_bytes(b"an earlier block's download")
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    genuine = started(make_claimed_download_mock(path=None, suggested_filename="receipt.pdf"))
    genuine.path.return_value = str(nested)

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, genuine, "#file")
    assert page.attached == []


@pytest.mark.asyncio
async def test_same_name_downloads_in_flight_together_are_both_refused(tmp_path: Path) -> None:
    """The file holds whichever same-name download finishes last, not whichever started last. Here the
    earlier one is still transferring when the later starts and finishes last, so the file is its bytes:
    neither claim can prove the file is its own."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
    )
    still_transferring = asyncio.Event()

    async def unfinished() -> None:
        await still_transferring.wait()

    earlier = make_claimed_download_mock(path=None, suggested_filename="certificate.pdf")
    earlier.failure.side_effect = unfinished
    started(earlier)
    await asyncio.sleep(0.01)
    later = started(make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"))
    await asyncio.sleep(0.01)
    (run_dir / "certificate.pdf").write_bytes(b"earlier download's bytes")

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, later, "#file")
    assert page.attached == []
    still_transferring.set()


@pytest.mark.asyncio
async def test_a_percent_encoded_name_is_matched_on_disk_as_the_browser_wrote_it(tmp_path: Path) -> None:
    """The browser saves a literal "%20"; matching the URL-decoded upload name would miss it."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    (run_dir / "cert%20copy.pdf").write_bytes(b"literal percent bytes")

    await attach(page, make_claimed_download_mock(path=None, suggested_filename="cert%20copy.pdf"), "#file")

    assert page.attached[0][1]["buffer"] == b"literal percent bytes"


@pytest.mark.asyncio
async def test_two_same_name_downloads_in_one_block_refuse_rather_than_upload_either(tmp_path: Path) -> None:
    """Where a binding uniquifies instead of overwriting, two fresh same-name files are indistinguishable
    to the worker, so the attach refuses rather than guess; uploading the wrong document is the harm."""
    run_dir = tmp_path / "downloads" / "wr_claimed"
    run_dir.mkdir(parents=True)
    page = _FakePage()
    attach = _bind_claimed_download_attach(page, run_dir)
    first = run_dir / "certificate.pdf"
    first.write_bytes(b"first invoice")
    second = run_dir / "certificate (1).pdf"
    second.write_bytes(b"second invoice")

    with pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, make_claimed_download_mock(path=None, suggested_filename="certificate.pdf"), "#file")

    assert page.attached == []


RegistrationRead = list[FileInfo] | BaseException | None


class _SourceOverrides(TypedDict, total=False):
    baseline: Mapping[RegisteredDownloadIdentity, str] | None
    stored: bytes
    artifact_run_id: str
    retrieve_error: BaseException


def _registered_downloads(
    monkeypatch: pytest.MonkeyPatch,
    *reads: RegistrationRead,
    baseline: Mapping[RegisteredDownloadIdentity, str] | None = MappingProxyType({}),
    stored: bytes = _SESSION_BYTES,
    artifact_run_id: str = "wr_session",
    retrieve_error: BaseException | None = None,
) -> RegisteredDownloadSource:
    """The run's registration as the worker reads it: one probe result per poll, the last one repeating."""
    monkeypatch.setattr(code_block_authorized_files, "_REGISTERED_DOWNLOAD_POLL_SECONDS", 0)
    remaining = list(reads)

    async def probe() -> tuple[list[FileInfo] | None, set[str]]:
        read = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(read, BaseException):
            raise read
        return read, set()

    artifact = SimpleNamespace(artifact_type=ArtifactType.DOWNLOAD, run_id=artifact_run_id)
    monkeypatch.setattr(app.DATABASE.artifacts, "get_artifact_by_id", AsyncMock(return_value=artifact))
    retrieve = AsyncMock(side_effect=retrieve_error) if retrieve_error is not None else AsyncMock(return_value=stored)
    monkeypatch.setattr(app.STORAGE, "retrieve_artifact", retrieve)
    return RegisteredDownloadSource(
        probe=probe, organization_id="org_authorized", run_id="wr_session", baseline=baseline
    )


def _remote_download(*, failure: str | None = None, context: object | None = None) -> Download:
    """A download on a remote browser: its path() cannot be read from this worker."""
    return make_claimed_download_mock(
        path=None,
        suggested_filename="certificate.pdf",
        path_error=RuntimeError("remote"),
        failure=failure,
        context=context,
    )


SessionAttach = Callable[[_FakePage, str | Download, str], Awaitable[dict[str, str | int]]]


def _session_attach(
    page: _FakePage, run_dir: Path, registered: RegisteredDownloadSource
) -> tuple[SessionAttach, Callable[[Download], Download]]:
    log, started = _browser_download_log()
    attach = bind_inline_attach_authorized_file(
        page,
        {},
        {},
        download_root=run_dir.parent,
        workflow_run_id=run_dir.name,
        organization_id="org_authorized",
        max_bytes=1024,
        download_log=log,
        registered_downloads=registered,
    )

    async def attach_started(target: _FakePage, file: str | Download, selector: str) -> dict[str, str | int]:
        return await attach(target, file if isinstance(file, str) else started(file), selector)

    return attach_started, started


def _decisions(logs: list[dict[str, Any]]) -> list[str]:
    return [log["outcome"] for log in logs if log["event"] == "codeblock.download_authorize_decision"]


async def _download_rows_registration_adds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> int:
    """Run the real local registration over the run directory and count the DOWNLOAD rows it creates."""
    create_download_artifact = AsyncMock()
    monkeypatch.setattr(
        local_storage_module,
        "app",
        SimpleNamespace(
            ARTIFACT_MANAGER=SimpleNamespace(create_download_artifact=create_download_artifact),
            DATABASE=SimpleNamespace(
                artifacts=SimpleNamespace(list_artifacts_for_run_by_type=AsyncMock(return_value=[]))
            ),
        ),
    )
    monkeypatch.setattr(local_storage_module, "get_download_dir", lambda run_id: str(run_dir))
    monkeypatch.setattr(
        storage_base_module, "resolve_download_attempt", AsyncMock(return_value=("wr_session", 1, None))
    )
    await LocalStorage(str(tmp_path / "artifacts")).save_downloaded_files(
        organization_id="org_authorized", run_id="wr_session"
    )
    return create_download_artifact.await_count


@pytest.mark.asyncio
async def test_a_session_download_uploads_from_the_registered_artifact_not_the_local_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent remote browser never writes the bytes to this worker, so the upload is resolved from the
    one DOWNLOAD artifact the claim registered, checked against its checksum."""
    run_dir = tmp_path / "downloads" / "wr_session"
    inherited = _registered_row("earlier.pdf", b"an earlier block's file", artifact_id="a_earlier")
    registered = _registered_downloads(
        monkeypatch,
        [inherited],
        [inherited, _registered_row()],
        baseline={(inherited.filename, inherited.checksum): inherited.filename},
    )
    page = _FakePage()
    attach, _ = _session_attach(page, run_dir, registered)

    with capture_logs() as logs:
        receipt = await attach(page, _remote_download(), "#file")

    assert receipt == {"filename": "certificate.pdf", "size": len(_SESSION_BYTES)}
    assert page.attached == [
        ("#file", {"name": "certificate.pdf", "mimeType": "application/pdf", "buffer": _SESSION_BYTES})
    ]
    app.DATABASE.artifacts.get_artifact_by_id.assert_awaited_once_with("a_session", "org_authorized")
    assert _decisions(logs) == ["authorized_registered_artifact"]
    assert await _download_rows_registration_adds(tmp_path, monkeypatch, run_dir) == 0
    (private_copy,) = run_dir.glob(".authorized-*/*")
    with pytest.raises(AuthorizedFileAccessError, match="accepts only a materialized file"):
        await attach(page, str(private_copy), "#file")
    assert len(page.attached) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        ("canceled", "authorized_registered_artifact"),
        (None, "authorized_registered_artifact"),
        ("network failed", "refused_browser_reported_failure"),
    ],
)
async def test_a_monitor_owned_download_uploads_from_its_registered_artifact_unless_it_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, outcome: str
) -> None:
    """The monitor denies the browser's own transfer, so Playwright calls it cancelled even when it arrived."""
    run_dir = tmp_path / "downloads" / "wr_session"
    run_dir.mkdir(parents=True)
    (run_dir / "certificate.pdf").write_bytes(b"a monitor-saved file this download cannot be tied to")
    monitor = MagicMock(spec=CDPDownloadInterceptor)
    monitor.is_monitoring_browser_downloads.return_value = True
    monitor.settle_browser_downloads.side_effect = _quiet_settle
    page = _FakePage()
    attach, _ = _session_attach(page, run_dir, _registered_downloads(monkeypatch, [_registered_row()]))

    with capture_logs() as logs, suppress(AuthorizedFileAccessError):
        await attach(page, _remote_download(failure=failure, context=_context_with(monitor)), "#file")

    assert _decisions(logs) == [outcome]
    expected = [_SESSION_BYTES] if outcome == "authorized_registered_artifact" else []
    assert [files["buffer"] for _, files in page.attached] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reads", "source", "failure", "outcome"),
    [
        ([[_registered_row(checksum="0" * 64)]], {}, None, "refused_registered_checksum_mismatch"),
        ([[_registered_row()]], {"stored": b"bytes storage changed"}, None, "refused_registered_checksum_mismatch"),
        ([[]], {}, None, "refused_no_registered_download"),
        (
            [[_registered_row()]],
            {"baseline": {("certificate.pdf", hashlib.sha256(_SESSION_BYTES).hexdigest()): "certificate.pdf"}},
            None,
            "refused_no_registered_download",
        ),
        (
            [[_registered_row(), _registered_row(content=b"a second copy", artifact_id="a_other")]],
            {},
            None,
            "refused_ambiguous_registered_download",
        ),
        (
            [[_registered_row(), _registered_row("certificate (1).pdf", b"a collision copy", artifact_id="a_copy")]],
            {},
            None,
            "refused_ambiguous_registered_download",
        ),
        ([[_registered_row(artifact_id=None)]], {}, None, "refused_registered_download_without_artifact"),
        ([[_registered_row(checksum="")]], {}, None, "refused_registered_download_without_checksum"),
        ([[_registered_row(file_size=4096)]], {}, None, "refused_registered_download_too_large"),
        (
            [[_registered_row().model_copy(update={"file_size": None})]],
            {},
            None,
            "refused_registered_download_without_size",
        ),
        ([[_registered_row()]], {"artifact_run_id": "wr_other"}, None, "refused_registered_artifact_unavailable"),
        ([None], {}, None, "refused_registration_unreadable"),
        ([RuntimeError("database down")], {}, None, "refused_registration_unreadable"),
        ([TimeoutError()], {}, None, "refused_no_registered_download"),
        ([[_registered_row()]], {"retrieve_error": OSError("storage down")}, None, "refused_registration_unreadable"),
        ([[_registered_row()]], {"baseline": None}, None, "refused_registration_baseline_unreadable"),
        ([[_registered_row()]], {}, "network failed", "refused_browser_reported_failure"),
    ],
    ids=[
        "row-checksum-mismatch",
        "stored-bytes-mismatch",
        "nothing-registered",
        "row-already-in-baseline",
        "two-matching-rows",
        "exact-and-collision-copy-rows",
        "row-without-artifact-id",
        "row-without-checksum",
        "known-oversize",
        "row-without-size",
        "artifact-of-another-run",
        "registration-unreadable",
        "probe-raises",
        "every-read-times-out",
        "storage-raises",
        "baseline-unreadable",
        "session-transfer-failed",
    ],
)
async def test_a_session_download_is_refused_unless_exactly_one_verified_registered_row_is_its_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reads: list[RegistrationRead],
    source: _SourceOverrides,
    failure: str | None,
    outcome: str,
) -> None:
    monkeypatch.setattr(code_block_authorized_files, "SAVE_DOWNLOADED_FILES_TIMEOUT", 0.2)
    run_dir = tmp_path / "downloads" / "wr_session"
    page = _FakePage()
    attach, _ = _session_attach(page, run_dir, _registered_downloads(monkeypatch, *reads, **source))

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, _remote_download(failure=failure), "#file")

    assert page.attached == []
    assert _decisions(logs) == [outcome]


@pytest.mark.asyncio
async def test_a_registration_read_that_outlasts_its_cap_is_polled_again_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read can wait on the download still in flight, so one slow read must not refuse a download that registers."""
    monkeypatch.setattr(code_block_authorized_files, "_REGISTERED_DOWNLOAD_PROBE_TIMEOUT_SECONDS", 0.05)
    registered = _registered_downloads(monkeypatch, [_registered_row()])
    reads = 0

    async def slow_first_read() -> tuple[list[FileInfo] | None, set[str]]:
        nonlocal reads
        reads += 1
        if reads == 1:
            await asyncio.sleep(1)
        return await registered.probe()

    page = _FakePage()
    attach, _ = _session_attach(page, tmp_path / "downloads" / "wr_session", replace(registered, probe=slow_first_read))

    with capture_logs() as logs:
        await attach(page, _remote_download(), "#file")

    assert _decisions(logs) == ["authorized_registered_artifact"]
    assert [files["buffer"] for _, files in page.attached] == [_SESSION_BYTES]


@pytest.mark.asyncio
async def test_a_session_download_uploads_from_a_registered_collision_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding that uniquifies registers the claim as "name (1).ext"; it still uploads under the suggested name."""
    registered = _registered_downloads(monkeypatch, [_registered_row("certificate (1).pdf")])
    page = _FakePage()
    attach, _ = _session_attach(page, tmp_path / "downloads" / "wr_session", registered)

    with capture_logs() as logs:
        await attach(page, _remote_download(), "#file")

    assert _decisions(logs) == ["authorized_registered_artifact"]
    assert page.attached == [
        ("#file", {"name": "certificate.pdf", "mimeType": "application/pdf", "buffer": _SESSION_BYTES})
    ]


@pytest.mark.asyncio
async def test_a_session_download_without_a_block_download_log_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the block's log no earlier same-name download can be ruled out as the owner of the row."""
    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await capture_claimed_download(
            _remote_download(),
            download_root=tmp_path / "downloads",
            download_run_id="wr_session",
            organization_id="org_authorized",
            max_bytes=1024,
            inherited_files=frozenset(),
            registered=_registered_downloads(monkeypatch, [_registered_row()]),
        )

    assert _decisions(logs) == ["refused_no_download_log"]


@pytest.mark.asyncio
async def test_a_session_download_is_refused_when_an_earlier_same_name_download_in_the_block_could_own_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block-start baseline predates both downloads, so the one new row cannot be told apart."""
    run_dir = tmp_path / "downloads" / "wr_session"
    page = _FakePage()
    attach, started = _session_attach(page, run_dir, _registered_downloads(monkeypatch, [_registered_row()]))
    started(_remote_download())
    await asyncio.sleep(0.01)

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, _remote_download(), "#file")

    assert page.attached == []
    assert _decisions(logs) == ["refused_same_name_download_in_block"]


@pytest.mark.asyncio
async def test_the_same_session_download_attached_twice_is_fetched_and_copied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block can attach one download to several inputs, and each registered-artifact capture writes a private
    copy, so repeating the attach must reuse the first capture rather than fill the run directory."""
    run_dir = tmp_path / "downloads" / "wr_session"
    registered = _registered_downloads(monkeypatch, [_registered_row()])
    page = _FakePage()
    attach, _ = _session_attach(page, run_dir, registered)
    download = _remote_download()

    first = await attach(page, download, "#file")
    second = await attach(page, download, "#second-file")

    assert first == second == {"filename": "certificate.pdf", "size": len(_SESSION_BYTES)}
    assert [selector for selector, _ in page.attached] == ["#file", "#second-file"]
    assert [files["buffer"] for _, files in page.attached] == [_SESSION_BYTES, _SESSION_BYTES]
    assert len(list(run_dir.glob(".authorized-*"))) == 1
    app.STORAGE.retrieve_artifact.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_session_download_whose_name_really_contains_an_escape_matches_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session directory keeps the escaping the site sent, so the row is spelled literally; decoding the
    claim's name before matching would miss it and refuse a download that did arrive."""
    registered = _registered_downloads(monkeypatch, [_registered_row("report%20final.pdf")])
    page = _FakePage()
    attach, _ = _session_attach(page, tmp_path / "downloads" / "wr_session", registered)
    claimed = make_claimed_download_mock(
        path=None, suggested_filename="report%20final.pdf", path_error=RuntimeError("remote")
    )

    with capture_logs() as logs:
        await attach(page, claimed, "#file")

    assert _decisions(logs) == ["authorized_registered_artifact"]
    assert [files["buffer"] for _, files in page.attached] == [_SESSION_BYTES]


@pytest.mark.asyncio
async def test_an_extensionless_claim_shares_the_name_of_a_sibling_that_carries_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name can select a row that carries a monitor-derived extension, so a sibling spelled with one is
    a collision too: the claim refuses rather than upload that sibling's bytes under its own name."""
    registered = _registered_downloads(monkeypatch, [_registered_row("2026.csv", b"the sibling's rows")])
    page = _FakePage()
    attach, started = _session_attach(page, tmp_path / "downloads" / "wr_session", registered)
    started(make_claimed_download_mock(path=None, suggested_filename="2026.csv", path_error=RuntimeError("remote")))
    await asyncio.sleep(0.01)
    claimed = make_claimed_download_mock(path=None, suggested_filename="2026", path_error=RuntimeError("remote"))

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, claimed, "#file")

    assert page.attached == []
    assert _decisions(logs) == ["refused_same_name_download_in_block"]


@pytest.mark.asyncio
async def test_a_download_registered_under_a_monitor_derived_extension_still_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site can send a name with no extension; the monitor adds one from the response type before it
    registers, so matching the bare name alone would refuse a download that did arrive."""
    registered = _registered_downloads(monkeypatch, [_registered_row("2026.pdf")])
    page = _FakePage()
    attach, _ = _session_attach(page, tmp_path / "downloads" / "wr_session", registered)
    claimed = make_claimed_download_mock(path=None, suggested_filename="2026", path_error=RuntimeError("remote"))

    with capture_logs() as logs:
        await attach(page, claimed, "#file")

    assert _decisions(logs) == ["authorized_registered_artifact"]
    assert [files["buffer"] for _, files in page.attached] == [_SESSION_BYTES]


@pytest.mark.asyncio
async def test_a_refused_claim_leaves_no_private_copy_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes are fetched and copied before the last checks run, so a claim refused there must discard its
    copy: the block can lose that race again under a name that never repeats."""
    run_dir = tmp_path / "downloads" / "wr_session"
    registered = _registered_downloads(monkeypatch, [], [_registered_row()])
    page = _FakePage()
    read = registered.probe
    sibling: list[Download] = []

    async def start_sibling_then_read() -> tuple[list[FileInfo] | None, set[str]]:
        rows = await read()
        if not sibling:
            sibling.append(
                started(
                    make_claimed_download_mock(
                        path=None, suggested_filename="certificate (1).pdf", path_error=RuntimeError("remote")
                    )
                )
            )
        return rows

    attach, started = _session_attach(page, run_dir, replace(registered, probe=start_sibling_then_read))

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, _remote_download(), "#file")

    assert _decisions(logs) == ["refused_same_name_download_during_capture"]
    assert list(run_dir.glob(".authorized-*")) == []


@pytest.mark.asyncio
async def test_a_percent_escaped_sibling_shares_the_name_the_monitor_saves_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monitor percent-decodes before saving, so "report%20final.pdf" and "report final.pdf" land on one
    file. The claim cannot tell whose row it found, so it refuses rather than upload the sibling's bytes."""
    saved_name = "report final.pdf"
    registered = _registered_downloads(monkeypatch, [_registered_row(saved_name)])
    page = _FakePage()
    attach, started = _session_attach(page, tmp_path / "downloads" / "wr_session", registered)
    started(
        make_claimed_download_mock(
            path=None, suggested_filename="report%20final.pdf", path_error=RuntimeError("remote")
        )
    )
    await asyncio.sleep(0.01)
    claimed = make_claimed_download_mock(path=None, suggested_filename=saved_name, path_error=RuntimeError("remote"))

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, claimed, "#file")

    assert page.attached == []
    assert _decisions(logs) == ["refused_same_name_download_in_block"]


@pytest.mark.asyncio
async def test_a_collision_equivalent_download_started_while_polling_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll waits up to 30s, and a row is matched by collision-equivalent name while supersession compares
    names exactly, so a sibling that starts mid-poll could own the row this claim takes."""
    registered = _registered_downloads(monkeypatch, [], [_registered_row()])
    page = _FakePage()
    read = registered.probe
    sibling: list[Download] = []

    async def start_sibling_then_read() -> tuple[list[FileInfo] | None, set[str]]:
        rows = await read()
        if not sibling:
            sibling.append(
                started(
                    make_claimed_download_mock(
                        path=None, suggested_filename="certificate (1).pdf", path_error=RuntimeError("remote")
                    )
                )
            )
        return rows

    attach, started = _session_attach(
        page, tmp_path / "downloads" / "wr_session", replace(registered, probe=start_sibling_then_read)
    )

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await attach(page, _remote_download(), "#file")

    assert page.attached == []
    assert _decisions(logs) == ["refused_same_name_download_during_capture"]


@pytest.mark.asyncio
async def test_a_superseded_session_download_is_refused_even_with_a_registered_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, started = _browser_download_log()
    first = started(_remote_download())
    started(_remote_download())

    with capture_logs() as logs, pytest.raises(AuthorizedFileAccessError, match="could not be materialized"):
        await capture_claimed_download(
            first,
            download_root=tmp_path / "downloads",
            download_run_id="wr_session",
            organization_id="org_authorized",
            max_bytes=1024,
            inherited_files=frozenset(),
            download_log=log,
            registered=_registered_downloads(monkeypatch, [_registered_row()]),
        )

    assert _decisions(logs) == ["refused_superseded_same_name_download"]


@pytest.mark.asyncio
async def test_a_registered_copy_changed_after_capture_is_refused_at_redemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "downloads" / "wr_session"
    log, started = _browser_download_log()

    materialized = await capture_claimed_download(
        started(_remote_download()),
        download_root=run_dir.parent,
        download_run_id="wr_session",
        organization_id="org_authorized",
        max_bytes=1024,
        inherited_files=frozenset(),
        download_log=log,
        registered=_registered_downloads(monkeypatch, [_registered_row()]),
    )
    (private_copy,) = run_dir.glob(".authorized-*/*")
    private_copy.write_bytes(b"swapped after capture")

    with pytest.raises(AuthorizedFileAccessError, match="unavailable or changed"):
        read_authorized_file(
            materialized,
            download_root=run_dir.parent,
            workflow_run_id="wr_session",
            organization_id="org_authorized",
            max_bytes=1024,
        )


@pytest.mark.asyncio
async def test_only_a_signed_remote_binding_snapshots_the_registered_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run-scoped binding resolves from its own directory, and an unsigned listing has no artifact ids to
    resolve, so neither pays for a registration read."""
    probe = AsyncMock(return_value=([_registered_row()], set()))
    local_page = SimpleNamespace(context=SimpleNamespace())

    async def source(binding: DownloadBinding) -> RegisteredDownloadSource | None:
        return await _registered_download_source(
            probe, local_page, download_binding=binding, organization_id="org_1", download_run_id="wr_1"
        )

    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None)
    assert await source(DownloadBinding.SESSION_DIR) is None
    monkeypatch.setattr(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", "k1:secret")
    assert await source(DownloadBinding.RUN_DIR) is None
    session_source = await source(DownloadBinding.SESSION_DIR)

    assert session_source is not None
    assert session_source.baseline == {
        ("certificate.pdf", hashlib.sha256(_SESSION_BYTES).hexdigest()): "certificate.pdf"
    }
    assert probe.await_count == 1


def _bind_chooser_attach(
    raw_page: FakeChooserPage,
    path: Path,
    materialized: MaterializedAuthorizedFile,
    *,
    workflow_run_id: str = "wr_authorized",
    organization_id: str = "org_authorized",
    max_bytes: int = 1024,
    deadline: float | None = None,
) -> tuple[RecordingPage, InlineAttach]:
    recording_page = RecordingPage(raw_page)  # type: ignore[arg-type]
    attach = bind_inline_attach_authorized_file(
        recording_page,  # type: ignore[arg-type]
        {"resume": materialized, "cover_letter": AuthorizedFileMaterializationFailure()},
        {"resume": str(path), "cover_letter": "https://invalid.example/cover.pdf"},
        download_root=path.parents[1],
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        max_bytes=max_bytes,
        locate=recording_page._pinned_locator(),
        file_chooser=pin_file_chooser(raw_page),
        deadline=deadline,
    )
    return recording_page, attach


def _recorded_types(recording_page: RecordingPage) -> list[ActionType]:
    return [action.action_type for action in recording_page.recorded_actions()]


@pytest.mark.asyncio
async def test_chooser_mode_clicks_the_trigger_once_and_hands_the_verified_bytes_to_the_chooser(
    authorized_file,
) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage()
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    receipt = await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert receipt == {"filename": "resume.pdf", "size": len(b"retained authorized bytes")}
    assert raw_page.clicks == [("#choose-file", True), ("#choose-file", False)]
    assert [files["buffer"] for files in raw_page.chosen] == [b"retained authorized bytes"]
    assert raw_page.input_files == []
    # The trial click goes through the unrecorded raw locator, so the timeline shows the one real click.
    assert _recorded_types(recording_page) == [ActionType.CLICK]
    assert raw_page.live_listeners == 0


@pytest.mark.asyncio
async def test_a_chooser_the_page_opens_before_the_real_click_never_receives_the_bytes(authorized_file) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage(trial_opens_chooser=True)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert raw_page.stray_chosen == []
    assert [files["buffer"] for files in raw_page.chosen] == [b"retained authorized bytes"]
    assert raw_page.dispatched_clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_options", "phase", "trigger_state", "dispatched", "recorded"),
    [
        ({"trial_error": PlaywrightError("strict mode violation: 2 elements")}, "trigger", "not_attempted", 0, []),
        ({"click_error": PlaywrightError("element was detached")}, "trigger", "unknown", 1, [ActionType.CLICK]),
        ({"opens_chooser": False}, "chooser", "confirmed", 1, [ActionType.CLICK]),
        ({"set_files_error": PlaywrightError("chooser closed")}, "attachment", "confirmed", 1, [ActionType.CLICK]),
    ],
    ids=["trial-fails", "click-raises", "no-chooser", "set-files-fails"],
)
async def test_a_chooser_failure_names_its_phase_and_never_clicks_twice(
    authorized_file,
    monkeypatch: pytest.MonkeyPatch,
    page_options: dict[str, Exception | bool],
    phase: str,
    trigger_state: str,
    dispatched: int,
    recorded: list[ActionType],
) -> None:
    monkeypatch.setattr(code_block_authorized_files, "_CHOOSER_WAIT_SECONDS", 0.2)
    path, materialized = authorized_file
    raw_page = FakeChooserPage(**page_options)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    started = time.monotonic()
    with pytest.raises(AuthorizedFileChooserError) as failure:
        await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert time.monotonic() - started < 2
    assert (failure.value.phase, failure.value.trigger_state) == (phase, trigger_state)
    assert failure.value.__cause__ is None
    assert "retained authorized bytes" not in str(failure.value)
    assert raw_page.dispatched_clicks == dispatched
    assert _recorded_types(recording_page) == recorded
    assert raw_page.chosen == [] and raw_page.input_files == []
    assert raw_page.live_listeners == 0


@pytest.mark.asyncio
async def test_a_timed_out_chooser_leaves_nothing_for_the_next_attach_to_collect(
    authorized_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_block_authorized_files, "_CHOOSER_WAIT_SECONDS", 0.2)
    path, materialized = authorized_file
    raw_page = FakeChooserPage(opens_chooser=False)
    first_page, first_attach = _bind_chooser_attach(raw_page, path, materialized)
    with pytest.raises(AuthorizedFileChooserError):
        await first_attach(first_page, str(path), "#choose-file", mode="file_chooser")
    assert raw_page.live_listeners == 0

    raw_page.opens_chooser = True
    second_page, second_attach = _bind_chooser_attach(raw_page, path, materialized)
    await second_attach(second_page, str(path), "#choose-file", mode="file_chooser")

    assert len(raw_page.chosen) == 1
    assert raw_page.dispatched_clicks == 2
    assert raw_page.live_listeners == 0


@pytest.mark.asyncio
async def test_cancelling_a_chooser_attach_propagates_and_removes_its_listener(authorized_file) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage(click_blocks=True)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    task = asyncio.create_task(attach(recording_page, str(path), "#choose-file", mode="file_chooser"))
    while raw_page.dispatched_clicks == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raw_page.live_listeners == 0
    assert raw_page.dispatched_clicks == 1
    assert raw_page.chosen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("set_files_stalls", "phase", "trigger_state", "set_files_calls"),
    [(False, "trigger", "unknown", 0), (True, "attachment", "confirmed", 1)],
    ids=["click", "set-files"],
)
async def test_a_chooser_call_cut_off_by_the_bound_stops_in_the_driver_too(
    authorized_file,
    monkeypatch: pytest.MonkeyPatch,
    set_files_stalls: bool,
    phase: str,
    trigger_state: str,
    set_files_calls: int,
) -> None:
    """A call whose caller is cancelled keeps running, so only its own timeout keeps it inside the bound."""
    monkeypatch.setattr(code_block_authorized_files, "_CHOOSER_WAIT_SECONDS", 0.2)
    path, materialized = authorized_file
    raw_page = FakeChooserPage(click_outlives_cancel=not set_files_stalls, set_files_outlives_cancel=set_files_stalls)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    with pytest.raises(AuthorizedFileChooserError) as failure:
        await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert (failure.value.phase, failure.value.trigger_state) == (phase, trigger_state)
    assert [trial for _, trial in raw_page.clicks] == [True, False]
    assert len(raw_page.set_files_timeouts) == set_files_calls
    timeouts = raw_page.click_timeouts + raw_page.set_files_timeouts
    assert all(timeout is not None and 1 <= timeout <= 200 for timeout in timeouts)
    _, still_running = await asyncio.wait(raw_page.driver_calls, timeout=1)
    assert still_running == set()


@pytest.mark.asyncio
async def test_a_chooser_attach_leaves_an_agent_upload_listener_on_another_page_alone(authorized_file) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage()
    other_page = FakeChooserPage()
    other_chosen: list[FileChooserHandle] = []
    other_handler = other_chosen.append
    other_page.on("filechooser", other_handler)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    with skyvern_context.scoped(SkyvernContext()) as context:
        pending = PendingFileChooserListener(
            page=other_page,  # type: ignore[arg-type]
            file_paths=["/tmp/agent-upload.pdf"],
            handler=other_handler,
        )
        context.pending_file_chooser = pending
        await attach(recording_page, str(path), "#choose-file", mode="file_chooser")
        assert context.pending_file_chooser is pending
        assert other_page.listeners["filechooser"] == [other_handler]

    assert [files["buffer"] for files in raw_page.chosen] == [b"retained authorized bytes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file", "selector", "mode", "bind_overrides", "tamper"),
    [
        ("resume", "#choose-file", "chooser", {}, False),
        ("resume", "#choose-file", "File_Chooser", {}, False),
        ("raw-path", "#choose-file", "file_chooser", {}, False),
        ("https://invalid.example/cover.pdf", "#choose-file", "file_chooser", {}, False),
        ("resume", "", "file_chooser", {}, False),
        ("resume", "#choose-file", "file_chooser", {"workflow_run_id": "wr_other"}, False),
        ("resume", "#choose-file", "file_chooser", {"organization_id": "org_other"}, False),
        ("resume", "#choose-file", "file_chooser", {"max_bytes": 4}, False),
        ("resume", "#choose-file", "file_chooser", {}, True),
    ],
    ids=[
        "unknown-mode",
        "mode-is-exact",
        "raw-path",
        "unmaterialized",
        "empty-selector",
        "wrong-run",
        "wrong-org",
        "oversized",
        "tampered",
    ],
)
async def test_a_refused_chooser_attach_never_clicks_or_hands_over_bytes(
    authorized_file,
    tmp_path: Path,
    file: str,
    selector: str,
    mode: str,
    bind_overrides: dict[str, str | int],
    tamper: bool,
) -> None:
    path, materialized = authorized_file
    if tamper:
        path.write_bytes(b"x" * materialized.size)
    raw_path = tmp_path / "guessed.pdf"
    raw_path.write_bytes(b"not authorized")
    target = {"resume": str(path), "raw-path": str(raw_path)}.get(file, file)
    raw_page = FakeChooserPage()
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized, **bind_overrides)

    with pytest.raises(AuthorizedFileAccessError) as refusal:
        await attach(recording_page, target, selector, mode=mode)

    assert "retained authorized bytes" not in str(refusal.value)
    assert raw_page.clicks == []
    assert raw_page.chosen == [] and raw_page.input_files == []
    assert raw_page.live_listeners == 0
    assert _recorded_types(recording_page) == []


@pytest.mark.asyncio
async def test_a_chooser_attach_on_another_page_is_refused_before_any_click(authorized_file) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage()
    _, attach = _bind_chooser_attach(raw_page, path, materialized)

    with pytest.raises(AuthorizedFileAccessError, match="current CodeBlock page"):
        await attach(RecordingPage(FakeChooserPage()), str(path), "#choose-file", mode="file_chooser")  # type: ignore[arg-type]

    assert raw_page.clicks == []


@pytest.mark.asyncio
async def test_a_chooser_attach_clears_an_agent_upload_listener_left_on_its_page(authorized_file) -> None:
    """An agent UPLOAD_FILE whose click opened no chooser leaves its listener armed; it must not fill this chooser."""
    path, materialized = authorized_file
    raw_page = FakeChooserPage()
    foreign: list[FileChooserHandle] = []
    foreign_handler = foreign.append
    raw_page.on("filechooser", foreign_handler)
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    with skyvern_context.scoped(SkyvernContext()) as context:
        context.pending_file_chooser = PendingFileChooserListener(
            page=raw_page,  # type: ignore[arg-type]
            file_paths=["/tmp/agent-upload.pdf"],
            handler=foreign_handler,
        )
        receipt = await attach(recording_page, str(path), "#choose-file", mode="file_chooser")
        assert context.pending_file_chooser is None

    assert foreign == []
    assert receipt == {"filename": "resume.pdf", "size": len(b"retained authorized bytes")}
    assert [files["buffer"] for files in raw_page.chosen] == [b"retained authorized bytes"]
    assert raw_page.live_listeners == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remaining", "phase", "trigger_state", "dispatched"),
    [(1.4, "chooser", "confirmed", 1), (0.5, "trigger", "not_attempted", 0)],
    ids=["bound-clamped-to-the-deadline", "no-time-left-to-click"],
)
async def test_a_chooser_attach_returns_its_typed_failure_before_the_block_deadline(
    authorized_file, remaining: float, phase: str, trigger_state: str, dispatched: int
) -> None:
    path, materialized = authorized_file
    raw_page = FakeChooserPage(opens_chooser=False)
    deadline = time.monotonic() + remaining
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized, deadline=deadline)

    with pytest.raises(AuthorizedFileChooserError) as failure:
        await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert time.monotonic() < deadline
    assert (failure.value.phase, failure.value.trigger_state) == (phase, trigger_state)
    assert raw_page.dispatched_clicks == dispatched
    assert raw_page.live_listeners == 0


def test_only_a_playwright_family_page_offers_chooser_mode() -> None:
    assert authorized_file_attach_modes(FakeChooserPage()) == {"input", "file_chooser"}
    assert authorized_file_attach_modes(FakeRawCDPChooserPage()) == {"input"}
    assert authorized_file_attach_modes(_RawPage()) == {"input"}
    assert pin_file_chooser(FakeRawCDPChooserPage()) is None


@pytest.mark.asyncio
async def test_a_page_without_chooser_mode_reports_it_unavailable_instead_of_filling_an_input(authorized_file) -> None:
    path, materialized = authorized_file
    raw_page = FakeRawCDPChooserPage()
    recording_page, attach = _bind_chooser_attach(raw_page, path, materialized)

    with pytest.raises(AuthorizedFileChooserError) as failure:
        await attach(recording_page, str(path), "#choose-file", mode="file_chooser")

    assert str(failure.value) == AUTHORIZED_FILE_CHOOSER_UNAVAILABLE_ERROR
    assert (failure.value.phase, failure.value.trigger_state) == ("chooser", "not_attempted")
    assert raw_page.clicks == [] and raw_page.input_files == []
    assert await attach(recording_page, str(path), "#direct-file") == {
        "filename": "resume.pdf",
        "size": len(b"retained authorized bytes"),
    }
    assert len(raw_page.input_files) == 1


@pytest.mark.asyncio
async def test_an_inline_chooser_failure_after_the_click_is_not_healable(authorized_file) -> None:
    """The click may have fired, so self-heal must not re-run the block and click the trigger a second time."""
    path, materialized = authorized_file
    raw_page = FakeChooserPage(click_error=PlaywrightError("element was detached"))
    recording_page = RecordingPage(raw_page)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    block = CodeBlock(
        label="chooser_block",
        code='await attach_authorized_file(page, resume, "#choose-file", mode="file_chooser")',
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="chooser_output",
            description="test output",
            output_parameter_id="op_chooser",
            workflow_id="w_test",
            created_at=now,
            modified_at=now,
        ),
    )
    run = block.generate_async_user_function(
        block.code,
        recording_page,  # type: ignore[arg-type]
        {"resume": str(path)},
        workflow_run_id="wr_authorized",
        organization_id="org_authorized",
        authorized_file_materializations={"resume": materialized},
    )

    with pytest.raises(AuthorizedFileChooserError) as failure:
        await run()

    assert (failure.value.phase, failure.value.trigger_state) == ("trigger", "unknown")
    assert block._is_healable_page_failure(failure.value, recording_page) is False
    assert raw_page.dispatched_clicks == 1
    assert _recorded_types(recording_page) == [ActionType.CLICK]
