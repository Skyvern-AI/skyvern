"""Tests for per-action duplicate file deduplication.

When a single click triggers multiple identical downloads (e.g., due to event
bubbling on pages where both a <tr onclick> and a child <a href> point to the
same URL), the action handler should deduplicate newly downloaded files by
checksum so only one copy is kept.
"""

from __future__ import annotations

import os

from skyvern.forge.sdk.api.files import calculate_sha256_for_file


def _write_file(directory: str, name: str, content: bytes) -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _deduplicate_new_files(new_file_paths: set[str]) -> list[str]:
    """Replicate the dedup logic from ActionHandler._handle_action_for_download."""
    seen_checksums: dict[str, str] = {}
    deduplicated: list[str] = []
    for fp in sorted(new_file_paths):
        if not os.path.isfile(fp):
            deduplicated.append(fp)
            continue
        checksum = calculate_sha256_for_file(fp)
        if checksum in seen_checksums:
            os.remove(fp)
        else:
            seen_checksums[checksum] = fp
            deduplicated.append(fp)
    return deduplicated


def test_duplicate_files_are_deduplicated(tmp_path):
    """Two files with identical content should keep only one."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "report.xlsx", b"identical content")
    b = _write_file(dir_, "report_1.xlsx", b"identical content")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 1
    assert len(os.listdir(dir_)) == 1


def test_duplicate_file_is_removed_from_disk(tmp_path):
    """The duplicate file should be deleted from the local download directory."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "file_a.pdf", b"same bytes")
    b = _write_file(dir_, "file_b.pdf", b"same bytes")

    _deduplicate_new_files({a, b})

    assert len(os.listdir(dir_)) == 1


def test_different_files_are_not_deduplicated(tmp_path):
    """Files with different content should both be kept."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "invoice_jan.xlsx", b"january data")
    b = _write_file(dir_, "invoice_feb.xlsx", b"february data")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 2
    assert len(os.listdir(dir_)) == 2


def test_three_duplicates_keeps_only_one(tmp_path):
    """Three identical files should keep one and delete two."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "doc.pdf", b"triplicate")
    b = _write_file(dir_, "doc_1.pdf", b"triplicate")
    c = _write_file(dir_, "doc_2.pdf", b"triplicate")

    result = _deduplicate_new_files({a, b, c})

    assert len(result) == 1
    assert len(os.listdir(dir_)) == 1


def test_mixed_unique_and_duplicate_files(tmp_path):
    """Mix of unique and duplicate files: only duplicates are removed."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "unique_a.xlsx", b"content A")
    b = _write_file(dir_, "unique_b.xlsx", b"content B")
    c = _write_file(dir_, "duplicate_of_a.xlsx", b"content A")

    result = _deduplicate_new_files({a, b, c})

    assert len(result) == 2
    assert len(os.listdir(dir_)) == 2


def test_original_filename_kept_over_suffixed_duplicate(tmp_path):
    """Sorted order keeps the original name over the _1 suffixed copy."""
    dir_ = str(tmp_path)
    a = _write_file(dir_, "Alignment-Feb-2026.xlsx", b"same content")
    b = _write_file(dir_, "Alignment-Feb-2026_1.xlsx", b"same content")

    result = _deduplicate_new_files({a, b})

    assert len(result) == 1
    kept = os.path.basename(result[0])
    assert kept == "Alignment-Feb-2026.xlsx"
    assert os.listdir(dir_) == ["Alignment-Feb-2026.xlsx"]


def test_remote_uris_are_passed_through(tmp_path):
    """S3/Azure URIs from browser sessions should not be hashed or removed."""
    dir_ = str(tmp_path)
    local = _write_file(dir_, "local.xlsx", b"local content")
    remote = "s3://skyvern-artifacts/v1/production/org/browser_sessions/bs_123/file.xlsx"

    result = _deduplicate_new_files({local, remote})

    assert len(result) == 2
    assert remote in result
    assert local in result
    assert len(os.listdir(dir_)) == 1  # only the local file on disk
