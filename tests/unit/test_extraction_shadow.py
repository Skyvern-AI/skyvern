"""Unit tests for the extract-information shadow-mode correctness verification."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from skyvern.forge.sdk.cache import extraction_shadow

# ---------------------------------------------------------------------------
# compare_results — strict equality
# ---------------------------------------------------------------------------


def test_compare_strict_identical_dicts_match() -> None:
    """Two dicts with identical fields should match under strict comparison."""
    cached = {"title": "Invoice #123", "total": 42.5}
    fresh = {"title": "Invoice #123", "total": 42.5}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is True
    assert result.mode == "strict"
    assert result.diff_summary == set()


def test_compare_strict_field_value_mismatch_reports_diff() -> None:
    cached = {"title": "Invoice #123", "total": 42.5}
    fresh = {"title": "Invoice #123", "total": 42.6}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is False
    assert result.mode == "strict"
    # diff_summary should name the mismatching path.
    assert "total" in result.diff_summary
    # And must NOT leak the raw mismatching values — we care about which path
    # differed, not the exact content (diff_summary is going to a log line).
    assert "42.5" not in str(result.diff_summary)
    assert "42.6" not in str(result.diff_summary)


def test_compare_strict_missing_field_reports_diff() -> None:
    cached = {"title": "x", "total": 1}
    fresh = {"title": "x"}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is False
    assert "total" in result.diff_summary


def test_compare_strict_extra_field_reports_diff() -> None:
    cached = {"title": "x"}
    fresh = {"title": "x", "extra": True}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is False
    assert "extra" in result.diff_summary


def test_compare_strict_nested_dict_mismatch_reports_path() -> None:
    cached = {"meta": {"page": 1, "count": 10}}
    fresh = {"meta": {"page": 1, "count": 11}}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is False
    # Path should surface the nested key so we can bucket regressions by field.
    assert any("count" in path for path in result.diff_summary)


def test_compare_strict_list_order_matters_without_schema() -> None:
    cached = {"docs": ["a.pdf", "b.pdf"]}
    fresh = {"docs": ["b.pdf", "a.pdf"]}
    # With no schema hint, lists are ordered — reordering is a mismatch.
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is False


def test_compare_strict_list_identical_order_match() -> None:
    cached = {"docs": ["a.pdf", "b.pdf"]}
    fresh = {"docs": ["a.pdf", "b.pdf"]}
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is True


def test_compare_string_result_match() -> None:
    result = extraction_shadow.compare_results("hello world", "hello world", schema=None)
    assert result.match is True


def test_compare_string_result_mismatch() -> None:
    result = extraction_shadow.compare_results("hello world", "hello universe", schema=None)
    assert result.match is False
    assert "root" in result.diff_summary or "" in result.diff_summary


def test_compare_root_list_result_match() -> None:
    """Some extraction schemas produce a list at the root — must still compare correctly."""
    cached = [{"id": 1}, {"id": 2}]
    fresh = [{"id": 1}, {"id": 2}]
    result = extraction_shadow.compare_results(cached, fresh, schema=None)
    assert result.match is True


def test_compare_none_results_match() -> None:
    result = extraction_shadow.compare_results(None, None, schema=None)
    assert result.match is True


def test_compare_one_none_one_populated_mismatch() -> None:
    result = extraction_shadow.compare_results(None, {"a": 1}, schema=None)
    assert result.match is False


# ---------------------------------------------------------------------------
# compare_results — semantic list-as-set when schema declares uniqueItems
# ---------------------------------------------------------------------------


def test_compare_semantic_unique_items_list_order_insensitive() -> None:
    """When schema marks a list as uniqueItems, reordering is a match, not a diff.

    This matches the RFC: extract-information may return list elements in a
    different order on a fresh run even though the set of items is identical.
    """
    schema = {
        "type": "object",
        "properties": {
            "docs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
    }
    cached = {"docs": ["a.pdf", "b.pdf"]}
    fresh = {"docs": ["b.pdf", "a.pdf"]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True
    assert result.mode == "semantic"


def test_compare_semantic_unique_items_list_content_mismatch_is_diff() -> None:
    """Different contents — not just order — are still a mismatch."""
    schema = {
        "type": "object",
        "properties": {
            "docs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
    }
    cached = {"docs": ["a.pdf", "b.pdf"]}
    fresh = {"docs": ["a.pdf", "c.pdf"]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is False
    assert "docs" in result.diff_summary


def test_compare_semantic_non_unique_list_still_order_sensitive() -> None:
    """Lists without uniqueItems must stay order-sensitive — we can't assume set semantics."""
    schema = {
        "type": "object",
        "properties": {
            "entries": {"type": "array", "items": {"type": "string"}},  # no uniqueItems
        },
    }
    cached = {"entries": ["a", "b"]}
    fresh = {"entries": ["b", "a"]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is False


def test_compare_semantic_unique_items_list_of_dicts() -> None:
    """Unique-item lists of dicts must compare as sets (hashable via sorted-json)."""
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "object"},
            },
        },
    }
    cached = {"items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    fresh = {"items": [{"id": 2, "name": "b"}, {"id": 1, "name": "a"}]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


def test_compare_semantic_root_array_with_unique_items_order_insensitive() -> None:
    """Schema whose *root* is a uniqueItems array must also get set semantics.

    Regression guard for a bug where _collect_unique_item_paths only recorded
    uniqueItems paths when they had a non-empty dotted prefix, so root arrays
    were still compared order-sensitively — inflating the shadow FP metric.
    """
    schema = {"type": "array", "uniqueItems": True, "items": {"type": "string"}}
    cached = ["a.pdf", "b.pdf"]
    fresh = ["b.pdf", "a.pdf"]
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True
    assert result.mode == "semantic"


def test_compare_semantic_root_array_with_unique_items_content_mismatch() -> None:
    schema = {"type": "array", "uniqueItems": True, "items": {"type": "string"}}
    cached = ["a.pdf", "b.pdf"]
    fresh = ["a.pdf", "c.pdf"]
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is False


# ---------------------------------------------------------------------------
# compare_results — combinator schemas (allOf/anyOf/oneOf)
# ---------------------------------------------------------------------------


def test_compare_semantic_unique_items_inside_all_of_wrapper() -> None:
    """Pydantic wraps array fields in allOf when Field(description=...) is used.

    Without combinator traversal, uniqueItems on these fields would be missed
    and reorder-only diffs would inflate the shadow FP metric for most
    real-world extraction schemas.
    """
    schema = {
        "type": "object",
        "properties": {
            "ids": {
                "allOf": [{"type": "array", "uniqueItems": True, "items": {"type": "integer"}}],
                "description": "unique identifiers",
            },
        },
    }
    cached = {"ids": [1, 2, 3]}
    fresh = {"ids": [3, 2, 1]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True
    assert result.mode == "semantic"


def test_compare_semantic_unique_items_inside_any_of_nullable() -> None:
    """anyOf is how JSON Schema expresses ``Optional[list[...]]`` — must still honor uniqueItems."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "anyOf": [
                    {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                    {"type": "null"},
                ],
            },
        },
    }
    cached = {"tags": ["a", "b"]}
    fresh = {"tags": ["b", "a"]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


def test_compare_semantic_unique_items_inside_one_of() -> None:
    schema = {
        "type": "object",
        "properties": {
            "vals": {
                "oneOf": [
                    {"type": "array", "uniqueItems": True, "items": {"type": "integer"}},
                    {"type": "string"},
                ],
            },
        },
    }
    result = extraction_shadow.compare_results(
        {"vals": [1, 2]},
        {"vals": [2, 1]},
        schema=schema,
    )
    assert result.match is True


# ---------------------------------------------------------------------------
# compare_results — $ref resolution for Pydantic-generated schemas
# ---------------------------------------------------------------------------


def test_compare_semantic_unique_items_behind_ref() -> None:
    """Pydantic puts nested models under $defs and references them via $ref.

    Without $ref resolution, uniqueItems inside those definitions would be
    missed and reorder-only diffs would inflate the shadow FP metric.
    """
    schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                },
            },
        },
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/Item"},
        },
    }
    cached = {"item": {"tags": ["a", "b"]}}
    fresh = {"item": {"tags": ["b", "a"]}}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


def test_compare_semantic_unique_items_ref_cycle_does_not_skip_siblings() -> None:
    """Hitting a cycle must not short-circuit the rest of the current node's keys.

    Regression guard: the cycle check used to `return` early, which dropped
    sibling traversal (properties/items/combinators) on any node that
    contained a `$ref` already in the current expansion path.
    """
    # Both the outer "ids" ref and the sibling uniqueItems must be picked up.
    schema = {
        "$defs": {
            "Container": {
                "type": "object",
                "properties": {
                    # Self-referential: Container.parent → Container
                    "parent": {"$ref": "#/$defs/Container"},
                    # Sibling that depends on cycle-guard allowing traversal.
                    "ids": {"type": "array", "uniqueItems": True, "items": {"type": "integer"}},
                },
            },
        },
        "$ref": "#/$defs/Container",
    }
    # Reorder-only diff at ids — must match because uniqueItems is detected.
    cached = {"ids": [1, 2, 3], "parent": {"ids": [1, 2, 3]}}
    fresh = {"ids": [3, 2, 1], "parent": {"ids": [1, 2, 3]}}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


def test_compare_semantic_unique_items_ref_circular_safe() -> None:
    """Circular $ref must not cause infinite recursion in the collector."""
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "children": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"$ref": "#/$defs/Node"},
                    },
                },
            },
        },
        "$ref": "#/$defs/Node",
    }
    # Same-shape trees, inner children reordered — should match.
    cached = {"children": [{"children": []}, {"children": []}]}
    fresh = {"children": [{"children": []}, {"children": []}]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


def test_compare_semantic_unique_items_external_ref_ignored() -> None:
    """External $ref (non-#/) must be silently skipped, not crash."""
    schema = {
        "type": "object",
        "properties": {
            "ids": {"$ref": "https://example.com/schema.json#/Foo"},
        },
    }
    # External ref can't be resolved, so these compare strictly.
    # The important thing is the collector doesn't raise.
    result = extraction_shadow.compare_results({"ids": [1]}, {"ids": [1]}, schema=schema)
    assert result.match is True


# ---------------------------------------------------------------------------
# compare_results — bool vs int must be a mismatch (Python treats True == 1)
# ---------------------------------------------------------------------------


def test_compare_bool_vs_int_at_field_is_mismatch() -> None:
    """True vs 1 must be a diff — Python treats them as equal, the cache metric must not."""
    result = extraction_shadow.compare_results({"flag": True}, {"flag": 1}, schema=None)
    assert result.match is False
    assert "flag" in result.diff_summary


def test_compare_bool_vs_int_at_root_is_mismatch() -> None:
    result = extraction_shadow.compare_results(True, 1, schema=None)
    assert result.match is False


def test_compare_false_vs_zero_is_mismatch() -> None:
    result = extraction_shadow.compare_results({"f": False}, {"f": 0}, schema=None)
    assert result.match is False


def test_compare_int_vs_float_still_allowed_when_equal() -> None:
    """Int vs float with the same value should still match — that's a JSON-ism, not a real diff."""
    result = extraction_shadow.compare_results({"n": 1}, {"n": 1.0}, schema=None)
    assert result.match is True


# ---------------------------------------------------------------------------
# compare_results — uniqueItems set comparison must preserve multiplicity
# ---------------------------------------------------------------------------


def test_compare_semantic_unique_items_preserves_multiplicity() -> None:
    """uniqueItems set comparison must not collapse duplicates.

    If cached is ['a', 'a'] and fresh is ['a'], the payloads differ even
    though the underlying set is identical — treat it as a mismatch so the
    FP metric doesn't undercount real divergences.
    """
    schema = {
        "type": "object",
        "properties": {
            "docs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
    }
    result = extraction_shadow.compare_results(
        {"docs": ["a.pdf", "a.pdf"]},
        {"docs": ["a.pdf"]},
        schema=schema,
    )
    assert result.match is False
    assert "docs" in result.diff_summary


def test_compare_semantic_unique_items_multiplicity_match() -> None:
    """Same multiset with different order should still match."""
    schema = {
        "type": "object",
        "properties": {
            "docs": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
    }
    result = extraction_shadow.compare_results(
        {"docs": ["a.pdf", "b.pdf", "a.pdf"]},
        {"docs": ["a.pdf", "a.pdf", "b.pdf"]},
        schema=schema,
    )
    assert result.match is True


# ---------------------------------------------------------------------------
# compare_results — uniqueItems inside array items (nested arrays)
# ---------------------------------------------------------------------------


def test_compare_semantic_unique_items_nested_inside_array_items() -> None:
    """Schema: {groups: array<array(uniqueItems)>}. Inner reordering must match."""
    schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
            },
        },
    }
    cached = {"groups": [["a", "b"], ["c", "d"]]}
    fresh = {"groups": [["b", "a"], ["d", "c"]]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True
    assert result.mode == "semantic"


def test_compare_semantic_outer_array_without_unique_stays_order_sensitive_when_inner_is_unique() -> None:
    """Outer array (no uniqueItems) must NOT inherit set semantics from inner unique arrays."""
    schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
            },
        },
    }
    # Outer order changed — must be a diff even though inner elements are the same sets.
    cached = {"groups": [["a"], ["b"]]}
    fresh = {"groups": [["b"], ["a"]]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is False


def test_compare_semantic_unique_items_preserves_large_int_precision() -> None:
    """Large ints above 2^53 must not collapse to the same float in canonical form."""
    schema = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "uniqueItems": True, "items": {"type": "integer"}},
        },
    }
    # 2^53 + 1 cannot be represented exactly as a float64; naive float(int)
    # conversion would collapse these two distinct ids to the same value.
    result = extraction_shadow.compare_results(
        {"ids": [9007199254740992]},
        {"ids": [9007199254740993]},
        schema=schema,
    )
    assert result.match is False
    assert "ids" in result.diff_summary


