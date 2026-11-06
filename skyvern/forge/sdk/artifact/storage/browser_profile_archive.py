"""Zip and unzip saved browser profiles, leaving out state Chrome rebuilds on every launch."""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any

import structlog

from skyvern.forge.sdk.api.files import unzip_bytes_to_temp_directory

LOG = structlog.get_logger()

# Chrome indexes each loaded extension's declarativeNetRequest rulesets into <profile dir>/DNR Extension Rules/<id>.
# An unpacked extension gets a new id whenever its load path changes and Chrome never prunes the old ones, so a
# profile that round-trips them grows without bound.
_REGENERATED_DIR_NAME = "DNR Extension Rules"


def is_regenerated_profile_path(relative_path: str) -> bool:
    return _REGENERATED_DIR_NAME in relative_path.replace("\\", "/").split("/")[:2]


def _tree_size(directory: str) -> tuple[int, int]:
    file_count = total_bytes = 0
    for dirpath, _, filenames in os.walk(directory):
        for name in filenames:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            file_count += 1
    return file_count, total_bytes


def write_profile_archive(directory: str, zip_file_path: str, **log_context: Any) -> None:
    archived_files = archived_bytes = excluded_files = excluded_bytes = 0
    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(directory):
            relative_dir = os.path.relpath(dirpath, directory)
            kept_dirnames = []
            for name in sorted(dirnames):
                relative_path = os.path.normpath(os.path.join(relative_dir, name))
                if is_regenerated_profile_path(relative_path):
                    skipped_files, skipped_bytes = _tree_size(os.path.join(dirpath, name))
                    excluded_files += skipped_files
                    excluded_bytes += skipped_bytes
                    continue
                archive.write(os.path.join(dirpath, name), relative_path)
                kept_dirnames.append(name)
            dirnames[:] = kept_dirnames
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.isfile(path):
                    archive.write(path, os.path.normpath(os.path.join(relative_dir, name)))
                    archived_files += 1
                    archived_bytes += archive.filelist[-1].file_size
    LOG.info(
        "Archived browser profile",
        archived_files=archived_files,
        archived_bytes=archived_bytes,
        excluded_files=excluded_files,
        excluded_bytes=excluded_bytes,
        zip_bytes=os.path.getsize(zip_file_path),
        **log_context,
    )


def extract_profile_archive(zip_bytes: bytes, **log_context: Any) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
    skipped = [info for info in files if is_regenerated_profile_path(info.filename)]
    skipped_bytes = sum(info.file_size for info in skipped)
    profile_dir = unzip_bytes_to_temp_directory(
        zip_bytes, prefix="skyvern_browser_profile_", skip_member=is_regenerated_profile_path
    )
    LOG.info(
        "Retrieved browser profile archive",
        zip_bytes=len(zip_bytes),
        extracted_files=len(files) - len(skipped),
        extracted_bytes=sum(info.file_size for info in files) - skipped_bytes,
        skipped_files=len(skipped),
        skipped_bytes=skipped_bytes,
        **log_context,
    )
    return profile_dir
