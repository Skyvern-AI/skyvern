from __future__ import annotations

import shutil
from pathlib import Path

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
