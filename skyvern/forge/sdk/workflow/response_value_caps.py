"""Per-value caps for run-detail API responses.

Zero project-internal imports so both the read path (``workflow/service.py``) and the
jsonb write path (``db/utils.py``) can consume these without creating a cycle.
"""

import json
from collections.abc import Iterable
from typing import Any, overload

import structlog

LOG = structlog.get_logger()

# Per-value cap for run-detail API responses. One block output — a sheet or
# file read — is echoed verbatim into every run-detail read (app run page, MCP get_run,
# run timeline); observed values reach 78MB, which wedges the browser's JSON view and
# overruns MCP's result limits. OUTPUT_PARAMETER_MAX_VALUE_BYTES stays the storage-side
# net; this is the far lower bound a synchronous JSON response can actually carry.
RUN_RESPONSE_MAX_VALUE_BYTES = 2 * 1024 * 1024

# Each level of sibling-preserving trim re-serializes its whole subtree, so unbounded
# recursion costs O(depth x size): a 2.5MB value nested 400 deep serializes 1.27GB and
# blocks the event loop for ~2s. Real block outputs need one or two levels; past this
# the whole value is marked instead of descending further.
RUN_RESPONSE_MAX_TRIM_DEPTH = 4

LOOP_VALUE_TRUNCATED_PLACEHOLDER = "[truncated]"


