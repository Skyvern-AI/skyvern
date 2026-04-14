"""Shadow-mode FP-rate sampling for the extract-information cache.

On sampled cache hits, fire the LLM call in the background and log a
comparison event so a log-based metric can derive the cache's false-positive
rate. ``diff_summary`` records only paths, never values (log safety). LLM
errors are swallowed — shadow is best-effort.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import structlog

LOG = structlog.get_logger()

# Single consolidated event. Filter on `status:ok` for the FP metric, `status:error` for reliability.
_SHADOW_EVENT = "extract_information.shadow_comparison"

_MODE_STRICT = "strict"
_MODE_SEMANTIC = "semantic"

# Strips the `[0]`/`[12]` index segments from a runtime diff path so it can be
# matched against schema paths that use `[*]` wildcards for list elements.
_LIST_INDEX_RE = re.compile(r"\[\d+\]")

_LlmCall = Callable[[], Awaitable[Any]]


def _elapsed_ms(start_seconds: float) -> int:
    """Elapsed ms since ``start_seconds`` (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - start_seconds) * 1000)


class _LoggerLike(Protocol):
    def debug(self, event: str, **kwargs: Any) -> None: ...

    def info(self, event: str, **kwargs: Any) -> None: ...

    def warning(self, event: str, **kwargs: Any) -> None: ...


@dataclass
class ComparisonResult:
    match: bool
    mode: str
    diff_summary: set[str] = field(default_factory=set)


def _resolve_ref(ref: str, root: Any) -> Any | None:
    """Resolve a local JSON Schema ``$ref`` (``#/$defs/Foo``) against ``root``.

    Returns ``None`` for anything non-local or malformed; the caller then just
    skips that branch rather than raising. External refs are never followed.
    """
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = root
    for segment in ref[2:].split("/"):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def _collect_unique_item_paths(
    schema: Any,
    prefix: str = "root",
    *,
    root: Any | None = None,
    seen_refs: frozenset[str] | None = None,
) -> set[str]:
    """Return dotted paths for arrays in ``schema`` declared ``uniqueItems``.

    Paths use ``_diff_paths``'s convention: ``"root"`` at the top, bare field
    names for top-level properties, dotted names for nested ones, and ``[*]``
    for array-item recursion so nested unique arrays don't pollute their
    parent's path (``groups`` vs ``groups[*]``).

    Recurses into ``allOf``/``anyOf``/``oneOf`` combinators and resolves local
    ``$ref`` pointers against ``$defs`` — Pydantic's ``model_json_schema()``
    uses both heavily for nested models and ``Field(description=...)`` fields.
    Missing either would make semantic mode a no-op for most real schemas.
    ``seen_refs`` guards against circular references.
    """
    paths: set[str] = set()
    if not isinstance(schema, dict):
        return paths

    if root is None:
        root = schema
    if seen_refs is None:
        seen_refs = frozenset()

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in seen_refs:
        # Skip re-expanding a ref already in the current path (cycle guard),
        # but fall through to scan any sibling keywords on this node.
        resolved = _resolve_ref(ref, root)
        if resolved is not None:
            paths.update(_collect_unique_item_paths(resolved, prefix, root=root, seen_refs=seen_refs | {ref}))

    if schema.get("uniqueItems") is True:
        paths.add(prefix)

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            child_prefix = f"{prefix}.{name}" if prefix != "root" else name
            paths.update(_collect_unique_item_paths(sub, child_prefix, root=root, seen_refs=seen_refs))

    items = schema.get("items")
    if isinstance(items, dict):
        paths.update(_collect_unique_item_paths(items, f"{prefix}[*]", root=root, seen_refs=seen_refs))

    for combinator in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list):
            for branch in branches:
                paths.update(_collect_unique_item_paths(branch, prefix, root=root, seen_refs=seen_refs))

    return paths


def _canonical_form(value: Any, *, unique_item_paths: set[str], prefix: str) -> Any:
    """Hashable canonical form for set-equality inside ``uniqueItems`` comparisons.

    Mirrors the semantic rules ``_diff_paths`` applies elsewhere so set-equality
    doesn't contradict field-level equality:

    - ``int`` and ``float`` collapse to the same ``("n", float)`` key (JSON
      number equivalence, matching the numeric cross-type exception in
      ``_diff_paths``). ``bool`` stays distinct from numerics.
    - Nested ``uniqueItems`` arrays inside an element are sorted so two
      elements that differ only by inner reorder produce the same form.
    - All other lists stay order-sensitive.

    ``prefix`` is the schema path of ``value`` using ``[*]`` wildcards (no
    runtime indices), so membership against ``unique_item_paths`` is direct.
    """
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int):
        # Keep exact int precision — float(large_int) would collapse distinct
        # ids above 2^53 to the same canonical key.
        return ("n", value)
    if isinstance(value, float):
        # Collapse integer-valued floats into the int form so 1 == 1.0 under
        # semantic equality (matches _diff_paths's int/float numeric exception).
        if value.is_integer():
            return ("n", int(value))
        return ("n", value)
    if value is None:
        return ("z",)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, dict):
        entries = []
        for key in sorted(value.keys()):
            child_prefix = f"{prefix}.{key}" if prefix != "root" else key
            entries.append((key, _canonical_form(value[key], unique_item_paths=unique_item_paths, prefix=child_prefix)))
        return ("d", tuple(entries))
    if isinstance(value, list):
        child_prefix = f"{prefix}[*]"
        child_forms = tuple(
            _canonical_form(item, unique_item_paths=unique_item_paths, prefix=child_prefix) for item in value
        )
        if prefix in unique_item_paths:
            # Sort by repr: canonical forms mix tuple shapes, and repr gives
            # a stable total order without requiring all elements to be
            # mutually comparable.
            return ("lu", tuple(sorted(Counter(child_forms).items(), key=lambda kv: repr(kv[0]))))
        return ("l", child_forms)
    raise TypeError(f"Non-JSON-serializable value in extraction result: {type(value)}")