def test_compare_semantic_unique_items_number_array_int_vs_float_match() -> None:
    """uniqueItems number array must treat 1 and 1.0 as equal, matching _diff_paths."""
    schema = {
        "type": "object",
        "properties": {
            "vals": {"type": "array", "uniqueItems": True, "items": {"type": "number"}},
        },
    }
    result = extraction_shadow.compare_results(
        {"vals": [1, 2]},
        {"vals": [1.0, 2.0]},
        schema=schema,
    )
    assert result.match is True


def test_compare_semantic_unique_items_close_but_unequal_number_array() -> None:
    """Arrays differing in one numeric value must register as a mismatch under set-equality."""
    schema = {
        "type": "object",
        "properties": {
            "vals": {"type": "array", "uniqueItems": True, "items": {"type": "number"}},
        },
    }
    result = extraction_shadow.compare_results(
        {"vals": [1, 2, 3]},
        {"vals": [1, 2, 4]},
        schema=schema,
    )
    assert result.match is False
    assert "vals" in result.diff_summary


def test_compare_semantic_unique_items_bool_still_distinct_from_int() -> None:
    """Even inside a uniqueItems array, True must not equal 1."""
    schema = {
        "type": "object",
        "properties": {
            "flags": {"type": "array", "uniqueItems": True, "items": {}},
        },
    }
    result = extraction_shadow.compare_results(
        {"flags": [True]},
        {"flags": [1]},
        schema=schema,
    )
    assert result.match is False


