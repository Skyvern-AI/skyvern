from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from skyvern.cli.core.trajectory_store import get_trajectory

from ._common import make_result
from ._session import active_api_key_hash


async def skyvern_trajectory_get(
    session_id: Annotated[str, Field(description="Browser session ID (pbs_...) whose captured trajectory to return")],
) -> dict[str, Any]:
    """Return trajectory_json, entry_count, truncated, and capture_status for a browser session.

    Pass trajectory_json directly to skyvern_code_block_synthesize. Only deterministic skyvern_click,
    skyvern_type, skyvern_select_option, and skyvern_press_key actions are captured, so entry_count reflects
    just those; retrieve before closing the session (close deletes the trajectory). A not_found capture_status
    means nothing was captured, or the trajectory expired, was evicted, or lives in another server process.
    Capture is per server process, so hosted multi-replica results may be partial.
    """
    entries, truncated, found = get_trajectory(api_key_hash=active_api_key_hash(), session_id=session_id)
    return make_result(
        "skyvern_trajectory_get",
        data={
            "trajectory_json": json.dumps(entries),
            "entry_count": len(entries),
            "truncated": truncated,
            "capture_status": "found" if found else "not_found",
        },
    )


__all__ = ["skyvern_trajectory_get"]