def _diff_paths(
    cached: Any,
    fresh: Any,
    *,
    unique_item_paths: set[str],
    prefix: str = "root",
) -> set[str]:
    """Dotted paths where ``cached`` and ``fresh`` differ; ``uniqueItems`` lists compared as sets."""
    diffs: set[str] = set()

    if type(cached) is not type(fresh):
        # int/float cross-compare is the one narrow exception. bool is a subclass of int,
        # so exclude it explicitly — True vs 1 must register as a diff.
        cached_numeric = isinstance(cached, (int, float)) and not isinstance(cached, bool)
        fresh_numeric = isinstance(fresh, (int, float)) and not isinstance(fresh, bool)
        if not (cached_numeric and fresh_numeric):
            diffs.add(prefix)
            return diffs

    if isinstance(cached, dict) and isinstance(fresh, dict):
        keys = set(cached.keys()) | set(fresh.keys())
        for key in keys:
            child_path = f"{prefix}.{key}" if prefix != "root" else key
            if key not in cached or key not in fresh:
                diffs.add(child_path)
                continue
            diffs.update(
                _diff_paths(
                    cached[key],
                    fresh[key],
                    unique_item_paths=unique_item_paths,
                    prefix=child_path,
                )
            )
        return diffs

    if isinstance(cached, list) and isinstance(fresh, list):
        # Normalize runtime indices (`groups[0]`) to schema wildcards (`groups[*]`) for lookup.
        normalized = _LIST_INDEX_RE.sub("[*]", prefix)
        if normalized in unique_item_paths:
            # Counter preserves multiplicity — ['a','a'] vs ['a'] is a mismatch.
            # _canonical_form recursively applies int/float equivalence and
            # nested-uniqueItems reorder tolerance so set-equality here stays
            # consistent with _diff_paths's field-level rules.
            child_prefix = f"{normalized}[*]"
            cached_forms = Counter(
                _canonical_form(item, unique_item_paths=unique_item_paths, prefix=child_prefix) for item in cached
            )
            fresh_forms = Counter(
                _canonical_form(item, unique_item_paths=unique_item_paths, prefix=child_prefix) for item in fresh
            )
            if cached_forms != fresh_forms:
                diffs.add(prefix)
            return diffs
        if len(cached) != len(fresh):
            diffs.add(prefix)
            return diffs
        for idx, (a, b) in enumerate(zip(cached, fresh)):
            diffs.update(_diff_paths(a, b, unique_item_paths=unique_item_paths, prefix=f"{prefix}[{idx}]"))
        return diffs

    if cached != fresh:
        diffs.add(prefix)
    return diffs


def compare_results(cached: Any, fresh: Any, *, schema: Any | None) -> ComparisonResult:
    """Compare cached vs fresh; ``uniqueItems`` arrays use set-equality (``semantic`` mode).

    Note: never short-circuit on ``cached == fresh``. Python treats ``True == 1``
    and ``False == 0`` as equal, which would mask real bool/int diffs.
    """
    unique_item_paths = _collect_unique_item_paths(schema) if schema else set()
    mode = _MODE_SEMANTIC if unique_item_paths else _MODE_STRICT

    diffs = _diff_paths(cached, fresh, unique_item_paths=unique_item_paths)
    return ComparisonResult(match=not diffs, mode=mode, diff_summary=diffs)