def test_compare_semantic_unique_items_with_nested_unique_objects_reorder_matches() -> None:
    """uniqueItems array of objects containing nested uniqueItems lists.

    Cached and fresh have identical elements modulo (a) outer reorder and
    (b) inner uniqueItems list reorder. Must match — the recursive semantic
    rules have to apply inside the set-equality comparison, not just at the
    top level.
    """
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "properties": {
                        "tags": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                    },
                },
            },
        },
    }
    cached = {"items": [{"tags": ["a", "b"]}, {"tags": ["c", "d"]}]}
    fresh = {"items": [{"tags": ["d", "c"]}, {"tags": ["b", "a"]}]}
    result = extraction_shadow.compare_results(cached, fresh, schema=schema)
    assert result.match is True


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _DummyLogCapture:
    """Structlog capture helper — records each call as (event, kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))


# ---------------------------------------------------------------------------
# run_shadow_comparison — exception sanitization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shadow_comparison_error_log_does_not_leak_exception_message() -> None:
    """Exception messages can contain raw LLM response payloads — log only the type."""
    captured = _DummyLogCapture()

    async def llm_call() -> Any:
        raise ValueError("SSN: 123-45-6789 leaked from model response")

    await extraction_shadow.run_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )

    assert len(captured.calls) == 1
    _event, fields = captured.calls[0]
    flat = " ".join(str(v) for v in fields.values())
    assert "123-45-6789" not in flat
    assert "SSN" not in flat
    # Class name is fine to log.
    assert "ValueError" in flat


# ---------------------------------------------------------------------------
# run_shadow_comparison — background runner
# ---------------------------------------------------------------------------


async def _fresh_ok(_result: Any) -> Any:
    return _result


@pytest.mark.asyncio
async def test_run_shadow_comparison_logs_match_event() -> None:
    cached = {"docs": ["a.pdf"]}
    captured = _DummyLogCapture()

    async def llm_call() -> Any:
        return {"docs": ["a.pdf"]}

    await extraction_shadow.run_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value=cached,
        cached_age_seconds=12.3,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )

    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    assert event == "extract_information.shadow_comparison"
    assert fields["status"] == "ok"
    assert fields["cache_key"] == "k1"
    assert fields["workflow_run_id"] == "wfr_1"
    assert fields["match"] is True
    assert fields["cached_age_seconds"] == 12.3
    assert "shadow_duration_ms" in fields
    assert fields["shadow_duration_ms"] >= 0
    assert fields["mode"] == "strict"


@pytest.mark.asyncio
async def test_run_shadow_comparison_logs_mismatch_with_diff() -> None:
    captured = _DummyLogCapture()

    async def llm_call() -> Any:
        return {"docs": ["a.pdf", "b.pdf"]}

    await extraction_shadow.run_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"docs": ["a.pdf"]},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )

    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    assert event == "extract_information.shadow_comparison"
    assert fields["match"] is False
    assert fields["diff_summary"]  # non-empty
    assert "docs" in fields["diff_summary"]


@pytest.mark.asyncio
async def test_run_shadow_comparison_swallows_llm_errors() -> None:
    """A failing LLM call must not propagate — shadow is best-effort and fire-and-forget."""
    captured = _DummyLogCapture()

    async def llm_call() -> Any:
        raise RuntimeError("LLM down")

    # Must not raise.
    await extraction_shadow.run_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )

    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    # Single consolidated event — filter on status=error to exclude from the FP metric.
    assert event == "extract_information.shadow_comparison"
    assert fields["status"] == "error"
    assert fields["cache_key"] == "k1"
    assert fields["error_type"] == "RuntimeError"
    assert fields["error_stage"] == "llm_call"


@pytest.mark.asyncio
async def test_run_shadow_comparison_uses_structlog_by_default(caplog: pytest.LogCaptureFixture) -> None:
    """If no logger is injected, the module's default structlog logger is used."""

    async def llm_call() -> Any:
        return {"a": 1}

    with caplog.at_level(logging.INFO):
        await extraction_shadow.run_shadow_comparison(
            cache_key="k1",
            workflow_run_id="wfr_1",
            cached_value={"a": 1},
            cached_age_seconds=0.0,
            llm_call=llm_call,
            schema=None,
        )

    # Default path should succeed without raising even when no logger override is provided.
    # We don't assert on caplog content (structlog routing varies by test env), only that
    # no exception escaped.


