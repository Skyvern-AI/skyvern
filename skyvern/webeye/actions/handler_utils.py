from typing import Any

import structlog

from skyvern.forge.sdk.api.files import download_file

LOG = structlog.get_logger()


async def download_file_safe(file_url: str, action: dict[str, Any] | None = None) -> str | list[str]:
    try:
        return await download_file(file_url)
    except Exception:
        LOG.exception(
            "Failed to download file, continuing without it",
            action=action,
            file_url=file_url,
        )
        return []
