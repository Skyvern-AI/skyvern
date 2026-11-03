from __future__ import annotations

import shutil
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.webeye.session_cookies import SESSION_COOKIES_FILENAME


def test_copy_directory_best_effort_skips_uncopyable_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "Default").mkdir(parents=True)
    (src / "good.txt").write_text("hello")
    (src / "Default" / "Cookies").write_text("db")
    (src / ".skyvern_session_cookies.json").write_text("[]")
    # Mimics a live Chromium profile: a path that resolves to nothing (e.g. RunningChromeVersion
    # deleted mid-walk) — shutil.copy2 raises FileNotFoundError, which must be skipped, not fatal.
    (src / "RunningChromeVersion").symlink_to(tmp_path / "missing")

    dst = tmp_path / "dst"
    LocalStorage()._copy_directory_best_effort(src, dst)

    assert (dst / "good.txt").read_text() == "hello"
    assert (dst / "Default" / "Cookies").read_text() == "db"
    assert (dst / ".skyvern_session_cookies.json").read_text() == "[]"
    assert not (dst / "RunningChromeVersion").exists()


def test_copy_directory_best_effort_reraises_non_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real failure on a needed file (e.g. ENOSPC/permission on Cookies) must abort the store, not
    # silently produce a partial profile that later gets reused as valid.
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cookies").write_text("auth-db")
    dst = tmp_path / "dst"

    real_copy2 = shutil.copy2

    def fake_copy2(s: str | Path, d: str | Path, *args: object, **kwargs: object) -> str | Path:
        if Path(s).name == "Cookies":
            raise PermissionError("disk full")
        return real_copy2(s, d)

    monkeypatch.setattr(shutil, "copy2", fake_copy2)

    with pytest.raises(PermissionError):
        LocalStorage()._copy_directory_best_effort(src, dst)


def test_drop_stale_session_sidecar_removes_dest_when_absent_in_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / SESSION_COOKIES_FILENAME).write_text("[stale]")

    LocalStorage()._drop_stale_session_sidecar(src, dst)

    assert not (dst / SESSION_COOKIES_FILENAME).exists()


def test_drop_stale_session_sidecar_keeps_dest_when_present_in_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / SESSION_COOKIES_FILENAME).write_text("[fresh]")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / SESSION_COOKIES_FILENAME).write_text("[stale]")

    LocalStorage()._drop_stale_session_sidecar(src, dst)

    assert (dst / SESSION_COOKIES_FILENAME).exists()


@pytest.mark.asyncio
async def test_an_upload_under_a_relative_storage_path_returns_an_absolute_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path.as_uri rejects a relative path, and the bytes are already written by then, so a
    relative ARTIFACT_STORAGE_PATH would fail every upload while leaving an untracked file."""
    import io

    from skyvern.forge.sdk.api.files import parse_uri_to_path

    monkeypatch.chdir(tmp_path)
    storage = LocalStorage(artifact_path="relative-artifacts")

    uri, _ = await storage.save_legacy_file(
        organization_id="o_1", filename="file_1_rows.csv", fileObj=io.BytesIO(b"row_id,url\n")
    )

    assert Path(parse_uri_to_path(uri)).read_bytes() == b"row_id,url\n"
    storage.assert_managed_file_access(uri, "o_1")
    assert storage.manages_local_file_uri(uri, "o_1") is True
    assert storage.manages_local_file_uri(uri, "o_other") is False


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("/srv/artifacts/local/o_1/2026-01-01/file_1_weird#1 q?2.csv"),
        PureWindowsPath("C:/Skyvern/artifacts/local/o_1/2026-01-01/file_1_weird#1 q?2.csv"),
    ],
)
def test_a_managed_file_uri_decodes_back_to_the_same_path(path: PurePath) -> None:
    """The writer and the shared parser must agree on both platforms: Path.as_uri emits
    file:///C:/... on Windows, which parse_uri_to_path would read as /C:/..."""
    from urllib.parse import quote

    from skyvern.forge.sdk.api.files import parse_uri_to_path
    from skyvern.forge.sdk.artifact.storage.local import managed_file_uri

    uri = managed_file_uri(path)

    assert type(path)(parse_uri_to_path(uri)) == path
    # download_file names its temporary copy after the URI's last segment.
    assert uri.split("/")[-1] == quote(path.name)


@pytest.mark.asyncio
async def test_an_upload_near_the_filename_limit_is_stored_under_a_bounded_name(tmp_path: Path) -> None:
    """The upload service prefixes the id, so a client name that fits 255 bytes on its own can
    overflow the filesystem's component limit once stored; the stored name must still fit."""
    import io

    from skyvern.forge.sdk.api.files import parse_uri_to_path

    original = "表" * 78 + ".csv"  # 238 bytes on its own
    prefix = "file_572714440041209402_"
    storage = LocalStorage(artifact_path=str(tmp_path))

    uri, _ = await storage.save_legacy_file(
        organization_id="o_1", filename=prefix + original, fileObj=io.BytesIO(b"row_id,url\n")
    )

    stored = Path(parse_uri_to_path(uri))
    assert len(stored.name.encode()) <= 255
    assert stored.name.startswith(prefix)
    assert stored.name.endswith(".csv")
    assert stored.read_bytes() == b"row_id,url\n"


@pytest.mark.asyncio
async def test_an_upload_name_windows_cannot_store_is_made_storable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A macOS or Linux client can name a file with characters NTFS rejects, which would fail the
    write on a Windows deployment."""
    import io

    from skyvern.forge.sdk.api.files import parse_uri_to_path
    from skyvern.forge.sdk.artifact.storage import local

    monkeypatch.setattr(local, "WINDOWS", True)
    storage = LocalStorage(artifact_path=str(tmp_path))

    uri, _ = await storage.save_legacy_file(
        organization_id="o_1", filename='file_1_q3: "totals"?.csv.', fileObj=io.BytesIO(b"row_id,url\n")
    )

    stored = Path(parse_uri_to_path(uri))
    assert not set('<>:"\\|?*') & set(stored.name)
    assert not stored.name.endswith((".", " "))
    assert stored.name.startswith("file_1_")
    assert stored.read_bytes() == b"row_id,url\n"