@pytest.mark.asyncio
async def test_schedule_shadow_check_runs_gate_in_background() -> None:
    """schedule_shadow_check must not await the gate on the caller's stack.

    Regression guard for the P1 where handler.py used to `await` the PostHog
    flag lookup directly, blocking cache-hit returns on the flag provider.
    """
    gate_release = asyncio.Event()
    captured = _DummyLogCapture()

    async def slow_gate() -> bool:
        await gate_release.wait()
        return True

    async def llm_call() -> Any:
        return {"a": 1}

    task = extraction_shadow.schedule_shadow_check(
        gate=slow_gate,
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )

    # Caller returns immediately — gate has not run yet.
    assert len(captured.calls) == 0

    gate_release.set()
    await task
    assert len(captured.calls) == 1
    assert captured.calls[0][1]["status"] == "ok"


@pytest.mark.asyncio
async def test_schedule_shadow_check_skips_when_gate_returns_false() -> None:
    captured = _DummyLogCapture()

    async def gate() -> bool:
        return False

    async def llm_call() -> Any:  # pragma: no cover — must not be called
        raise AssertionError("LLM should not be called when gate is False")

    task = extraction_shadow.schedule_shadow_check(
        gate=gate,
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )
    await task
    # One info log confirming the gate evaluated to False — used as the
    # sampling-rate denominator (status:skipped) alongside status:ok/error.
    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    assert event == "extract_information.shadow_comparison"
    assert fields["status"] == "skipped"


