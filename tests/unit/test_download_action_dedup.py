"""Tests for the download action file dedup/cleanup logic in handler.py.

These tests exercise the dedup block inline in handle_action by creating
real files in tmp_path and calling the same logic the handler uses:
re-scan → remove 0-byte → checksum dedup.
"""

import os
from pathlib import Path

from skyvern.forge.sdk.api.files import calculate_sha256_for_file, list_files_in_directory


def _run_dedup(download_dir: Path, list_files_before: list[str]) -> list[str]:
    """Reproduce the dedup block from handle_action (post-rescan version)."""
    list_files_after = list_files_in_directory(download_dir)
    new_file_paths = set(list_files_after) - set(list_files_before)
    seen_checksums: dict[str, str] = {}
    deduplicated_paths: list[str] = []
    for fp in sorted(new_file_paths):
        if not os.path.isfile(fp):
            deduplicated_paths.append(fp)
            continue
        if os.path.getsize(fp) == 0:
            os.remove(fp)
            continue
        checksum = calculate_sha256_for_file(fp)
        if checksum in seen_checksums:
            os.remove(fp)
        else:
            seen_checksums[checksum] = fp
            deduplicated_paths.append(fp)
    return [os.path.basename(fp) for fp in deduplicated_paths]


class TestRescanCatchesLateFiles:
    def test_file_appearing_after_initial_snapshot(self, tmp_path: Path) -> None:
        """A file that appears after the polling-loop snapshot should be
        caught by re-scanning the directory."""
        before: list[str] = []

        # Simulate: polling loop sees file_a and breaks
        (tmp_path / "file_a.pdf").write_bytes(b"content_a")
        stale_snapshot = list_files_in_directory(tmp_path)
        assert len(stale_snapshot) == 1

        # Simulate: browser-native download completes after the break
        (tmp_path / "file_b.pdf").write_bytes(b"content_b")

        # Re-scan picks up both
        result = _run_dedup(tmp_path, before)
        assert sorted(result) == ["file_a.pdf", "file_b.pdf"]

    def test_late_duplicate_removed_by_rescan(self, tmp_path: Path) -> None:
        """A late-arriving file with identical content should be deduped."""
        before: list[str] = []
        content = b"same pdf content"

        (tmp_path / "xhr_capture.pdf").write_bytes(content)
        # Browser-native download arrives later with different name
        (tmp_path / "abc123.pdf").write_bytes(content)

        result = _run_dedup(tmp_path, before)
        assert len(result) == 1
        # Only one file should remain on disk
        remaining = list(tmp_path.iterdir())
        assert len(remaining) == 1


class TestZeroByteFileRemoval:
    def test_zero_byte_file_removed(self, tmp_path: Path) -> None:
        """A 0-byte file should be deleted, not included in results."""
        before: list[str] = []
        (tmp_path / "good.pdf").write_bytes(b"real content")
        (tmp_path / "empty.pdf").write_bytes(b"")

        result = _run_dedup(tmp_path, before)
        assert result == ["good.pdf"]
        assert not (tmp_path / "empty.pdf").exists()

    def test_only_zero_byte_file_yields_empty_result(self, tmp_path: Path) -> None:
        """If the only new file is 0 bytes, result is empty."""
        before: list[str] = []
        (tmp_path / "broken.pdf").write_bytes(b"")

        result = _run_dedup(tmp_path, before)
        assert result == []
        assert not (tmp_path / "broken.pdf").exists()


class TestChecksumDedup:
    def test_identical_files_deduped(self, tmp_path: Path) -> None:
        """Two files with same content: keep first (sorted), remove second."""
        before: list[str] = []
        content = b"identical content"
        (tmp_path / "a_first.pdf").write_bytes(content)
        (tmp_path / "b_second.pdf").write_bytes(content)

        result = _run_dedup(tmp_path, before)
        assert result == ["a_first.pdf"]
        assert (tmp_path / "a_first.pdf").exists()
        assert not (tmp_path / "b_second.pdf").exists()

    def test_different_files_both_kept(self, tmp_path: Path) -> None:
        before: list[str] = []
        (tmp_path / "report_a.pdf").write_bytes(b"content_a")
        (tmp_path / "report_b.pdf").write_bytes(b"content_b")

        result = _run_dedup(tmp_path, before)
        assert sorted(result) == ["report_a.pdf", "report_b.pdf"]


class TestRemoteUriSafety:
    def test_remote_uri_not_hashed_or_deleted(self, tmp_path: Path) -> None:
        """Remote URIs (s3://, azure://) should be passed through without
        hashing or deletion attempts."""
        before: list[str] = []
        (tmp_path / "local.pdf").write_bytes(b"local content")

        # Simulate browser-session files as remote URIs in list_files_after
        remote_uri = "s3://bucket/org/browser_sessions/bs_123/downloads/report.pdf"
        list_files_after = list_files_in_directory(tmp_path) + [remote_uri]

        new_file_paths = set(list_files_after) - set(before)
        deduplicated_paths: list[str] = []
        seen_checksums: dict[str, str] = {}
        for fp in sorted(new_file_paths):
            if not os.path.isfile(fp):
                # Remote URIs pass through here (not a local file)
                deduplicated_paths.append(fp)
                continue
            if os.path.getsize(fp) == 0:
                os.remove(fp)
                continue
            checksum = calculate_sha256_for_file(fp)
            if checksum in seen_checksums:
                os.remove(fp)
            else:
                seen_checksums[checksum] = fp
                deduplicated_paths.append(fp)

        basenames = sorted(os.path.basename(fp) for fp in deduplicated_paths)
        assert basenames == ["local.pdf", "report.pdf"]
        assert (tmp_path / "local.pdf").exists()


class TestPreExistingFilesExcluded:
    def test_files_present_before_action_not_in_result(self, tmp_path: Path) -> None:
        """Files in list_files_before should not appear in new_file_paths."""
        (tmp_path / "old.pdf").write_bytes(b"old")
        before = list_files_in_directory(tmp_path)

        (tmp_path / "new.pdf").write_bytes(b"new")

        result = _run_dedup(tmp_path, before)
        assert result == ["new.pdf"]
        assert (tmp_path / "old.pdf").exists()