def _response_value_size_bytes(value: Any, log_context: dict[str, Any]) -> int | None:
    """Serialized size as FastAPI would put it on the wire, or None if it can't be measured."""
    try:
        # Mirrors FastAPI's JSONResponse.render. ensure_ascii=False because the default
        # escapes non-ASCII to \uXXXX (double-counting CJK); the compact separators because
        # the defaults add ", " and ": ", ~12% on a wide row-array.
        return len(json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception:
        LOG.warning("Failed to measure run response value size; passing through", exc_info=True, **log_context)
        return None


# Room reserved so the trailing marker still fits inside the cap alongside kept entries.
_TRUNCATION_MARKER_RESERVE_BYTES = 512

TRUNCATION_MARKER_KEY = "_truncated"


def _truncation_marker(
    size_bytes: int,
    *,
    limit_bytes: int | None = None,
    original_count: int | None = None,
    kept_count: int | None = None,
) -> dict:
    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if limit_bytes is None else limit_bytes
    marker = {
        "truncated": True,
        "reason": "exceeded_max_run_response_value_size",
        "original_size_bytes": size_bytes,
        "limit_bytes": effective_limit,
    }
    if original_count is not None:
        marker["original_count"] = original_count
        marker["kept_count"] = kept_count
    return marker


def _string_wire_size(value: str) -> int:
    """Serialized size of a string including JSON quoting and escapes."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@overload
def truncate_oversized_response_text(current_value: None, *, limit_bytes: int | None = None) -> None: ...


@overload
def truncate_oversized_response_text(current_value: str, *, limit_bytes: int | None = None) -> str: ...


def truncate_oversized_response_text(current_value: str | None, *, limit_bytes: int | None = None) -> str | None:
    """Cap one unbounded text field on a timeline block.

    Prompts, email bodies, rendered branch expressions and ``str(loop_over_value)`` are all
    persisted without a size guard. Stays a string, since every one of these is typed
    ``str | None``, keeping a readable prefix in the same shape as the DecisionBlock cap.
    """
    if current_value is None:
        return current_value
    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if limit_bytes is None else limit_bytes
    if _string_wire_size(current_value) <= effective_limit:
        return current_value
    # JSON escaping inflates quotes, backslashes and control characters — up to 6x for a
    # control character — so a raw byte count understates the serialized size by as much as
    # half. Shrink until the encoded form actually fits rather than trusting len().
    keep = min(len(current_value), effective_limit)
    while keep > 0:
        candidate = f"{current_value[:keep]}...[truncated {len(current_value) - keep} chars]"
        if _string_wire_size(candidate) <= effective_limit:
            return candidate
        keep //= 2
    return "...[truncated]"


def capped_text_list(values: list[str] | None, **log_context: Any) -> list[str] | None:
    """Cap a ``list[str]`` without inserting a JSON marker object into it."""
    if not values:
        return values
    size_bytes = _response_value_size_bytes(values, log_context)
    if size_bytes is None or size_bytes <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return values

    kept: list[str] = []
    running_size = 2
    for value in values:
        values_after_current = len(values) - len(kept) - 1
        if values_after_current == 0:
            item_separator = 1 if kept else 0
            available_for_item = RUN_RESPONSE_MAX_VALUE_BYTES - running_size - item_separator
            capped_value = truncate_oversized_response_text(value, limit_bytes=available_for_item)
            item_size = _string_wire_size(capped_value)
            if running_size + item_separator + item_size <= RUN_RESPONSE_MAX_VALUE_BYTES:
                kept.append(capped_value)
            break

        remaining_count = len(values) - len(kept)
        marker = f"...[truncated {remaining_count} values]"
        marker_size = _string_wire_size(marker)
        item_separator = 1 if kept else 0
        available_for_item = RUN_RESPONSE_MAX_VALUE_BYTES - running_size - item_separator - marker_size - 1
        if available_for_item <= _string_wire_size("...[truncated]"):
            break
        capped_value = truncate_oversized_response_text(value, limit_bytes=available_for_item)
        item_size = _string_wire_size(capped_value)
        if running_size + item_separator + item_size + 1 + marker_size > RUN_RESPONSE_MAX_VALUE_BYTES:
            break
        kept.append(capped_value)
        running_size += item_separator + item_size

    if len(kept) == len(values):
        return kept

    marker = f"...[truncated {len(values) - len(kept)} values]"
    LOG.warning(
        "Trimming oversized run response string list",
        original_size_bytes=size_bytes,
        kept_count=len(kept),
        original_count=len(values),
        **log_context,
    )
    return [*kept, marker]


def capped_loop_values(loop_values: list[Any] | None, **log_context: Any) -> list[Any] | None:
    """Cap ``loop_values`` without changing how many iterations it reports.

    The run-detail page reads the iteration count off ``loop_values.length`` and resolves a
    selected iteration by index, so collapsing the list would render a finished 200-iteration
    loop as 200/1 and strand every selection past the first.
    """
    if not loop_values:
        return loop_values

    capped = truncate_oversized_response_value(loop_values, field="loop_values", **log_context)
    # A same-length list means every iteration survived (some trimmed in place). A shorter
    # one is the partial-plus-marker shape, which would misreport the iteration count here.
    if isinstance(capped, list) and len(capped) == len(loop_values):
        return capped
    # Per-element trimming could not fit either. Repeat a compact placeholder so the count
    # and index lookups still line up; repeating the full marker would re-exceed the cap on
    # a long list. The sizes are on the warning the call above already emitted.
    placeholders = [LOOP_VALUE_TRUNCATED_PLACEHOLDER] * len(loop_values)
    placeholder_size = _response_value_size_bytes(placeholders, log_context)
    if placeholder_size is not None and placeholder_size <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return placeholders
    # Even one placeholder per iteration overruns the cap (~150k entries). The count has to
    # move into bounded metadata rather than stay encoded as list length.
    size_bytes = _response_value_size_bytes(loop_values, log_context) or 0
    return [_truncation_marker(size_bytes, original_count=len(loop_values), kept_count=0)]


def truncate_oversized_response_value(
    value: Any,
    _remaining_trim_depth: int | None = None,
    _loop_payload_budget_granted: bool = False,
    _limit_bytes: int | None = None,
    _known_size_bytes: int | None = None,
    **log_context: Any,
) -> Any:
    """Fail-open cap for one value echoed into a run-detail API response.

    A dict or list keeps every element that fits; only oversized ones are replaced, so
    structure the UI reads survives alongside the one huge field — a block's
    ``downloaded_file_urls``, a trigger block's ``workflow_run_id``, and the per-iteration
    entries of a for-loop's ``list[list[dict]]`` output. A replaced element becomes an
    opaque marker rather than a partial value: a truncated list is indistinguishable from
    a complete one to an SDK caller.

    ``_known_size_bytes`` is this value's already-measured wire size. Every descent measures
    its children before recursing, so without it each subtree is serialized once by the
    parent and again by the child — the dominant cost on a multi-megabyte value.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value

    effective_limit = RUN_RESPONSE_MAX_VALUE_BYTES if _limit_bytes is None else _limit_bytes
    remaining_trim_depth = RUN_RESPONSE_MAX_TRIM_DEPTH if _remaining_trim_depth is None else _remaining_trim_depth
    size_bytes = _known_size_bytes if _known_size_bytes is not None else _response_value_size_bytes(value, log_context)
    if size_bytes is None or size_bytes <= effective_limit:
        return value

    if isinstance(value, (dict, list)) and remaining_trim_depth > 0:
        is_loop_output_envelope = (
            isinstance(value, dict)
            and "output_value" in value
            and ("output_parameter" in value or "loop_value" in value)
        )
        if is_loop_output_envelope and isinstance(value, dict):
            # The transport wrapper is cheap and the UI needs all of it. Reserve its
            # encoded bytes, then trim only output_value to what remains; otherwise a
            # marginally oversized entry can keep loop metadata and drop the payload.
            trimmed_envelope = {
                key: truncate_oversized_response_value(
                    entry,
                    _remaining_trim_depth=remaining_trim_depth - 1,
                    _loop_payload_budget_granted=_loop_payload_budget_granted,
                    _limit_bytes=effective_limit,
                    **log_context,
                )
                for key, entry in value.items()
                if key != "output_value"
            }
            envelope_size = _response_value_size_bytes(trimmed_envelope, log_context)
            output_key_size = _response_value_size_bytes("output_value", log_context)
            if envelope_size is not None and output_key_size is not None:
                output_overhead = (1 if trimmed_envelope else 0) + output_key_size + 1
                output_limit = effective_limit - envelope_size - output_overhead
                if output_limit > 0:
                    grants_loop_payload_budget = not _loop_payload_budget_granted
                    trimmed_output = truncate_oversized_response_value(
                        value["output_value"],
                        _remaining_trim_depth=(
                            RUN_RESPONSE_MAX_TRIM_DEPTH if grants_loop_payload_budget else remaining_trim_depth - 1
                        ),
                        _loop_payload_budget_granted=True,
                        _limit_bytes=output_limit,
                        **log_context,
                    )
                    candidate = {
                        key: trimmed_output if key == "output_value" else trimmed_envelope[key] for key in value
                    }
                    candidate_size = _response_value_size_bytes(candidate, log_context)
                    if candidate_size is not None and candidate_size <= effective_limit:
                        return candidate

        # Reserve the encoded size of every sibling before trimming one child. Without this,
        # a trimmable child can consume nearly the entire parent budget, after which the
        # parent falls back to one bare marker and drops UI metadata such as screenshot URLs.
        # This mirrors the loop-envelope allocation above, but applies to ordinary dicts
        # and lists too. The direct formula is the child size that would make the original
        # collection fit: cap - original collection size + original child size. Attempt it
        # lazily while accumulating: pre-scanning a many-row list would reintroduce the
        # synchronous breadth work this cap is meant to avoid.
        items: Iterable[tuple[Any, Any]]
        if isinstance(value, dict):
            items = value.items()
            is_dict = True
        else:
            items = enumerate(value)
            is_dict = False

        # Accumulate as we go and stop once the kept elements fill the cap. Entries that
        # fit are kept and a trailing marker records what was dropped, so a table 1% over
        # the limit still renders instead of vanishing. The marker is what keeps this from
        # being a silent truncation — a bare prefix would be indistinguishable from the
        # whole value to an SDK caller.
        # Start at the enclosing braces/brackets and charge each entry its separators —
        # and, for a dict, its quoted key. Summing child values alone under-counts a
        # key-heavy dict by roughly half, which would let a payload through at ~2x the cap.
        running_size = 2
        trimmed_pairs: list[tuple[Any, Any]] = []
        dropped_any = False
        # The loop's list/list/entry wrappers are transport structure, not payload depth.
        # Give output_value one fresh, bounded trim budget so nested extracted data and
        # links survive while the one heavy sub-value is replaced. The grant flag prevents
        # nested or model-produced envelopes from resetting the budget repeatedly.
        for key, entry in items:
            raw_entry_size = _response_value_size_bytes(entry, log_context)
            entry_limit = effective_limit - size_bytes + raw_entry_size if raw_entry_size is not None else 0
            grants_loop_payload_budget = (
                is_loop_output_envelope and key == "output_value" and not _loop_payload_budget_granted
            )
            child_remaining_trim_depth = (
                RUN_RESPONSE_MAX_TRIM_DEPTH if grants_loop_payload_budget else remaining_trim_depth - 1
            )
            if raw_entry_size is not None and 0 < entry_limit < raw_entry_size:
                sibling_reserved_entry = truncate_oversized_response_value(
                    entry,
                    _remaining_trim_depth=child_remaining_trim_depth,
                    _loop_payload_budget_granted=_loop_payload_budget_granted or grants_loop_payload_budget,
                    _limit_bytes=entry_limit,
                    _known_size_bytes=raw_entry_size,
                    **log_context,
                )
                sibling_candidate: dict[Any, Any] | list[Any]
                if isinstance(value, dict):
                    sibling_candidate = {**value, key: sibling_reserved_entry}
                else:
                    sibling_candidate = list(value)
                    sibling_candidate[key] = sibling_reserved_entry
                candidate_size = _response_value_size_bytes(sibling_candidate, log_context)
                if candidate_size is not None and candidate_size <= effective_limit:
                    return sibling_candidate
            trimmed_entry = truncate_oversized_response_value(
                entry,
                _remaining_trim_depth=child_remaining_trim_depth,
                _loop_payload_budget_granted=_loop_payload_budget_granted or grants_loop_payload_budget,
                _limit_bytes=effective_limit,
                _known_size_bytes=raw_entry_size,
                **log_context,
            )
            if trimmed_entry is entry:
                entry_size = raw_entry_size
            else:
                entry_size = _response_value_size_bytes(trimmed_entry, log_context)
            if entry_size is None:
                dropped_any = True
                break
            # +1 for the comma; for a dict also the serialized key and its colon.
            running_size += entry_size + 1
            if is_dict:
                key_size = _response_value_size_bytes(key, log_context)
                if key_size is None:
                    dropped_any = True
                    break
                running_size += key_size + 1
            if running_size > effective_limit - _TRUNCATION_MARKER_RESERVE_BYTES:
                dropped_any = True
                break
            trimmed_pairs.append((key, trimmed_entry))
        # `dropped_any`, not the running total: the loop stops a marker's width below the
        # cap, so a size check here would call a partial result complete and drop entries
        # with nothing recording it.
        if not dropped_any:
            if is_dict:
                return dict(trimmed_pairs)
            return [entry for _, entry in trimmed_pairs]
        if trimmed_pairs:
            marker = _truncation_marker(
                size_bytes,
                limit_bytes=effective_limit,
                original_count=len(value),
                kept_count=len(trimmed_pairs),
            )
            LOG.warning(
                "Trimming oversized run response collection",
                original_size_bytes=size_bytes,
                kept_count=len(trimmed_pairs),
                original_count=len(value),
                **log_context,
            )
            if is_dict:
                return {**dict(trimmed_pairs), TRUNCATION_MARKER_KEY: marker}
            return [*(entry for _, entry in trimmed_pairs), marker]
        # Nothing fit at all — fall through to replacing the whole value.

    LOG.warning(
        "Truncating oversized run response value",
        original_size_bytes=size_bytes,
        limit_bytes=effective_limit,
        **log_context,
    )
    return _truncation_marker(size_bytes, limit_bytes=effective_limit)


def collect_extracted_information(value: Any) -> list[Any]:
    """Recursively collect extracted_information values from nested outputs.

    Lives next to the caps because capping destroys it: a loop output whose iterations are
    individually small but collectively over the cap keeps only the iterations that fit, so
    this has to run against the whole value — on the write path when the value is oversized.
    """
    results: list[Any] = []
    if isinstance(value, dict):
        if "extracted_information" in value and value["extracted_information"] is not None:
            extracted = value["extracted_information"]
            if isinstance(extracted, list):
                results.extend(extracted)
            else:
                results.append(extracted)
        else:
            for nested in value.values():
                results.extend(collect_extracted_information(nested))
    elif isinstance(value, list):
        for item in value:
            results.extend(collect_extracted_information(item))
    return results


def capped_extracted_information(value: list[Any], **log_context: Any) -> list[Any] | None:
    """Cap the collected values while retaining a truncation marker if no item fits."""
    size_bytes = _response_value_size_bytes(value, log_context)
    if size_bytes is None:
        return None
    if size_bytes <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return value
    capped = truncate_oversized_response_value(value, _known_size_bytes=size_bytes, **log_context)
    if isinstance(capped, dict):
        return [capped]
    if not isinstance(capped, list):
        return None
    capped_size = _response_value_size_bytes(capped, log_context)
    if capped_size is None or capped_size > RUN_RESPONSE_MAX_VALUE_BYTES:
        return None
    return capped


def precomputed_capped_value(value: Any, **log_context: Any) -> Any | None:
    """Build the capped copy a ``cap_output_values`` read would compute, at write time.

    Returns a strictly smaller capped copy when one exists; otherwise the read trims on the fly.
    """
    size_bytes = _response_value_size_bytes(value, log_context)
    if size_bytes is None or size_bytes <= RUN_RESPONSE_MAX_VALUE_BYTES:
        return None
    capped = truncate_oversized_response_value(value, _known_size_bytes=size_bytes, **log_context)
    if capped is value:
        return None
    capped_size = _response_value_size_bytes(capped, log_context)
    # A capped copy that did not actually shrink would double this row's storage for
    # nothing. The trim is fail-open, so treat an unmeasurable or still-oversized result as
    # "no capped copy" rather than persisting it.
    if capped_size is None or capped_size > RUN_RESPONSE_MAX_VALUE_BYTES:
        return None
    return capped
