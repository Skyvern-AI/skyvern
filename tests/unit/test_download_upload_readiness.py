"""Uploads must only ever see a downloaded file's final name (SKY-11849).

The async download listener renames an extensionless download in place moments after
the browser finalizes it (bare GUID -> GUID.pdf). A storage sync that lists the
download dir inside that window would upload and register the bare name, and a later
sync would add the renamed one — the same bytes registered twice. The save loops call
``wait_for_pending_extension_rename`` so the upload waits out the rename and uses the
file's final name.
"""

import asyncio

import pytest

import skyvern.forge.sdk.api.files as files_module
from skyvern.forge.sdk.api.files import wait_for_pending_extension_rename


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
