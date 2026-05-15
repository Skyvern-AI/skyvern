"""Tests for per-action duplicate file deduplication.

When a single click triggers multiple identical downloads (e.g., due to event
bubbling on pages where both a <tr onclick> and a child <a href> point to the
same URL), the action handler should deduplicate newly downloaded files by
checksum so only one copy is kept.
"""

from __future__ import annotations

import os
from pathlib import Path

from skyvern.webeye.actions.handler import _deduplicate_new_downloaded_file_paths


def _write_file(directory: str, name: str, content: bytes) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _deduplicate_new_files(
    new_file_paths: set[str],
    observed_file_paths: set[str] | None = None,
) -> list[str]:
    """Call the action handler helper with a stable test workflow id."""
    return _deduplicate_new_downloaded_file_paths(
        new_file_paths,
        workflow_run_id="wr_test",
        observed_file_paths=observed_file_paths,
    )


def test_duplicate_files_are_deduplicated(tmp_path: Path) -> None:
    """Two files with identical content should keep only one."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "report.xlsx", b"identical content")
    b = _write_file(dir_, "report_1.xlsx", b"identical content")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 1
    assert len(os.listdir(dir_)) == 1


def test_duplicate_file_is_removed_from_disk(tmp_path: Path) -> None:
    """The duplicate file should be deleted from the local download directory."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "file_a.pdf", b"same bytes")
    b = _write_file(dir_, "file_b.pdf", b"same bytes")

    _deduplicate_new_files({a, b})

    assert len(os.listdir(dir_)) == 1


def test_different_files_are_not_deduplicated(tmp_path: Path) -> None:
    """Files with different content should both be kept."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "invoice_jan.xlsx", b"january data")
    b = _write_file(dir_, "invoice_feb.xlsx", b"february data")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 2
    assert len(os.listdir(dir_)) == 2


def test_three_duplicates_keeps_only_one(tmp_path: Path) -> None:
    """Three identical files should keep one and delete two."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "doc.pdf", b"triplicate")
    b = _write_file(dir_, "doc_1.pdf", b"triplicate")
    c = _write_file(dir_, "doc_2.pdf", b"triplicate")

    result = _deduplicate_new_files({a, b, c})

    assert len(result) == 1
    assert len(os.listdir(dir_)) == 1


def test_mixed_unique_and_duplicate_files(tmp_path: Path) -> None:
    """Mix of unique and duplicate files: only duplicates are removed."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "unique_a.xlsx", b"content A")
    b = _write_file(dir_, "unique_b.xlsx", b"content B")
    c = _write_file(dir_, "duplicate_of_a.xlsx", b"content A")

    result = _deduplicate_new_files({a, b, c})

    assert len(result) == 2
    assert len(os.listdir(dir_)) == 2


def test_original_filename_kept_over_suffixed_duplicate(tmp_path: Path) -> None:
    """Sorted order keeps the original name over the _1 suffixed copy."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "Alignment-Feb-2026.xlsx", b"same content")
    b = _write_file(dir_, "Alignment-Feb-2026_1.xlsx", b"same content")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 1
    kept = os.path.basename(result[0])
    assert kept == "Alignment-Feb-2026.xlsx"
    assert os.listdir(dir_) == ["Alignment-Feb-2026.xlsx"]


def test_remote_uris_are_passed_through(tmp_path: Path) -> None:
    """S3/Azure URIs from browser sessions should not be hashed or removed."""
    dir_ = str(tmp_path)
    local = _write_file(dir_, "local.xlsx", b"local content")
    remote = "s3://skyvern-artifacts/v1/production/org/browser_sessions/bs_123/file.xlsx"

    result = _deduplicate_new_files({local, remote})

    assert len(result) == 2
    assert remote in result
    assert local in result
    assert len(os.listdir(dir_)) == 1  # only the local file on disk


def test_zero_byte_duplicate_placeholder_is_removed_from_disk(tmp_path: Path) -> None:
    """A blank duplicate-name placeholder should be deleted."""
    dir_ = str(tmp_path)
    valid = _write_file(dir_, "report.pdf", b"valid content")
    empty = _write_file(dir_, "report_1.pdf", b"")

    result = _deduplicate_new_files({empty, valid})

    assert result == [valid]
    assert os.listdir(dir_) == ["report.pdf"]


def test_zero_byte_placeholder_matches_prior_observed_file(tmp_path: Path) -> None:
    """A new blank duplicate is deleted when the real file predates the action."""
    dir_ = str(tmp_path)
    prior_valid = _write_file(dir_, "report.pdf", b"valid content")
    new_empty = _write_file(dir_, "report_1.pdf", b"")

    result = _deduplicate_new_files(
        {new_empty},
        observed_file_paths={prior_valid, new_empty},
    )

    assert result == []
    assert os.listdir(dir_) == ["report.pdf"]


def test_unsuffixed_zero_byte_file_is_preserved_with_prior_suffixed_file(tmp_path: Path) -> None:
    """A new empty canonical filename is not deleted beside a prior suffixed file."""
    dir_ = str(tmp_path)
    prior_valid = _write_file(dir_, "report_1.pdf", b"valid content")
    new_empty = _write_file(dir_, "report.pdf", b"")

    result = _deduplicate_new_files(
        {new_empty},
        observed_file_paths={prior_valid, new_empty},
    )

    assert result == [new_empty]
    assert sorted(os.listdir(dir_)) == ["report.pdf", "report_1.pdf"]


def test_single_zero_byte_file_is_preserved_without_alternate(tmp_path: Path) -> None:
    """A single empty download remains valid when no better artifact exists."""
    dir_ = str(tmp_path)
    empty = _write_file(dir_, "empty.csv", b"")

    result = _deduplicate_new_files({empty})

    assert result == [empty]
    assert os.listdir(dir_) == ["empty.csv"]


def test_named_zero_byte_file_is_preserved_with_non_empty_artifact(tmp_path: Path) -> None:
    """An intentionally empty export should not be removed beside another file."""
    dir_ = str(tmp_path)
    empty_export = _write_file(dir_, "empty_rows.csv", b"")
    manifest = _write_file(dir_, "manifest.csv", b"columns,row_count")

    result = _deduplicate_new_files({empty_export, manifest})

    assert set(result) == {empty_export, manifest}
    assert sorted(os.listdir(dir_)) == ["empty_rows.csv", "manifest.csv"]
