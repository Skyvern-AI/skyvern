from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote

SCRIPT_FILE_PATH_ERROR = (
    "Script file path must be a relative POSIX path without empty, '.', '..', absolute, "
    "drive-qualified, backslash, or null-byte segments"
)
_MAX_SCRIPT_FILE_PATH_DECODE_ITERATIONS = 16


def normalize_script_file_path(file_path: str) -> str:
    normalized = _decode_storage_path(file_path)
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith(("/", "\\"))
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).drive
    ):
        raise ValueError(SCRIPT_FILE_PATH_ERROR)

    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(SCRIPT_FILE_PATH_ERROR)
    return "/".join(parts)


def build_script_file_storage_uri(
    storage_base_uri: str,
    *,
    script_id: str,
    script_version: int,
    file_path: str,
) -> str:
    normalized_file_path = normalize_script_file_path(file_path)
    return f"{storage_base_uri.rstrip('/')}/scripts/{script_id}/{script_version}/{normalized_file_path}"


def _decode_storage_path(file_path: str) -> str:
    decoded = file_path
    for _ in range(_MAX_SCRIPT_FILE_PATH_DECODE_ITERATIONS):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            return decoded
        decoded = next_decoded
    if unquote(decoded) != decoded:
        raise ValueError(SCRIPT_FILE_PATH_ERROR)
    return decoded
