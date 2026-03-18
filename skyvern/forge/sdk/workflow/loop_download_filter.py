import urllib.parse
from collections import Counter
from typing import Any, TypeAlias

from skyvern.forge.sdk.schemas.files import FileInfo

DownloadedFileSignature: TypeAlias = tuple[str | None, str | None, str | None]


def _normalize_downloaded_file_signature(value: Any) -> DownloadedFileSignature | None:
    """Returns a valid downloaded file signature tuple or None for invalid values."""
    if not (isinstance(value, (list, tuple)) and len(value) == 3):
        return None

    filename, checksum, url = value
    if not all(part is None or isinstance(part, str) for part in (filename, checksum, url)):
        return None

    return filename, checksum, url


def to_downloaded_file_signature(file_info: FileInfo) -> DownloadedFileSignature:
    """Converts a FileInfo object into a tuple representation suitable for use as a downloaded file signature"""
    parsed = urllib.parse.urlsplit(file_info.url)
    url_without_query = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return file_info.filename, file_info.checksum, url_without_query


def filter_downloaded_files_for_current_iteration(
    downloaded_files: list[FileInfo],
    loop_internal_state: dict[str, Any] | None,
) -> list[FileInfo]:
    """Filters downloaded files excluding previous iteration matches"""
    if not loop_internal_state:
        return downloaded_files

    before_iteration = loop_internal_state.get("downloaded_file_signatures_before_iteration")
    if not isinstance(before_iteration, list):
        return downloaded_files

    # Build counter from only validated previous file signatures.
    normalized_previous_signatures: list[DownloadedFileSignature] = []
    for signature in before_iteration:
        normalized_signature = _normalize_downloaded_file_signature(signature)
        if normalized_signature is not None:
            normalized_previous_signatures.append(normalized_signature)

    previous_file_counter: Counter[DownloadedFileSignature] = Counter(normalized_previous_signatures)

    current_iteration_files: list[FileInfo] = []
    for file_info in downloaded_files:
        signature = to_downloaded_file_signature(file_info)
        if previous_file_counter[signature] > 0:
            previous_file_counter[signature] -= 1
            continue
        current_iteration_files.append(file_info)

    return current_iteration_files


__all__ = [
    "filter_downloaded_files_for_current_iteration",
    "to_downloaded_file_signature",
]