@pytest.mark.asyncio
async def test_schedule_shadow_check_swallows_gate_errors() -> None:
    captured = _DummyLogCapture()

    async def gate() -> bool:
        raise RuntimeError("posthog unavailable")

    async def llm_call() -> Any:  # pragma: no cover — must not be called
        raise AssertionError("LLM should not be called when gate raises")

    task = extraction_shadow.schedule_shadow_check(
        gate=gate,
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )
    await task  # must not raise
    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    assert event == "extract_information.shadow_comparison"
    assert fields["status"] == "error"
    assert fields["error_stage"] == "gate"
    assert fields["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_schedule_returns_none_and_warns_when_cap_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety valve: when _PENDING_SHADOW_TASKS is full, schedule must skip and warn.

    Protects the hot path from LLM-provider rate-limit contention when shadow
    tasks pile up (slow provider, sustained cache-hit burst).
    """

    # Fill the pending set with already-done tasks that won't be pruned by _prune_pending()
    # — simulates an in-flight backlog rather than leaked done tasks.
    class _PendingMarker:
        def done(self) -> bool:
            return False

    fake_pending: set[Any] = {_PendingMarker() for _ in range(extraction_shadow._MAX_PENDING_SHADOWS)}
    monkeypatch.setattr(extraction_shadow, "_PENDING_SHADOW_TASKS", fake_pending)

    captured = _DummyLogCapture()

    async def llm_call() -> Any:  # pragma: no cover — must not run when capped
        raise AssertionError("shadow LLM must not run when the cap is hit")

    task = extraction_shadow.schedule_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=llm_call,
        schema=None,
        logger=captured,
    )
    assert task is None
    assert len(captured.calls) == 1
    event, fields = captured.calls[0]
    assert event == "shadow_task_cap_reached"
    assert fields["pending"] == extraction_shadow._MAX_PENDING_SHADOWS


@pytest.mark.asyncio
async def test_schedule_shadow_comparison_does_not_block_caller() -> None:
    """schedule_shadow_comparison must return immediately; background task executes after."""
    release = asyncio.Event()
    observed: list[bool] = []

    async def slow_llm_call() -> Any:
        # Block until the test tells us to proceed, proving the caller isn't awaiting us.
        await release.wait()
        return {"a": 1}

    captured = _DummyLogCapture()

    task = extraction_shadow.schedule_shadow_comparison(
        cache_key="k1",
        workflow_run_id="wfr_1",
        cached_value={"a": 1},
        cached_age_seconds=0.0,
        llm_call=slow_llm_call,
        schema=None,
        logger=captured,
    )

    # Caller returns immediately — no logs yet.
    observed.append(task is not None)
    assert len(captured.calls) == 0

    release.set()
    await task
    assert len(captured.calls) == 1
    assert captured.calls[0][1]["match"] is True
