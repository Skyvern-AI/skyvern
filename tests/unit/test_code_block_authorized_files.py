from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.code_block_authorized_files import (
    AuthorizedFileAccessError,
    AuthorizedFileMaterializationFailure,
    bind_inline_attach_authorized_file,
    capture_authorized_file,
    read_authorized_file,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.actions.action_types import ActionType


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
