"""
In-memory extraction-result cache for `extract-information` LLM calls.

Context: several workflows call `extract-information` repeatedly on
the same page (e.g. a loop that re-navigates to the same list page for every
document it needs to download). The page content, data-extraction goal, and
output schema are identical across iterations, but we still pay the full LLM
cost each time. This cache keys on the content that actually affects the
extraction output and skips the LLM call on a hit.

This module is the v2 in-process tier. A cross-run Redis tier (SKY-8873) sits
behind the `lookup_cross_run_extraction_cache` / `store_cross_run_extraction_cache`
hooks on `AgentFunction` — this module stays OSS-safe and scope-neutral.

Scope and lifetime:
- In-process tier scoped by `workflow_run_id`. Cache entries for a different
  run are isolated, and a run's entries can be cleared via `clear_workflow_run`
  (e.g. at run end).
- Purely in-memory, per-process. Cross-run / cross-worker persistence is
  handled by the cloud-side Redis tier behind the AgentFunction hooks.
- Two-tier eviction:
    - **Outer** (workflow runs): LRU with a cap of `_MAX_WORKFLOW_RUNS`.
      Reads and writes refresh the run's position via `move_to_end`.
    - **Inner** (entries per run): FIFO with a cap of `_MAX_ENTRIES_PER_RUN`.
      Oldest entry is popped when the limit is exceeded.

Key derivation (shared with the cross-run tier):
- Hashes only the inputs that determine the LLM's output:
    - element tree (HTML, canonicalized to collapse transient IDs)
    - extracted page text (ISO-timestamp lines collapsed to date prefix)
    - current URL (query params sorted; nonce values redacted)
    - data extraction goal
    - extracted information schema (JSON-normalized)
    - navigation payload (JSON-normalized)
    - error_code_mapping (JSON-normalized) — changes the rendered prompt
    - previous_extracted_information (JSON-normalized) — rendered in the prompt
      as prior context. Within a single loop iteration this is None on the
      first step of each task, so cache hits still land across iterations of
      a "list -> click row -> list again" loop. Including it keeps
      correctness if an intra-task second-step extraction happens.
    - llm_key — the caller's model override. Prevents stale hits when a user
      changes models to retune quality.
- Date is intentionally NOT in the key. Two calls on byte-identical page
  content are semantically the same extraction regardless of wall-clock
  date; relying on the content hash keeps hit rate up for scheduled
  workflows that run many days apart on stable pages. Staleness is bounded
  by the Redis TTL and the shadow-mode FP gate (SKY-8871).
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
from urllib.parse import urlparse, urlunparse

import structlog
from selectolax.parser import HTMLParser

LOG = structlog.get_logger()

_MAX_ENTRIES_PER_RUN = 64
_MAX_WORKFLOW_RUNS = 256
# Sentinel hashed in place of None so that None and "" produce different keys.
_NULL_SENTINEL = "\x00__NULL__"

# Cache scope identifiers. "run" is the in-process per-workflow-run tier;
# "wpid" is the Redis cross-run tier keyed on workflow_permanent_id
# (SKY-8873). "global" is reserved for a future cross-WPID tier.
SCOPE_RUN = "run"
SCOPE_WPID = "wpid"

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


def _normalize_datetime_lines(text: str) -> str:
    """Collapse ISO-timestamp-only lines to their date prefix so same-day calls
    hash identically. Operates on any plain-text input (rendered prompts,
    extracted text, etc.); only the key-derivation copy is normalized.
    """
    return _ISO_LINE_RE.sub(lambda m: m.group(1), text)


# Query parameter names treated as nondeterministic nonces/session tokens.
# Values are replaced with a placeholder; the param is still kept so the
# presence/absence of the param still matters. Only names that are
# unambiguously cachebusters/CSRF/session markers are listed — ambiguous
# short names like `state`, `token`, `sid`, `ts`, and `cb` (callback,
# category, customer bucket) are excluded because redacting them risks
# false cache hits when they're primary differentiators.
_NONCE_PARAM_NAMES = frozenset(
    {
        "_",
        "_csrf",
        "authenticity_token",
        "cache_buster",
        "cachebuster",
        "csrf",
        "csrf_token",
        "csrfmiddlewaretoken",
        "csrftoken",
        "nonce",
        "session_id",
        "sessionid",
        "timestamp",
    }
)


# Element attribute names whose values are treated as nondeterministic and
# replaced with a sentinel during cache-key derivation. Scoped to identifier
# attributes — values of `class`, `href`, `src`, `alt`, `title`, and `name`
# are preserved because they typically differentiate real pages.
# NOTE: `name` is intentionally excluded — <input name="field_name"> carries
# semantic field identity (not a transient ID) and must differentiate pages.
_SUSPECT_ATTR_NAMES = frozenset(
    {
        "id",
        "for",
        "aria-labelledby",
        "aria-describedby",
        "data-testid",
    }
)

# <input name="..."> values that are CSRF/anti-forgery tokens. The value is
# replaced with "" (empty string) during canonicalization.
_CSRF_INPUT_NAMES = frozenset(
    {
        "_csrf",
        "_token",
        "authenticity_token",
        "csrf",
        "csrf_token",
        "csrfmiddlewaretoken",
        "csrftoken",
        "__requestverificationtoken",
    }
)

# <meta name="..."> values that carry CSRF tokens in `content=`.
_CSRF_META_NAMES = frozenset({"csrf-token", "csrf_token", "_csrf"})

_NONCE_VALUE_SENTINEL = "__NONCE__"

# Matches UUID v4 substrings inside an attribute value. v4 is random; v1/v3/v5
# are time- or namespace-based and can be stable business keys, so we leave
# them alone to avoid collapsing genuinely different entities into one key.
_UUID_V4_IN_VALUE_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Matches a random-looking hex suffix preceded by a `-`, `_`, or `:` delimiter
# inside an attribute value. Two lookaheads anchor the "random" shape:
#   - ``(?=[0-9a-f]*[a-f])`` keeps purely numeric suffixes like ``order-123456``
#     intact — those are typically stable business IDs.
#   - ``(?=[0-9a-f]*[0-9])`` keeps hex-letter-only English words like ``facade``
#     or ``decade`` intact — collapsing them would introduce false cache hits.
_TRANSIENT_HEX_SUFFIX_IN_VALUE_RE = re.compile(
    r"(?<=[-_:])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{6,}",
    re.IGNORECASE,
)


def _redact_transient_in_value(value: str) -> str:
    """Replace UUID-v4 and random-hex-suffix substrings within an attribute value.

    Preserves stable business IDs (``id="submit-button"``, ``id="order-123456"``,
    ``id="zone-facade"``) while collapsing rotating IDs (``id="item-3f8a9b12…"``,
    ``data-testid="btn-1a2b3c4d"``).
    """
    value = _UUID_V4_IN_VALUE_RE.sub("__UUID__", value)
    value = _TRANSIENT_HEX_SUFFIX_IN_VALUE_RE.sub("__TID__", value)
    return value


def _canonical_url(url: str | None) -> str | None:
    """Return a canonical form of ``url`` for cache-key derivation.

    Redacts nonce query *values* (keys in ``_NONCE_PARAM_NAMES``) to
    ``_NONCE_VALUE_SENTINEL`` while keeping the param itself so presence/absence
    still differentiates cache keys, and sorts remaining pairs by lowercased key.
    Fragments are preserved because SPAs use hash routing
    (``#/orders/123`` vs ``#/orders/456``) to encode page identity; stripping
    would collapse structurally-different pages into the same key.
    Scheme and host casing are NOT normalized — URLs come from the browser so
    case variance is rare; flag if we see misses driven by it.
    Path segments are NOT normalized — URLs that embed session tokens in the
    path (``/session/abc123/docs``) will produce distinct keys per session; if
    this becomes a hit-rate concern we can extend the canonicalization.
    Never raises; on a malformed input that ``urlparse`` can't round-trip we
    return the original string so cache lookup degrades gracefully.
    """
    if url is None:
        return None
    if url == "":
        return ""
    try:
        parsed = urlparse(url)
        # Manual split so `?flag` (bare) and `?flag=` (empty value) stay
        # distinct. parse_qsl collapses both to ('flag', ''), which produced
        # false cache hits when both forms appeared for the same key.
        triples: list[tuple[str, str, bool]] = []
        for seg in parsed.query.split("&"):
            if not seg:
                continue
            if "=" in seg:
                k, _, v = seg.partition("=")
                triples.append((k, v, True))
            else:
                triples.append((seg, "", False))
        # Only redact when the nonce has a non-empty value; `?nonce=` keeps
        # its empty value so it doesn't collide with `?nonce=abc`.
        canonicalized = [
            (k, _NONCE_VALUE_SENTINEL if v and k.lower() in _NONCE_PARAM_NAMES else v, has_eq)
            for k, v, has_eq in triples
        ]
        canonicalized.sort(key=lambda p: p[0].lower())
        new_query = "&".join(f"{k}={v}" if has_eq else k for k, v, has_eq in canonicalized)
        return urlunparse(parsed._replace(query=new_query))
    except (ValueError, TypeError):
        return url


def _canonical_element_tree(html: str | None) -> str | None:
    """Return a canonicalized HTML string for cache-key derivation.

    Redacts UUID-v4 / random-hex-suffix substrings within identifier-style
    attribute values (id/for/aria-*/data-testid) and zeros CSRF-token
    <input>/<meta> contents. Stable business IDs (``id='submit-button'``,
    ``id='order-123456'``) and semantic fields (``class``, ``href``, ``src``,
    ``name``, text content, document structure) are preserved.

    Never raises. Returns the input unchanged if parsing fails.
    """
    if html is None:
        return None
    if html == "":
        return ""
    try:
        tree = HTMLParser(html)
        for node in tree.root.traverse(include_text=False):
            if not node.attributes:
                continue
            tag = node.tag

            # CSRF scrubbing reads `name` before the sentinel pass so it is
            # always available regardless of _SUSPECT_ATTR_NAMES membership.
            if tag == "input":
                input_name = (node.attributes.get("name", "") or "").lower()
                if input_name in _CSRF_INPUT_NAMES:
                    node.attrs["value"] = ""
            elif tag == "meta":
                meta_name = (node.attributes.get("name", "") or "").lower()
                if meta_name in _CSRF_META_NAMES:
                    node.attrs["content"] = ""

            # Pattern-based value redaction inside suspect attributes: UUIDs
            # and random hex suffixes collapse; stable business IDs survive.
            for attr_name in list(node.attributes.keys()):
                if attr_name.lower() in _SUSPECT_ATTR_NAMES:
                    current_val = node.attributes.get(attr_name) or ""
                    node.attrs[attr_name] = _redact_transient_in_value(current_val)

        # selectolax's html property returns the full serialized tree
        return tree.html or html
    except Exception:
        # WARNING rather than DEBUG so a transient parser regression surfaces
        # in Datadog instead of silently degrading cache hits.
        LOG.warning("canonical_element_tree_failed", exc_info=True)
        return html


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
    call_path: str,
    element_tree: str | None = None,
    extracted_text: str | None = None,
    current_url: str | None = None,
    data_extraction_goal: str | None = None,
    extracted_information_schema: Any = None,
    navigation_payload: Any = None,
    error_code_mapping: Any = None,
    previous_extracted_information: Any = None,
    llm_key: str | None = None,
) -> str:
    """Return a stable sha256 hex digest for the inputs that affect extraction output.

    ``call_path`` is a required discriminator so different callsites (agent,
    handler, script) never collide even when their other inputs happen to
    hash identically — e.g. when ``ai_extract`` runs on a goal with no
    ``{{ var }}`` substitutions and no nav context, all other parts can match
    ``extract_information_for_navigation_goal``'s inputs and produce the same
    SHA otherwise.

    Date is intentionally omitted: two calls on the same page content are
    semantically the same extraction regardless of wall-clock date. The
    in-prompt ``{{ local_datetime }}`` interpolation is not part of the
    key — if the scraped content is identical, the cached result is valid.
    Staleness is bounded by the Redis TTL (cross-run tier) and the shadow-
    mode FP gate (SKY-8871) that compares cached vs fresh on a sampled rate.
    """

    def _s(v: str | None) -> str:
        """Map None to a sentinel so None and '' hash differently."""
        return _NULL_SENTINEL if v is None else v

    canonical_url = _canonical_url(current_url)
    canonical_element_tree = _canonical_element_tree(element_tree)
    canonical_extracted_text = _normalize_datetime_lines(extracted_text) if extracted_text is not None else None
    canonical_goal = _normalize_datetime_lines(data_extraction_goal) if data_extraction_goal is not None else None

    parts = [
        call_path,
        _s(canonical_element_tree),
        _s(canonical_extracted_text),
        _s(canonical_url),
        _s(canonical_goal),
        _normalize(extracted_information_schema),
        _normalize(navigation_payload),
        _normalize(error_code_mapping),
        _normalize(previous_extracted_information),
        _s(llm_key),
    ]
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
    """Store an extraction result for later reuse within the same workflow run.

    No size bound today — extraction results are typically small JSON. Revisit
    when the v4 Redis transition lands and introduces a per-entry byte cap.
    """
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


def invalidate_key(workflow_run_id: str | None, cache_key: str) -> bool:
    """Drop a single cached entry within a workflow run.

    Used by the retry self-heal path (SKY-8873): when a step retries we
    assume the previous attempt's cached value is suspect, so we evict it
    and let the subsequent ``store`` overwrite with the fresh LLM result.

    Returns True if an entry was removed, False otherwise. Safe to call on
    unknown IDs / keys.
    """
    if not workflow_run_id:
        return False
    run_cache = _CACHE.get(workflow_run_id)
    if run_cache is None:
        return False
    return run_cache.pop(cache_key, None) is not None


def _reset_for_tests() -> None:
    """Test-only: wipe the global cache and counters between unit tests."""
    global _hits, _misses  # noqa: PLW0603
    _CACHE.clear()
    _hits = 0
    _misses = 0
