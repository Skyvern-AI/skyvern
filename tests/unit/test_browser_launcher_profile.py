"""Tests for Chrome profile utilities in browser_launcher.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skyvern.cli.core.browser_launcher import (
    _PROFILE_COPY_IGNORE,
    clone_local_chrome_profile,
    get_local_chrome_profile_dir,
    is_chrome_running,
)


class TestGetLocalChromeProfileDir:
    def test_darwin(self) -> None:
        with patch("skyvern.cli.core.browser_launcher.platform.system", return_value="Darwin"):
            result = get_local_chrome_profile_dir()
        assert result == Path.home() / "Library/Application Support/Google/Chrome"

    def test_windows(self) -> None:
        with patch("skyvern.cli.core.browser_launcher.platform.system", return_value="Windows"):
            result = get_local_chrome_profile_dir()
        assert result == Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"

    def test_linux(self) -> None:
        with patch("skyvern.cli.core.browser_launcher.platform.system", return_value="Linux"):
            result = get_local_chrome_profile_dir()
        assert result == Path.home() / ".config" / "google-chrome"


def _create_mock_chrome_dir(base: Path, profile_name: str = "Default") -> Path:
    """Create a minimal mock Chrome user data directory with cache dirs."""
    chrome_dir = base / "chrome_data"
    profile = chrome_dir / profile_name
    profile.mkdir(parents=True)

    # Local State at the chrome data dir level
    (chrome_dir / "Local State").write_text('{"os_crypt": {}}')

    # Lock files
    (chrome_dir / "SingletonLock").write_text("")
    (chrome_dir / "SingletonSocket").write_text("")
    (chrome_dir / "SingletonCookie").write_text("")

    # Profile-level files
    (profile / "Preferences").write_text('{"profile": {}}')
    (profile / "Cookies").write_bytes(b"fake-sqlite-data")

    # Subdirectory
    local_storage = profile / "Local Storage"
    local_storage.mkdir()
    (local_storage / "data.txt").write_text("stored_value")

    # Cache directories (should be skipped in selective mode)
    for cache_name in [
        "Cache",
        "Code Cache",
        "GPUCache",
        "Service Worker",
        "blob_storage",
        "Extensions",
        "IndexedDB",
        "File System",
        "Session Storage",
    ]:
        cache_dir = profile / cache_name
        cache_dir.mkdir()
        (cache_dir / "data").write_bytes(b"cache_data")

    return chrome_dir


def _patch_for_clone(chrome_dir: Path, tmp_path: Path):
    """Return a combined context manager that patches both get_local_chrome_profile_dir and SKYVERN_DATA_DIR."""
    return _MultiPatch(chrome_dir, tmp_path)


class _MultiPatch:
    """Patches get_local_chrome_profile_dir and SKYVERN_DATA_DIR together."""

    def __init__(self, chrome_dir: Path, skyvern_data: Path) -> None:
        self._p1 = patch("skyvern.cli.core.browser_launcher.get_local_chrome_profile_dir", return_value=chrome_dir)
        self._p2 = patch("skyvern.cli.core.browser_launcher.SKYVERN_DATA_DIR", skyvern_data)

    def __enter__(self):
        self._p1.__enter__()
        self._p2.__enter__()
        return self

    def __exit__(self, *args):
        self._p2.__exit__(*args)
        self._p1.__exit__(*args)


class TestCloneLocalChromeProfileSelective:
    """Tests for selective (default) copy mode."""

    def test_copies_profile_and_local_state(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest)

        assert (dest / "Local State").exists()
        assert (dest / "Local State").read_text() == '{"os_crypt": {}}'
        assert (dest / "Default" / "Preferences").exists()
        assert (dest / "Default" / "Cookies").exists()
        assert (dest / "Default" / "Local Storage" / "data.txt").exists()
        assert (dest / "Default" / "Local Storage" / "data.txt").read_text() == "stored_value"

    def test_skips_cache_directories(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest)

        for cache_name in _PROFILE_COPY_IGNORE:
            assert not (dest / "Default" / cache_name).exists(), f"Cache dir {cache_name} should have been skipped"

    def test_clears_lock_files(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest)

        assert not (dest / "SingletonLock").exists()
        assert not (dest / "SingletonSocket").exists()
        assert not (dest / "SingletonCookie").exists()

    def test_removes_existing_dest_before_copy(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"
        dest.mkdir(parents=True)
        (dest / "stale_file.txt").write_text("old")

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest)

        assert not (dest / "stale_file.txt").exists()
        assert (dest / "Default" / "Preferences").exists()

    def test_copies_profile_with_spaces_in_name(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path, profile_name="Profile 1")
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Profile 1", dest)

        assert (dest / "Profile 1" / "Preferences").exists()
        assert (dest / "Profile 1" / "Cookies").exists()
        assert (dest / "Local State").exists()

    def test_works_without_local_state(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        (chrome_dir / "Local State").unlink()
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest)

        assert not (dest / "Local State").exists()
        assert (dest / "Default" / "Preferences").exists()


class TestCloneLocalChromeProfileFull:
    """Tests for full copy mode."""

    def test_copies_full_user_data_dir(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest, full=True)

        assert (dest / "Local State").exists()
        assert (dest / "Default" / "Preferences").exists()
        assert (dest / "Default" / "Cookies").exists()
        assert (dest / "Default" / "Local Storage" / "data.txt").exists()
        assert (dest / "Default" / "Local Storage" / "data.txt").read_text() == "stored_value"

    def test_full_includes_cache_dirs(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest, full=True)

        assert (dest / "Default" / "Cache" / "data").exists()
        assert (dest / "Default" / "Code Cache" / "data").exists()

    def test_full_clears_lock_files(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest, full=True)

        assert not (dest / "SingletonLock").exists()
        assert not (dest / "SingletonSocket").exists()
        assert not (dest / "SingletonCookie").exists()

    def test_full_removes_existing_dest_before_copy(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        dest = tmp_path / "dest_data"
        dest.mkdir(parents=True)
        (dest / "stale_file.txt").write_text("old")

        with _patch_for_clone(chrome_dir, tmp_path):
            clone_local_chrome_profile("Default", dest, full=True)

        assert not (dest / "stale_file.txt").exists()
        assert (dest / "Default" / "Preferences").exists()


class TestCloneLocalChromeProfileErrors:
    def test_raises_when_profile_not_found(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)

        with _patch_for_clone(chrome_dir, tmp_path):
            with pytest.raises(FileNotFoundError, match="NoSuchProfile"):
                clone_local_chrome_profile("NoSuchProfile", tmp_path / "dest")

    def test_lists_available_profiles_in_error(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        (chrome_dir / "Profile 1").mkdir()

        with _patch_for_clone(chrome_dir, tmp_path):
            with pytest.raises(FileNotFoundError, match="Default"):
                clone_local_chrome_profile("BadName", tmp_path / "dest")

    def test_rejects_dest_outside_skyvern_data_dir(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)
        skyvern_dir = tmp_path / "skyvern_data"
        skyvern_dir.mkdir()
        outside_dest = tmp_path / "somewhere_else"

        with _patch_for_clone(chrome_dir, skyvern_dir):
            with pytest.raises(ValueError, match="outside Skyvern's data directory"):
                clone_local_chrome_profile("Default", outside_dest)

    def test_raises_when_chrome_not_installed(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "no_chrome_here"

        with _patch_for_clone(nonexistent, tmp_path):
            with pytest.raises(FileNotFoundError, match="Is Google Chrome installed"):
                clone_local_chrome_profile("Default", tmp_path / "dest")

    def test_rejects_path_traversal_in_profile_name(self, tmp_path: Path) -> None:
        chrome_dir = _create_mock_chrome_dir(tmp_path)

        with _patch_for_clone(chrome_dir, tmp_path):
            with pytest.raises(ValueError, match="resolves outside"):
                clone_local_chrome_profile("../../etc", tmp_path / "dest")


def _make_proc(name: str) -> MagicMock:
    proc = MagicMock()
    proc.info = {"name": name}
    return proc


class TestIsChromeRunning:
    def test_detects_chrome_main_process(self) -> None:
        procs = [_make_proc("Google Chrome")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is True

    def test_detects_lowercase_chrome(self) -> None:
        procs = [_make_proc("chrome")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is True

    def test_detects_google_chrome_stable(self) -> None:
        procs = [_make_proc("google-chrome-stable")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is True

    def test_detects_chromium(self) -> None:
        procs = [_make_proc("chromium")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is True

    def test_ignores_crashpad_handler(self) -> None:
        procs = [_make_proc("chrome_crashpad_handler")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is False

    def test_ignores_chromedriver(self) -> None:
        procs = [_make_proc("chromedriver")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is False

    def test_ignores_chrome_helper(self) -> None:
        procs = [_make_proc("Google Chrome Helper")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is False

    def test_no_false_positive_on_substring(self) -> None:
        """Processes with 'chrome' as a substring should NOT match."""
        procs = [_make_proc("chrome_renderer"), _make_proc("chrome-sandbox")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is False

    def test_returns_false_with_no_processes(self) -> None:
        with patch("psutil.process_iter", return_value=[]):
            assert is_chrome_running() is False

    def test_returns_false_with_unrelated_processes(self) -> None:
        procs = [_make_proc("firefox"), _make_proc("code"), _make_proc("python")]
        with patch("psutil.process_iter", return_value=procs):
            assert is_chrome_running() is False
