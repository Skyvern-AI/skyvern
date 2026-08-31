import asyncio
from typing import Any


async def get_main_document_loader_id(page: Any) -> str | None:
    """Read Chromium's main-frame loader id using a short-lived CDP session."""
    raw_page = getattr(page, "page", page)
    try:
        session = await raw_page.context.new_cdp_session(raw_page)
    except Exception:
        return None
    loader_id: str | None = None
    send_cancelled: asyncio.CancelledError | None = None
    try:
        tree = await session.send("Page.getFrameTree")
        candidate = tree.get("frameTree", {}).get("frame", {}).get("loaderId")
        loader_id = candidate if isinstance(candidate, str) else None
    except asyncio.CancelledError as exc:
        send_cancelled = exc
    except Exception:
        loader_id = None
    try:
        await session.detach()
    except asyncio.CancelledError:
        raise
    except Exception:
        if send_cancelled is None:
            return None
    if send_cancelled is not None:
        raise send_cancelled
    return loader_id
