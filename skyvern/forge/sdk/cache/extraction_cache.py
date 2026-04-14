"""
In-memory extraction-result cache for `extract-information` LLM calls.

Context: several workflows call `extract-information` repeatedly on
the same page (e.g. a loop that re-navigates to the same list page for every
document it needs to download). The page content, data-extraction goal, and
output schema are identical across iterations, but we still pay the full LLM
cost each time. This cache keys on the content that actually affects the
extraction output and skips the LLM call on a hit.

Scope and lifetime:
- Scoped by `workflow_run_id`. Cache entries for a different run are isolated,
  and a run's entries can be cleared via `clear_workflow_run` (e.g. at run end).
- Purely in-memory, per-process. A workflow run that spans multiple workers
  will still pay the LLM cost the first time each worker sees the page.
- Two-tier eviction:
    - **Outer** (workflow runs): LRU with a cap of `_MAX_WORKFLOW_RUNS`.
      Reads and writes refresh the run's position via `move_to_end`.
    - **Inner** (entries per run): FIFO with a cap of `_MAX_ENTRIES_PER_RUN`.
      Oldest entry is popped when the limit is exceeded.

Key derivation:
- Hashes the inputs that determine the LLM's output:
    - element tree (HTML)
    - extracted page text
    - current URL
    - data extraction goal
    - extracted information schema (JSON-normalized)
    - navigation payload (JSON-normalized)
    - error_code_mapping (JSON-normalized) — changes the rendered prompt
    - previous_extracted_information (JSON-normalized) — rendered in the prompt
      as prior context. Within a single loop iteration this is None on the
      first step of each task, so cache hits still land across iterations of
      a "list -> click row -> list again" loop. Including it keeps
      correctness if an intra-task second-step extraction happens.
    - llm_key — the caller's model override. Cheap to include today (one key
      per block type per run) and prevents stale hits if we later move this
      cache off-process and a user changes models to retune quality.
- Two calls with identical values hash to the same key. Any meaningful change
  (new page content, different schema, etc.) produces a fresh key and a miss.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import structlog

LOG = structlog.get_logger()

_MAX_ENTRIES_PER_RUN = 64
_MAX_WORKFLOW_RUNS = 256
# Sentinel hashed in place of None so that None and "" produce different keys.
_NULL_SENTINEL = "\x00__NULL__"

# Cache scope identifiers. v1 ships with "run" only; "wpid" and "global"
# are reserved for the Redis cross-run cache (SKY-8873/SKY-8874).
SCOPE_RUN = "run"

# Fallback reasons emitted on cache misses. Used by log-based metrics to
# distinguish first-call-in-run (unavoidable) from key-not-found (possible
# normalization opportunity) from lookup_error (bug or infra issue).
FALLBACK_FIRST_CALL_IN_RUN = "first_call_in_run"
FALLBACK_KEY_NOT_FOUND = "key_not_found"
# Reserved for the TTL-backed Redis cache in v4 (SKY-8874). Never emitted in v1.
FALLBACK_TTL_EXPIRED = "ttl_expired"
FALLBACK_LOOKUP_ERROR = "lookup_error"


@dataclass(frozen=True)
class _CacheEntry:
    """Internal wrapper storing a cached value alongside its insertion time.

    `stored_at` is a monotonic clock reading, used only for computing the
    `cache_age_seconds` field reported on cache hits.
    """

    value: Any
    stored_at: float


@dataclass(frozen=True)
class LookupResult:
    """Result of a cache lookup with telemetry metadata.

    On a hit: ``hit=True``, ``value`` is the cached result, ``age_seconds``
    is the elapsed seconds since ``store``, and ``fallback_reason`` is None.
    On a miss: ``hit=False``, ``value`` is None, ``age_seconds`` is None,
    and ``fallback_reason`` identifies why the lookup missed.
    """

    hit: bool
    value: Any | None
    age_seconds: float | None
    fallback_reason: str | None
    scope: str


# workflow_run_id -> ordered dict of {cache_key: _CacheEntry}
_CACHE: OrderedDict[str, OrderedDict[str, _CacheEntry]] = OrderedDict()

# Simple hit/miss counters for post-deploy observability.
_hits = 0
_misses = 0
_HIT_RATE_LOG_INTERVAL = 50  # log hit rate every N lookups


# Matches an ISO-8601 timestamp like 2026-04-10T15:30:45.123456+00:00 on
# its own line. We replace it with the date-only prefix so two calls on the
# same day hash identically.
_ISO_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T[\d:.+\-]+$", re.MULTILINE)


def _normalize_prompt_datetime(prompt: str) -> str:
    """Replace ISO timestamp lines in the prompt with their date-only prefix.

    This lets us hash the rendered prompt directly without microsecond-level
    timestamp churn defeating the cache. The actual prompt sent to the LLM is
    unchanged; only the key-derivation copy is normalized.
    """
    return _ISO_LINE_RE.sub(lambda m: m.group(1), prompt)


def _normalize(value: Any) -> str:
    """Stable JSON serialization for hashing — sorted keys, no whitespace churn."""
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def compute_cache_key(
    *,
    element_tree: str | None = None,
    extracted_text: str | None = None,
    current_url: str | None = None,
    data_extraction_goal: str | None = None,
    extracted_information_schema: Any = None,
    navigation_payload: Any = None,
    error_code_mapping: Any = None,
    previous_extracted_information: Any = None,
    llm_key: str | None = None,
    local_datetime: str | None = None,
    rendered_prompt: str | None = None,
) -> str:
    """Return a stable sha256 hex digest for the inputs that affect extraction output.

    Preferred usage: pass `rendered_prompt` (the fully-rendered extract-information
    prompt string) together with `llm_key` and `local_datetime`. This captures
    any prompt transformations (economy element tree, 2/3 truncation) applied
    inside `load_prompt_with_elements`, so the cache key matches exactly what
    goes to the LLM.

    Legacy usage: the loose-field parameters (element_tree, extracted_text, …)
    are retained for tests and backward compatibility. They are ignored when
    `rendered_prompt` is provided.

    Note: screenshots are passed to the LLM as multimodal input but are NOT
    included in the cache key. For the target loop pattern (same URL, same DOM
    on each re-visit), screenshots are expected to be visually identical when
    the element tree and extracted text match. If this assumption proves wrong
    (e.g. dynamic overlays), we can add a SHA-256 of the screenshot bytes as
    a follow-up.
    """

    def _s(v: str | None) -> str:
        """Map None to a sentinel so None and '' hash differently."""
        return _NULL_SENTINEL if v is None else v

    # Truncate local_datetime to date-only (YYYY-MM-DD) so the key is stable
    # within a single run but changes across midnight for date-relative goals.
    date_only = local_datetime[:10] if local_datetime and len(local_datetime) >= 10 else _s(local_datetime)

    if rendered_prompt is not None:
        # Normalize the local_datetime line inside the rendered prompt so that
        # two calls on the same day produce the same hash. The template emits
        # the full ISO timestamp on its own line; strip sub-date precision
        # before hashing.
        normalized_prompt = _normalize_prompt_datetime(rendered_prompt)
        parts = [normalized_prompt, _s(llm_key)]
    else:
        parts = [
            _s(element_tree),
            _s(extracted_text),
            _s(current_url),
            _s(data_extraction_goal),
            _normalize(extracted_information_schema),
            _normalize(navigation_payload),
            _normalize(error_code_mapping),
            _normalize(previous_extracted_information),
            _s(llm_key),
            date_only,
        ]
    # Use a delimiter that cannot appear inside any part naturally.
    joined = "\x1f".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(joined).hexdigest()


def _miss(fallback_reason: str) -> LookupResult:
    """Build a miss `LookupResult` for the v1 run-scoped cache and bump counters.

    Sole owner of ``_misses`` mutations — ``lookup()`` delegates all miss
    accounting here so the counter is never bumped without also ticking the
    hit-rate logger.
    """
    global _misses  # noqa: PLW0603
    _misses += 1
    _maybe_log_hit_rate()
    return LookupResult(
        hit=False,
        value=None,
        age_seconds=None,
        fallback_reason=fallback_reason,
        scope=SCOPE_RUN,
    )


def lookup(workflow_run_id: str | None, cache_key: str) -> LookupResult | None:
    """Look up a cached extraction result and return a structured telemetry record.

    Returns a :class:`LookupResult` on a genuine hit or miss, or ``None`` when
    the cache path is bypassed entirely (no ``workflow_run_id``).

    Call sites should treat ``None`` as "cache not applicable here" — no hit/miss
    log should be emitted and no metric counter should be bumped. ``None`` is
    intentionally distinct from a miss so that Datadog dashboards are not
    inflated with non-actionable zero-run lookups.

    On a genuine miss, the returned ``LookupResult`` carries :attr:`~LookupResult.fallback_reason`
    so log-based metrics can distinguish first-call-in-run (unavoidable) from
    key-not-found (possible normalization opportunity) from lookup-error (infra issue).
    """
    global _hits  # noqa: PLW0603

    if not workflow_run_id:
        return None

    run_cache = _CACHE.get(workflow_run_id)
    if not run_cache:
        return _miss(FALLBACK_FIRST_CALL_IN_RUN)

    entry = run_cache.get(cache_key)
    if entry is None:
        return _miss(FALLBACK_KEY_NOT_FOUND)

    _hits += 1
    # Refresh LRU position so actively-read runs aren't evicted.
    _CACHE.move_to_end(workflow_run_id)
    _maybe_log_hit_rate()
    # Clamp to zero to guard against monotonic clock edge cases.
    age = max(0.0, time.monotonic() - entry.stored_at)
    return LookupResult(
        hit=True,
        value=entry.value,
        age_seconds=age,
        fallback_reason=None,
        scope=SCOPE_RUN,
    )


def _maybe_log_hit_rate() -> None:
    """Log cumulative hit rate every _HIT_RATE_LOG_INTERVAL lookups."""
    total = _hits + _misses
    if total > 0 and total % _HIT_RATE_LOG_INTERVAL == 0:
        LOG.info(
            "extraction_cache.hit_rate",
            hits=_hits,
            misses=_misses,
            total=total,
            hit_rate=round(_hits / total, 3),
        )


def store(workflow_run_id: str | None, cache_key: str, result: Any) -> None:
    """Store an extraction result for later reuse within the same workflow run."""
    if not workflow_run_id:
        return
    if workflow_run_id in _CACHE:
        run_cache = _CACHE[workflow_run_id]
        _CACHE.move_to_end(workflow_run_id)
    else:
        run_cache = OrderedDict()
        _CACHE[workflow_run_id] = run_cache
    if cache_key in run_cache:
        run_cache.move_to_end(cache_key)
    run_cache[cache_key] = _CacheEntry(value=result, stored_at=time.monotonic())
    while len(run_cache) > _MAX_ENTRIES_PER_RUN:
        evicted_key, _ = run_cache.popitem(last=False)
        LOG.debug(
            "extraction_cache.evicted",
            workflow_run_id=workflow_run_id,
            cache_key=evicted_key,
        )
    # Evict oldest workflow runs if the global cache grows too large.
    while len(_CACHE) > _MAX_WORKFLOW_RUNS:
        evicted_run_id, _ = _CACHE.popitem(last=False)
        LOG.debug("extraction_cache.run_evicted", workflow_run_id=evicted_run_id)


def clear_workflow_run(workflow_run_id: str | None) -> None:
    """Drop all cached entries for the given workflow run. Safe to call on unknown IDs."""
    if not workflow_run_id:
        return
    _CACHE.pop(workflow_run_id, None)


def _reset_for_tests() -> None:
    """Test-only: wipe the global cache and counters between unit tests."""
    global _hits, _misses  # noqa: PLW0603
    _CACHE.clear()
    _hits = 0
    _misses = 0
