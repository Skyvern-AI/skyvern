"""Uploads must only ever see a downloaded file's final name (SKY-11849).

The async download listener renames an extensionless download in place moments after
the browser finalizes it (bare GUID -> GUID.pdf). A storage sync that lists the
download dir inside that window would upload and register the bare name, and a later
sync would add the renamed one — the same bytes registered twice. The save loops call
``wait_for_pending_extension_rename`` so the upload waits out the rename and uses the
file's final name.
"""

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyvern.forge.sdk.api.files as files_module
import skyvern.forge.sdk.artifact.storage.base as base_module
import skyvern.forge.sdk.artifact.storage.local as local_module
from skyvern.forge.sdk.api.files import wait_for_pending_extension_rename
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from tests.unit.conftest import fake_download_registration_app
from tests.unit.scoped_asyncio import ScopedAsyncio


@pytest.fixture(autouse=True)
def fast_rename_wait(monkeypatch):
    monkeypatch.setattr(files_module, "PENDING_EXTENSION_RENAME_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(files_module, "PENDING_EXTENSION_RENAME_POLL_SECONDS", 0.02)


@pytest.mark.asyncio
async def test_extensioned_filename_returns_immediately(tmp_path) -> None:
    (tmp_path / "invoice.pdf").write_bytes(b"%PDF-")
    assert await wait_for_pending_extension_rename(str(tmp_path), "invoice.pdf") == "invoice.pdf"


@pytest.mark.asyncio
async def test_waits_for_concurrent_rename_and_returns_final_name(tmp_path) -> None:
    bare = tmp_path / "71ee78b7"
    bare.write_bytes(b"%PDF- content")

    async def rename_later() -> None:
        await asyncio.sleep(0.1)
        bare.rename(tmp_path / "71ee78b7.pdf")

    rename_task = asyncio.create_task(rename_later())
    result = await wait_for_pending_extension_rename(str(tmp_path), "71ee78b7")
    await rename_task

    assert result == "71ee78b7.pdf"


@pytest.mark.asyncio
async def test_returns_original_name_when_no_rename_lands(tmp_path) -> None:
    (tmp_path / "no-extension-ever").write_bytes(b"unsniffable bytes")
    result = await wait_for_pending_extension_rename(str(tmp_path), "no-extension-ever")
    assert result == "no-extension-ever"


@pytest.mark.asyncio
async def test_returns_original_name_when_file_disappears_without_twin(tmp_path) -> None:
    doomed = tmp_path / "ghost"
    doomed.write_bytes(b"bytes")

    async def delete_later() -> None:
        await asyncio.sleep(0.05)
        doomed.unlink()

    delete_task = asyncio.create_task(delete_later())
    result = await wait_for_pending_extension_rename(str(tmp_path), "ghost")
    await delete_task

    assert result == "ghost"


@pytest.mark.asyncio
async def test_ignores_unrelated_files_when_resolving_twin(tmp_path) -> None:
    bare = tmp_path / "abc"
    bare.write_bytes(b"bytes")
    (tmp_path / "abcdef.pdf").write_bytes(b"other file whose name merely starts with abc")

    async def rename_later() -> None:
        await asyncio.sleep(0.1)
        bare.rename(tmp_path / "abc.csv")

    rename_task = asyncio.create_task(rename_later())
    result = await wait_for_pending_extension_rename(str(tmp_path), "abc")
    await rename_task

    assert result == "abc.csv"


@pytest.mark.asyncio
async def test_resolves_twin_on_final_check_even_when_wait_budget_exhausted(tmp_path, monkeypatch) -> None:
    # A rename landing between the last poll and the deadline must still be picked up.
    monkeypatch.setattr(files_module, "PENDING_EXTENSION_RENAME_WAIT_SECONDS", 0.0)
    (tmp_path / "71ee78b7.pdf").write_bytes(b"%PDF- already renamed before the call")

    result = await wait_for_pending_extension_rename(str(tmp_path), "71ee78b7")

    assert result == "71ee78b7.pdf"


@pytest.mark.asyncio
async def test_aged_file_waits_only_the_rest_of_the_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(files_module, "PENDING_EXTENSION_RENAME_WAIT_SECONDS", 4.0)
    bare = tmp_path / "aged"
    bare.write_bytes(b"bytes")
    helper_now = time.time() + 10
    os.utime(bare, (helper_now - 3.5, helper_now - 3.5))
    monkeypatch.setattr(files_module, "time", SimpleNamespace(time=lambda: helper_now, monotonic=time.monotonic))

    started = time.monotonic()
    result = await wait_for_pending_extension_rename(str(tmp_path), "aged")
    elapsed = time.monotonic() - started

    assert result == "aged"
    # 0.5 s of the 4 s window remains; a wait of the full window would take 4 s.
    assert 0.45 <= elapsed < 3.0


@pytest.mark.asyncio
async def test_save_pass_waits_once_for_a_fresh_file_not_once_per_old_extensionless_file(tmp_path, monkeypatch) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    old_names = {f"old-{index}" for index in range(60)}
    for name in old_names:
        (download_dir / name).write_bytes(name.encode())
    fresh = download_dir / "fresh"
    fresh.write_bytes(b"%PDF- fresh")
    # The helper's clock runs 10 s ahead, so every file is old except the one dated to match it.
    helper_now = time.time() + 10
    os.utime(fresh, (helper_now, helper_now))
    monkeypatch.setattr(files_module, "time", SimpleNamespace(time=lambda: helper_now, monotonic=time.monotonic))

    async def rename_fresh_then_sleep(seconds: float) -> None:
        if fresh.exists():
            fresh.rename(download_dir / "fresh.pdf")
        await asyncio.sleep(seconds)

    monkeypatch.setattr(files_module, "asyncio", ScopedAsyncio(sleep=rename_fresh_then_sleep))
    saved: list[str] = []

    async def register(*, filename: str, **_: str | int | None) -> None:
        saved.append(filename)

    monkeypatch.setattr(local_module, "app", fake_download_registration_app(register, []))
    monkeypatch.setattr(local_module, "get_download_dir", lambda *args, **kwargs: str(download_dir))
    monkeypatch.setattr(base_module, "resolve_download_attempt", AsyncMock(return_value=("wr_1", 1, None)))

    started = time.monotonic()
    await LocalStorage(str(tmp_path / "artifacts")).save_downloaded_files(organization_id="o_1", run_id="wr_1")
    elapsed = time.monotonic() - started

    # Waiting one window per old file would take 60 windows.
    assert elapsed < 5 * files_module.PENDING_EXTENSION_RENAME_WAIT_SECONDS
    assert sorted(saved) == sorted(old_names | {"fresh.pdf"})
