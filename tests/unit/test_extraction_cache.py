"""Unit tests for the extract-information result cache."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.cache import extraction_cache


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    extraction_cache._reset_for_tests()
    yield
    extraction_cache._reset_for_tests()


def _key(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "element_tree": "<html><body>docs</body></html>",
        "extracted_text": "Document list",
        "current_url": "https://example.com/docs",
        "data_extraction_goal": "Extract document list",
        "extracted_information_schema": {"type": "object", "properties": {"docs": {"type": "array"}}},
        "navigation_payload": {"user": "alice"},
    }
    defaults.update(overrides)
    return extraction_cache.compute_cache_key(**defaults)  # type: ignore[arg-type]


def test_identical_inputs_produce_identical_key() -> None:
    assert _key() == _key()


def test_key_changes_when_element_tree_changes() -> None:
    assert _key() != _key(element_tree="<html><body>different</body></html>")


def test_key_changes_when_schema_changes() -> None:
    assert _key() != _key(extracted_information_schema={"type": "object", "properties": {}})


def test_key_changes_when_extracted_text_changes() -> None:
    assert _key() != _key(extracted_text="Something else entirely")


def test_key_changes_when_url_changes() -> None:
    assert _key() != _key(current_url="https://example.com/other")


def test_key_changes_when_error_code_mapping_changes() -> None:
    # RFC review: error_code_mapping is rendered into the prompt,
    # so it must be part of the key.
    assert _key(error_code_mapping={"E1": "oops"}) != _key(error_code_mapping={"E1": "different"})


def test_key_changes_when_previous_extracted_information_changes() -> None:
    # RFC review: previous_extracted_information is rendered into the prompt as
    # prior context. In a loop where each iteration is a fresh task so
    # this is None on step 1 — the cross-iteration cache hits still land —
    # but if an intra-task second-step extraction happens, the key must change.
    assert _key(previous_extracted_information=None) != _key(previous_extracted_information={"prior": "value"})


def test_key_changes_when_llm_key_changes() -> None:
    # RFC review: include llm_key so swapping models forces a fresh extraction
    # once this cache is backed by an off-process store.
    assert _key(llm_key="gpt-4o") != _key(llm_key="claude-sonnet-4-6")


def test_key_is_stable_across_equivalent_schema_dict_orderings() -> None:
    schema_a = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    schema_b = {"properties": {"b": {"type": "string"}, "a": {"type": "string"}}, "type": "object"}
    assert _key(extracted_information_schema=schema_a) == _key(extracted_information_schema=schema_b)


def test_lookup_returns_miss_on_empty_cache() -> None:
    result = extraction_cache.lookup("wfr_1", _key())
    assert result.hit is False
    assert result.value is None
    assert result.age_seconds is None
    assert result.fallback_reason == extraction_cache.FALLBACK_FIRST_CALL_IN_RUN
    assert result.scope == extraction_cache.SCOPE_RUN


def test_store_then_lookup_returns_hit_with_age() -> None:
    key = _key()
    extraction_cache.store("wfr_1", key, {"docs": ["a.pdf"]})
    result = extraction_cache.lookup("wfr_1", key)
    assert result.hit is True
    assert result.value == {"docs": ["a.pdf"]}
    assert result.age_seconds is not None
    assert result.age_seconds >= 0.0
    assert result.fallback_reason is None
    assert result.scope == extraction_cache.SCOPE_RUN


def test_lookup_returns_key_not_found_when_run_exists_but_key_does_not() -> None:
    """A run with other entries but missing this key must report key_not_found,
    not first_call_in_run — downstream metrics use this split to distinguish
    unavoidable first-call misses from potential normalization opportunities."""
    extraction_cache.store("wfr_1", _key(current_url="https://example.com/A"), {"a": 1})
    result = extraction_cache.lookup("wfr_1", _key(current_url="https://example.com/B"))
    assert result.hit is False
    assert result.value is None
    assert result.fallback_reason == extraction_cache.FALLBACK_KEY_NOT_FOUND


def test_cache_is_isolated_per_workflow_run_id() -> None:
    key = _key()
    extraction_cache.store("wfr_1", key, {"docs": ["a.pdf"]})
    result = extraction_cache.lookup("wfr_2", key)
    assert result.hit is False
    assert result.fallback_reason == extraction_cache.FALLBACK_FIRST_CALL_IN_RUN


def test_empty_workflow_run_id_bypasses_cache() -> None:
    key = _key()
    extraction_cache.store(None, key, {"docs": ["a.pdf"]})
    assert extraction_cache.lookup(None, key) is None


def test_clear_workflow_run_drops_entries() -> None:
    key = _key()
    extraction_cache.store("wfr_1", key, {"docs": ["a.pdf"]})
    extraction_cache.clear_workflow_run("wfr_1")
    assert extraction_cache.lookup("wfr_1", key).hit is False


def test_fifo_eviction_when_run_cache_is_full() -> None:
    # Insert MAX + 1 distinct entries; the oldest should be evicted.
    max_entries = extraction_cache._MAX_ENTRIES_PER_RUN
    first_key = _key(current_url="https://example.com/0")
    extraction_cache.store("wfr_1", first_key, {"i": 0})
    for i in range(1, max_entries + 1):
        k = _key(current_url=f"https://example.com/{i}")
        extraction_cache.store("wfr_1", k, {"i": i})

    assert extraction_cache.lookup("wfr_1", first_key).hit is False
    last_key = _key(current_url=f"https://example.com/{max_entries}")
    last_result = extraction_cache.lookup("wfr_1", last_key)
    assert last_result.hit is True
    assert last_result.value == {"i": max_entries}


def test_store_and_lookup_list_result() -> None:
    """Extraction schemas with array roots produce list results — these must be cached too."""
    key = _key()
    extraction_cache.store("wfr_1", key, [{"doc": "a.pdf"}, {"doc": "b.pdf"}])
    result = extraction_cache.lookup("wfr_1", key)
    assert result.hit is True
    assert result.value == [{"doc": "a.pdf"}, {"doc": "b.pdf"}]


def test_store_and_lookup_string_result() -> None:
    """Some extractions return a plain string — these must be cached too."""
    key = _key()
    extraction_cache.store("wfr_1", key, "plain text extraction")
    result = extraction_cache.lookup("wfr_1", key)
    assert result.hit is True
    assert result.value == "plain text extraction"


def test_lookup_age_seconds_is_monotonic_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """age_seconds should reflect elapsed time between store() and lookup()."""
    fake_now = [1_000.0]

    def _fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr(extraction_cache.time, "monotonic", _fake_monotonic)

    key = _key()
    extraction_cache.store("wfr_1", key, {"docs": []})

    fake_now[0] = 1_012.5
    result = extraction_cache.lookup("wfr_1", key)
    assert result.hit is True
    assert result.age_seconds == pytest.approx(12.5)


def test_key_changes_when_local_date_changes() -> None:
    """Date-relative extraction goals must miss when the date changes (midnight boundary)."""
    assert _key(local_datetime="2026-04-10T00:00:00") != _key(local_datetime="2026-04-11T00:00:00")


def test_key_stable_across_same_date_different_times() -> None:
    """Same date with different timestamps should produce the same key (truncated to date)."""
    assert _key(local_datetime="2026-04-10T08:30:00.123456") == _key(local_datetime="2026-04-10T23:59:59.999999")


def test_none_and_empty_string_produce_different_keys() -> None:
    """None and '' are distinct states and must not collide in the cache key."""
    assert _key(extracted_text=None) != _key(extracted_text="")
    assert _key(current_url=None) != _key(current_url="")
    assert _key(data_extraction_goal=None) != _key(data_extraction_goal="")


def test_rendered_prompt_path_identical_prompts_hit() -> None:
    """Two calls with the same rendered prompt should produce the same key."""
    prompt = "Extract docs.\nCurrent datetime, ISO format:\n2026-04-10T08:00:00\nDone."
    key1 = extraction_cache.compute_cache_key(rendered_prompt=prompt, llm_key="gpt-4o")
    key2 = extraction_cache.compute_cache_key(rendered_prompt=prompt, llm_key="gpt-4o")
    assert key1 == key2


def test_rendered_prompt_path_different_prompts_miss() -> None:
    """Different rendered prompts must produce different keys."""
    p1 = "Extract docs from list A\n2026-04-10T08:00:00"
    p2 = "Extract docs from list B\n2026-04-10T08:00:00"
    assert extraction_cache.compute_cache_key(rendered_prompt=p1) != extraction_cache.compute_cache_key(
        rendered_prompt=p2
    )


def test_rendered_prompt_normalizes_timestamp_line() -> None:
    """Same-day prompts with different ISO timestamps should hash identically."""
    p1 = "Header\n2026-04-10T08:30:15.123456\nFooter"
    p2 = "Header\n2026-04-10T23:59:59.999999\nFooter"
    assert extraction_cache.compute_cache_key(rendered_prompt=p1) == extraction_cache.compute_cache_key(
        rendered_prompt=p2
    )


def test_rendered_prompt_midnight_crossing_produces_miss() -> None:
    """Different dates in the rendered prompt timestamp must produce different keys."""
    p1 = "Header\n2026-04-10T23:59:59\nFooter"
    p2 = "Header\n2026-04-11T00:00:01\nFooter"
    assert extraction_cache.compute_cache_key(rendered_prompt=p1) != extraction_cache.compute_cache_key(
        rendered_prompt=p2
    )


def test_rendered_prompt_llm_key_affects_hash() -> None:
    """Same rendered prompt with different llm_key should produce different keys."""
    prompt = "Extract docs\n2026-04-10T08:00:00"
    k1 = extraction_cache.compute_cache_key(rendered_prompt=prompt, llm_key="gpt-4o")
    k2 = extraction_cache.compute_cache_key(rendered_prompt=prompt, llm_key="claude-sonnet-4-6")
    assert k1 != k2


def test_lookup_refreshes_lru_position() -> None:
    """A cache hit should refresh the run's LRU position, preventing eviction."""
    max_runs = extraction_cache._MAX_WORKFLOW_RUNS
    key = _key()

    # Fill the global cache to capacity: wfr_oldest first, then wfr_1..wfr_(N-1).
    extraction_cache.store("wfr_oldest", key, {"v": 0})
    for i in range(1, max_runs):
        extraction_cache.store(f"wfr_{i}", key, {"v": i})

    # Cache is at capacity. wfr_oldest is the LRU candidate.
    # A lookup() hit should refresh its position to most-recent.
    refreshed = extraction_cache.lookup("wfr_oldest", key)
    assert refreshed.hit is True
    assert refreshed.value == {"v": 0}

    # Adding one more run triggers eviction. Without the LRU refresh,
    # wfr_oldest would be evicted; with it, wfr_1 (now the oldest) goes.
    extraction_cache.store("wfr_new", key, {"v": 999})

    oldest_after = extraction_cache.lookup("wfr_oldest", key)
    assert oldest_after.hit is True
    assert oldest_after.value == {"v": 0}
    assert extraction_cache.lookup("wfr_1", key).hit is False  # evicted
