from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api.files import is_remote_url, validate_local_file_path

RUN_ID = "wr_test_run_123"


@pytest.fixture()
def download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary download directory structure mimicking production."""
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir()
    return run_dir


@pytest.fixture()
def sample_file(download_dir: Path) -> Path:
    """Create a sample file inside the download directory."""
    f = download_dir / "report.pdf"
    f.write_text("sample content")
    return f


@pytest.fixture()
def sample_subdir_file(download_dir: Path) -> Path:
    """Create a sample file in a subdirectory of the download directory."""
    subdir = download_dir / "subdir"
    subdir.mkdir()
    f = subdir / "nested.pdf"
    f.write_text("nested content")
    return f


# ---------------------------------------------------------------------------
# validate_local_file_path: happy path
# ---------------------------------------------------------------------------


class TestValidateLocalFilePath:
    def test_accepts_file_within_download_dir(self, sample_file: Path) -> None:
        """A file inside downloads/{run_id}/ should be accepted."""
        result = validate_local_file_path(str(sample_file), RUN_ID)
        assert result == str(sample_file.resolve())

    def test_accepts_file_in_subdirectory(self, sample_subdir_file: Path) -> None:
        """A file in a subdirectory of downloads/{run_id}/ should be accepted."""
        result = validate_local_file_path(str(sample_subdir_file), RUN_ID)
        assert result == str(sample_subdir_file.resolve())

    def test_accepts_download_directory_itself(self, download_dir: Path) -> None:
        """The download directory itself should be accepted (for SKYVERN_DOWNLOAD_DIRECTORY)."""
        result = validate_local_file_path(str(download_dir), RUN_ID)
        assert result == str(download_dir.resolve())

    # ---------------------------------------------------------------------------
    # validate_local_file_path: path traversal attacks
    # ---------------------------------------------------------------------------

    def test_rejects_path_traversal_with_dotdot(self, download_dir: Path) -> None:
        """Path traversal via ../../ should be rejected."""
        malicious = str(download_dir / ".." / ".." / "etc" / "passwd")
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path(malicious, RUN_ID)

    def test_rejects_absolute_path_outside_download_dir(self, download_dir: Path) -> None:
        """Absolute paths to sensitive files should be rejected."""
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path("/var/run/secrets/kubernetes.io/serviceaccount/token", RUN_ID)

    def test_rejects_proc_self_environ(self, download_dir: Path) -> None:
        """/proc/self/environ should be rejected."""
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path("/proc/self/environ", RUN_ID)

    def test_rejects_symlink_escaping_sandbox(self, download_dir: Path) -> None:
        """A symlink inside the download dir pointing outside should be rejected."""
        # Create a symlink inside the sandbox that points to /etc/passwd
        symlink_path = download_dir / "evil_link"
        symlink_path.symlink_to("/etc/passwd")
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path(str(symlink_path), RUN_ID)

    def test_rejects_relative_path_outside_download_dir(self, download_dir: Path) -> None:
        """Relative paths that resolve outside the download dir should be rejected."""
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path("../../../etc/shadow", RUN_ID)

    def test_rejects_empty_path(self, download_dir: Path) -> None:
        """Empty path should be rejected."""
        with pytest.raises(PermissionError, match="path must not be empty"):
            validate_local_file_path("", RUN_ID)

    def test_rejects_none_run_id(self, download_dir: Path) -> None:
        """None run_id should be rejected immediately."""
        with pytest.raises(PermissionError, match="no workflow run ID provided"):
            validate_local_file_path("/some/path", None)

    def test_rejects_path_to_different_run_id(self, download_dir: Path) -> None:
        """A file belonging to a different workflow run should be rejected."""
        other_run_dir = download_dir.parent / "wr_other_run_456"
        other_run_dir.mkdir()
        other_file = other_run_dir / "secret.pdf"
        other_file.write_text("other tenant data")
        with pytest.raises(PermissionError, match="outside the allowed download directory"):
            validate_local_file_path(str(other_file), RUN_ID)

    def test_accepts_absolute_path_within_download_dir(self, sample_file: Path) -> None:
        """Absolute path like /app/downloads/wr_xxx/file.pdf should be accepted."""
        absolute = str(sample_file.resolve())
        result = validate_local_file_path(absolute, RUN_ID)
        assert result == absolute


class TestIsRemoteUrl:
    """Tests for the is_remote_url() helper."""

    def test_http_url(self) -> None:
        assert is_remote_url("http://example.com/file.pdf") is True

    def test_https_url(self) -> None:
        assert is_remote_url("https://example.com/file.pdf") is True

    def test_s3_uri(self) -> None:
        assert is_remote_url("s3://bucket/key/file.pdf") is True

    def test_azure_uri(self) -> None:
        assert is_remote_url("azure://container/blob/file.pdf") is True

    def test_local_path_not_remote(self) -> None:
        assert is_remote_url("/app/downloads/wr_123/file.pdf") is False

    def test_relative_path_not_remote(self) -> None:
        assert is_remote_url("downloads/wr_123/file.pdf") is False

    def test_www_prefix_is_remote(self) -> None:
        """www. is treated as a remote URL for backward compatibility."""
        assert is_remote_url("www.example.com/report.pdf") is True

    def test_empty_string_not_remote(self) -> None:
        assert is_remote_url("") is False
