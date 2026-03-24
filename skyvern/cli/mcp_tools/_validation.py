from __future__ import annotations

from typing import Any

from ._common import ErrorCode, make_error, make_result


def validate_folder_id(folder_id: str, action: str) -> dict[str, Any] | None:
    if "/" in folder_id or "\\" in folder_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "folder_id must not contain path separators",
                "Provide a valid folder ID (starts with fld_)",
            ),
        )
    if not folder_id.startswith("fld_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid folder_id format: {folder_id!r}",
                "Folder IDs start with fld_. Use skyvern_folder_list to find valid IDs.",
            ),
        )
    return None