async def run_shadow_comparison(
    *,
    cache_key: str,
    workflow_run_id: str,
    cached_value: Any,
    cached_age_seconds: float,
    llm_call: _LlmCall,
    schema: Any | None,
    logger: _LoggerLike | None = None,
) -> None:
    """Run the shadow LLM call, compare, emit one log event. Never raises.

    Emits a single ``extract_information.shadow_comparison`` event with a
    ``status`` field (``ok`` or ``error``). Only exception *class names* are
    logged — messages can contain raw model output and would leak extracted
    content into observability data.
    """
    log = logger or LOG
    started_at = time.monotonic()
    try:
        fresh = await llm_call()
    except Exception as exc:  # noqa: BLE001 — shadow is best-effort
        log.warning(
            _SHADOW_EVENT,
            status="error",
            error_stage="llm_call",
            error_type=type(exc).__name__,
            cache_key=cache_key,
            workflow_run_id=workflow_run_id,
            cached_age_seconds=cached_age_seconds,
            shadow_duration_ms=_elapsed_ms(started_at),
        )
        return

    try:
        comparison = compare_results(cached_value, fresh, schema=schema)
    except Exception as exc:  # noqa: BLE001 — defensive; compare_results is pure
        log.warning(
            _SHADOW_EVENT,
            status="error",
            error_stage="compare",
            error_type=type(exc).__name__,
            cache_key=cache_key,
            workflow_run_id=workflow_run_id,
            cached_age_seconds=cached_age_seconds,
            shadow_duration_ms=_elapsed_ms(started_at),
        )
        return

    log.info(
        _SHADOW_EVENT,
        status="ok",
        cache_key=cache_key,
        workflow_run_id=workflow_run_id,
        match=comparison.match,
        mode=comparison.mode,
        diff_summary=sorted(_LIST_INDEX_RE.sub("[*]", p) for p in comparison.diff_summary),
        cached_age_seconds=cached_age_seconds,
        shadow_duration_ms=_elapsed_ms(started_at),
    )


# Strong refs so asyncio doesn't GC in-flight shadow tasks (create_task only holds a weak ref).
_PENDING_SHADOW_TASKS: set[asyncio.Task[None]] = set()
# Cap chosen so shadow calls never sustain more than ~10% of a typical provider's
# burst quota at the expected 1% sample rate. Raise if sample rate climbs.
_MAX_PENDING_SHADOWS = 50


def _prune_pending() -> None:
    """Drop already-finished tasks before the cap check.

    ``add_done_callback`` fires on the task's own event loop, so tasks created on
    a loop that later gets replaced (worker recycle, test-session boundaries)
    would never ``discard`` themselves and the set would leak.
    """
    _PENDING_SHADOW_TASKS.difference_update({t for t in _PENDING_SHADOW_TASKS if t.done()})


def _track(task: asyncio.Task[None]) -> asyncio.Task[None]:
    _PENDING_SHADOW_TASKS.add(task)
    task.add_done_callback(_PENDING_SHADOW_TASKS.discard)
    return task


def schedule_shadow_comparison(
    *,
    cache_key: str,
    workflow_run_id: str,
    cached_value: Any,
    cached_age_seconds: float,
    llm_call: _LlmCall,
    schema: Any | None,
    logger: _LoggerLike | None = None,
) -> asyncio.Task[None] | None:
    """Fire-and-forget ``run_shadow_comparison``; returns the task so tests can await."""
    _prune_pending()
    if len(_PENDING_SHADOW_TASKS) >= _MAX_PENDING_SHADOWS:
        (logger or LOG).warning("shadow_task_cap_reached", pending=len(_PENDING_SHADOW_TASKS))
        return None

    return _track(
        asyncio.create_task(
            run_shadow_comparison(
                cache_key=cache_key,
                workflow_run_id=workflow_run_id,
                cached_value=cached_value,
                cached_age_seconds=cached_age_seconds,
                llm_call=llm_call,
                schema=schema,
                logger=logger,
            )
        )
    )


def schedule_shadow_check(
    *,
    gate: Callable[[], Awaitable[bool]],
    cache_key: str,
    workflow_run_id: str,
    cached_value: Any,
    cached_age_seconds: float,
    llm_call: _LlmCall,
    schema: Any | None,
    logger: _LoggerLike | None = None,
) -> asyncio.Task[None] | None:
    """Fire-and-forget: await ``gate`` in the background, run the comparison if it returns True.

    Keeps the PostHog (or any other) feature-flag lookup off the cache-hit hot
    path. The caller returns immediately; the background task does both the
    gate evaluation and the shadow LLM call.
    """
    _prune_pending()
    if len(_PENDING_SHADOW_TASKS) >= _MAX_PENDING_SHADOWS:
        (logger or LOG).warning("shadow_task_cap_reached", pending=len(_PENDING_SHADOW_TASKS))
        return None

    async def _runner() -> None:
        log = logger or LOG
        try:
            enabled = await gate()
        except Exception as exc:  # noqa: BLE001 — gate errors must not propagate
            log.warning(
                _SHADOW_EVENT,
                status="error",
                error_stage="gate",
                error_type=type(exc).__name__,
                cache_key=cache_key,
                workflow_run_id=workflow_run_id,
                cached_age_seconds=cached_age_seconds,
            )
            return
        if not enabled:
            # Info, not debug — service default level is INFO, so debug events are
            # dropped in production. We need the skipped count as the denominator
            # when verifying the PostHog sampling rate from logs.
            log.info(
                _SHADOW_EVENT,
                status="skipped",
                cache_key=cache_key,
                workflow_run_id=workflow_run_id,
            )
            return
        await run_shadow_comparison(
            cache_key=cache_key,
            workflow_run_id=workflow_run_id,
            cached_value=cached_value,
            cached_age_seconds=cached_age_seconds,
            llm_call=llm_call,
            schema=schema,
            logger=logger,
        )

    return _track(asyncio.create_task(_runner()))
