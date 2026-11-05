from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from skyvern.forge.sdk.copilot.context import ChatHistoryRow

from .run_execution import parse_run_results_cursor, run_results_cursor

TOOL_NAME = "read_chat_history"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
PAGE_CHAR_BUDGET = 16_000
INVALID_CURSOR_ERROR = "cursor is not one this tool returned for this chat."
EARLIER_UNAVAILABLE_ERROR = "earlier messages unavailable."

TOOL_DESCRIPTION = f"""Return earlier messages of this chat that are older than the messages shown in chat_history.
Each message has message_id, sender (user, ai or product), created_at and text, rendered the way chat_history renders
it. Product rows are turns the product opened for the user, such as a run diagnosis. A question-card answer shows as
a "user question result:" line inside the ai row that asked it. Secret values read as [REDACTED_SECRET],
and a refused turn that held raw credentials reads as one redaction notice. Messages come oldest first. With no
cursor, or with "<oldest_shown_message_id>:0", the page ends just before the oldest message in chat_history; passing
earlier_cursor reads the page before this one, and start_of_history: true means no older message exists. When
this chat's earlier messages cannot be read on this turn, the result is ok: false with an error. A page
holds at most `limit` messages (default {DEFAULT_LIMIT}, at most {MAX_LIMIT}) and {PAGE_CHAR_BUDGET:,} characters.
A message too long for one page has complete: false and a continuation_cursor that reads the rest of it."""


def _serialized_len(page: dict[str, Any]) -> int:
    return len(json.dumps(page, ensure_ascii=False))


def _with_earlier(page: dict[str, Any], rows: Sequence[ChatHistoryRow], first_index: int) -> dict[str, Any]:
    if first_index == 0:
        page["start_of_history"] = True
    else:
        page["earlier_cursor"] = run_results_cursor(rows[first_index].message_id, 0)
    return page


def _clipped_row_page(rows: Sequence[ChatHistoryRow], index: int, offset: int) -> dict[str, Any]:
    row = rows[index]
    remaining = row.text[offset:]
    entry = {**asdict(row), "text": remaining, "offset": offset, "complete": True}
    page = _with_earlier({"ok": True, "messages": [entry]}, rows, index)
    # A one-character remainder cannot be split without a cursor at the row's end, which no read accepts.
    if _serialized_len(page) <= PAGE_CHAR_BUDGET or len(remaining) <= 1:
        return page
    entry["complete"] = False

    def fits(take: int) -> bool:
        entry["text"] = remaining[:take]
        entry["continuation_cursor"] = run_results_cursor(row.message_id, offset + take)
        return _serialized_len(page) <= PAGE_CHAR_BUDGET

    # JSON escaping makes serialized length nonlinear in source chars, so search for the largest fitting take.
    low, high = 1, len(remaining) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    fits(low)
    return page


def read_chat_history(
    rows: Sequence[ChatHistoryRow], cursor: str | None, limit: int, oldest_shown_id: str | None
) -> dict[str, Any]:
    limit = min(max(limit, 1), MAX_LIMIT)
    end = len(rows)
    if oldest_shown_id is not None and cursor == run_results_cursor(oldest_shown_id, 0):
        cursor = None
    if cursor is not None:
        parsed = parse_run_results_cursor(cursor)
        index = next((i for i, row in enumerate(rows) if parsed and row.message_id == parsed[0]), None)
        if parsed is None or index is None or (parsed[1] > 0 and parsed[1] >= len(rows[index].text)):
            return {"ok": False, "error": INVALID_CURSOR_ERROR}
        if parsed[1] > 0:
            return _clipped_row_page(rows, index, parsed[1])
        end = index
    if end == 0:
        return {"ok": True, "messages": [], "start_of_history": True}
    entries: list[dict[str, Any]] = []
    first = end
    while first > 0 and len(entries) < limit:
        candidate = [asdict(rows[first - 1]), *entries]
        if _serialized_len(_with_earlier({"ok": True, "messages": candidate}, rows, first - 1)) > PAGE_CHAR_BUDGET:
            break
        entries = candidate
        first -= 1
    if not entries:
        return _clipped_row_page(rows, end - 1, 0)
    return _with_earlier({"ok": True, "messages": entries}, rows, first)
