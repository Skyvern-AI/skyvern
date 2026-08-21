"""Tool-aware response summaries for network inspection tools.

Inputs to these formatters have already crossed inspection's redaction and
capture-size boundaries. The formatters only select from those sanitized
structures; they never inspect URLs, headers, or body text for new data.
"""

from __future__ import annotations

import json
from typing import Any

from skyvern.cli.mcp_tools.response_distillation import TransformResult, TransformTier, distill_value

_MAX_REPRESENTATIVE_ITEMS = 5
_MAX_VERBATIM_COMPACT_BODY_CHARS = 2_000
_NETWORK_REQUEST_FIELDS = (
    "request_id",
    "url",
    "method",
    "status",
    "resource_type",
    "content_type",
    "timing_ms",
    "response_size",
    "page_url",
    "tab_id",
)


def _passthrough(value: Any, reason: str) -> TransformResult[Any]:
    return TransformResult(
        value=value,
        tier=TransformTier.PASSTHROUGH,
        complete=True,
        fallback_reason=reason,
    )


def _representative_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select a deterministic sample spanning the beginning through the end."""
    if len(items) <= _MAX_REPRESENTATIVE_ITEMS:
        return items

    last = len(items) - 1
    indexes = [(offset * last) // (_MAX_REPRESENTATIVE_ITEMS - 1) for offset in range(_MAX_REPRESENTATIVE_ITEMS)]
    return [items[index] for index in indexes]


def _selected_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: value[field] for field in fields if field in value}


def format_network_requests_response(response: Any) -> TransformResult[Any]:
    """Summarize a network-list response while retaining usable request IDs."""
    if not isinstance(response, dict) or response.get("ok") is not True:
        return _passthrough(response, "network_list_not_successful")
    data = response.get("data")
    if not isinstance(data, dict):
        return _passthrough(response, "network_list_data_missing")
    requests = data.get("requests")
    count = data.get("count")
    if not isinstance(requests, list) or not isinstance(count, int) or isinstance(count, bool):
        return _passthrough(response, "network_list_shape_invalid")
    if count < len(requests) or any(not isinstance(request, dict) for request in requests):
        return _passthrough(response, "network_list_shape_invalid")

    representatives = _representative_items(requests)
    summarized_requests = [_selected_fields(request, _NETWORK_REQUEST_FIELDS) for request in representatives]
    omitted_count = count - len(summarized_requests)
    summarized_data = {
        **data,
        "requests": summarized_requests,
        "count": count,
        "omitted_request_count": omitted_count,
    }
    summarized = {**response, "data": summarized_data}
    content_was_discarded = omitted_count > 0 or any(
        len(summary) != len(original) for summary, original in zip(summarized_requests, representatives, strict=True)
    )
    return TransformResult(
        value=summarized,
        tier=TransformTier.STRUCTURED,
        complete=not content_was_discarded,
        fallback_reason="network_requests_summarized" if content_was_discarded else None,
        protected_paths=(("data", "requests"),),
    )


def format_network_request_detail_response(response: Any) -> TransformResult[Any]:
    """Compact a captured detail body without weakening the central parser's safety rules."""
    if not isinstance(response, dict) or response.get("ok") is not True:
        return _passthrough(response, "network_detail_not_successful")
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
        return _passthrough(response, "network_detail_shape_invalid")

    body = data.get("body")
    if body is None:
        return TransformResult(value=response, tier=TransformTier.STRUCTURED, complete=True)
    if not isinstance(body, str):
        return _passthrough(response, "network_detail_body_invalid")

    compacted_body = distill_value(body)
    if compacted_body.tier is TransformTier.PASSTHROUGH:
        # Preserve malformed, ambiguous, or otherwise unsafe text exactly. Its
        # fallback reason is carried by the central TransformResult boundary.
        return TransformResult(
            value=response,
            tier=TransformTier.PASSTHROUGH,
            complete=True,
            fallback_reason=compacted_body.fallback_reason,
        )

    summarized = {
        **response,
        "data": {
            **data,
            "body": compacted_body.value,
        },
    }
    return TransformResult(
        value=summarized,
        tier=compacted_body.tier,
        complete=compacted_body.complete,
        fallback_reason=compacted_body.fallback_reason,
        protected_paths=(("data", "request"),)
        + (
            (("data", "body"),)
            if len(json.dumps(compacted_body.value, ensure_ascii=False, default=str))
            <= _MAX_VERBATIM_COMPACT_BODY_CHARS
            else ()
        ),
    )


def _format_har_entry(entry: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    request = entry.get("request")
    if isinstance(request, dict):
        if "method" in request:
            summary["method"] = request["method"]
        if "url" in request:
            summary["url"] = request["url"]

    response = entry.get("response")
    if isinstance(response, dict):
        if "status" in response:
            summary["status"] = response["status"]
        content = response.get("content")
        if isinstance(content, dict):
            if "mimeType" in content:
                summary["content_type"] = content["mimeType"]
            if "size" in content:
                summary["response_size"] = content["size"]

    if "time" in entry:
        summary["time"] = entry["time"]
    timings = entry.get("timings")
    if isinstance(timings, dict):
        summary["timings"] = dict(timings)
    return summary


def format_har_response(response: Any) -> TransformResult[Any]:
    """Summarize HAR entries without deriving headers, query values, or bodies."""
    if not isinstance(response, dict) or response.get("ok") is not True:
        return _passthrough(response, "har_not_successful")
    data = response.get("data")
    if not isinstance(data, dict):
        return _passthrough(response, "har_data_missing")
    har = data.get("har")
    entry_count = data.get("entry_count")
    if not isinstance(har, dict) or not isinstance(entry_count, int) or isinstance(entry_count, bool):
        return _passthrough(response, "har_shape_invalid")
    log = har.get("log")
    if not isinstance(log, dict):
        return _passthrough(response, "har_shape_invalid")
    entries = log.get("entries")
    if (
        not isinstance(entries, list)
        or entry_count < len(entries)
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        return _passthrough(response, "har_shape_invalid")

    representatives = _representative_items(entries)
    representative_entries = [_format_har_entry(entry) for entry in representatives]
    omitted_count = entry_count - len(representative_entries)
    har_metadata = {key: log[key] for key in log if key != "entries"}
    # Keep a HAR-shaped log: every non-entries key (version, creator, pages, …)
    # is retained verbatim; only the entries list is summarized down to
    # representative items.
    summarized_log = {**har_metadata, "entries": representative_entries}
    summarized = {
        **response,
        "data": {
            **data,
            "har": {"log": summarized_log},
            "entry_count": entry_count,
            "omitted_entry_count": omitted_count,
        },
    }
    content_was_discarded = bool(entries)
    # The summarized log is already bounded (representative entries only) and
    # HAR-shaped; protect it so downstream generic compaction cannot replace
    # pages/creator/entries with depth summaries and break the standard shape.
    return TransformResult(
        value=summarized,
        tier=TransformTier.STRUCTURED,
        complete=not content_was_discarded,
        fallback_reason="har_entries_summarized" if content_was_discarded else None,
        protected_paths=(("data", "har", "log"),),
    )


__all__ = [
    "format_har_response",
    "format_network_request_detail_response",
    "format_network_requests_response",
]
