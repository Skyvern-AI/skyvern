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
        "call_path": "test",
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


# ---------------------------------------------------------------------------
# _canonical_url primitive
# ---------------------------------------------------------------------------


class TestCanonicalUrl:
    def test_returns_none_for_none(self) -> None:
        assert extraction_cache._canonical_url(None) is None

    def test_returns_empty_for_empty(self) -> None:
        assert extraction_cache._canonical_url("") == ""

    def test_leaves_simple_url_unchanged(self) -> None:
        assert extraction_cache._canonical_url("https://example.com/docs") == "https://example.com/docs"

    def test_sorts_query_params_by_key(self) -> None:
        assert extraction_cache._canonical_url("https://x/y?b=2&a=1") == "https://x/y?a=1&b=2"

    def test_redacts_nonce_param_values_and_preserves_keys(self) -> None:
        """Nonce values are replaced with a sentinel but keys are preserved so
        presence/absence of the param still differentiates cache keys.
        """
        out = extraction_cache._canonical_url("https://x/y?a=1&_csrf=abc&b=2")
        assert "_csrf=__NONCE__" in out
        assert "a=1" in out and "b=2" in out

    def test_same_nonce_key_different_values_produce_same_canonical(self) -> None:
        """Two URLs that differ only in a nonce value must hash identically."""
        a = extraction_cache._canonical_url("https://x/y?_csrf=abc")
        b = extraction_cache._canonical_url("https://x/y?_csrf=xyz")
        assert a == b

    def test_nonce_key_absent_vs_present_produce_different_canonical(self) -> None:
        """A URL with the nonce key absent must canonicalize differently than one with the key present."""
        with_nonce = extraction_cache._canonical_url("https://x/y?_csrf=abc")
        without = extraction_cache._canonical_url("https://x/y")
        assert with_nonce != without

    def test_empty_nonce_value_does_not_collide_with_populated_value(self) -> None:
        """`?_csrf=` (empty) must canonicalize differently than `?_csrf=abc`."""
        empty = extraction_cache._canonical_url("https://x/y?_csrf=")
        populated = extraction_cache._canonical_url("https://x/y?_csrf=abc")
        assert empty != populated
        assert "_csrf=__NONCE__" not in empty

    def test_bare_flag_does_not_collide_with_empty_value(self) -> None:
        """`?flag` (no `=`) must canonicalize differently than `?flag=`."""
        bare = extraction_cache._canonical_url("https://x/y?flag")
        empty = extraction_cache._canonical_url("https://x/y?flag=")
        assert bare != empty
        assert bare.endswith("?flag")
        assert empty.endswith("?flag=")

    def test_preserves_fragment(self) -> None:
        """SPAs with hash routing encode page identity in the fragment (e.g. `#/orders/123` vs
        `#/orders/456`); stripping the fragment would collapse structurally-different pages."""
        assert extraction_cache._canonical_url("https://x/y?a=1#section") == "https://x/y?a=1#section"

    def test_different_fragments_produce_different_canonical(self) -> None:
        """Hash-routed SPA URLs must canonicalize distinctly when the fragment differs."""
        a = extraction_cache._canonical_url("https://x/y#/orders/123")
        b = extraction_cache._canonical_url("https://x/y#/orders/456")
        assert a != b

    def test_preserves_duplicate_keys_in_order(self) -> None:
        # Repeated keys can be semantically ordered (first-wins handlers,
        # ordered multi-sort). Python's stable sort preserves insertion order
        # within the same key.
        url = "https://x/y?sort=price&sort=rating"
        assert extraction_cache._canonical_url(url) == "https://x/y?sort=price&sort=rating"

    def test_trailing_punctuation_is_not_stripped(self) -> None:
        # _canonical_url operates on pre-parsed URL strings, not on URLs
        # embedded in prose. Callers pass `current_url` directly.
        assert extraction_cache._canonical_url("https://x/y.") == "https://x/y."

    def test_malformed_url_returns_input_unchanged(self) -> None:
        # Never raise — cache lookup must degrade gracefully.
        assert extraction_cache._canonical_url("not a url at all") == "not a url at all"

    def test_case_insensitive_nonce_match_redacts_value(self) -> None:
        """Uppercase nonce keys are still matched; the value is redacted, the key preserved."""
        out = extraction_cache._canonical_url("https://x/y?CSRF=abc&a=1")
        assert "CSRF=__NONCE__" in out
        assert "a=1" in out


# ---------------------------------------------------------------------------
# _canonical_element_tree primitive
# ---------------------------------------------------------------------------


class TestCanonicalElementTree:
    def test_returns_none_for_none(self) -> None:
        assert extraction_cache._canonical_element_tree(None) is None

    def test_returns_empty_for_empty(self) -> None:
        assert extraction_cache._canonical_element_tree("") == ""

    def test_scrubs_uuid_in_id_attribute(self) -> None:
        h1 = '<div id="3f8a9b12-1234-4678-9abc-def012345678">x</div>'
        h2 = '<div id="fedcba98-8765-4321-abcd-123456789abc">x</div>'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_scrubs_random_hex_suffix_in_id_attribute(self) -> None:
        h1 = '<div id="row-abc123def">x</div>'
        h2 = '<div id="row-fedcba987">x</div>'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_scrubs_data_testid(self) -> None:
        h1 = '<button data-testid="btn-1a2b3c4d">go</button>'
        h2 = '<button data-testid="btn-5e6f7a8b">go</button>'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_leaves_class_and_href_untouched(self) -> None:
        # class and href carry semantic weight — they must differentiate pages.
        h1 = '<a class="btn primary" href="/docs">go</a>'
        h2 = '<a class="btn danger" href="/docs">go</a>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_scrubs_csrf_input_value(self) -> None:
        h1 = '<input name="_csrf" value="abc123">'
        h2 = '<input name="_csrf" value="zyx987">'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_scrubs_csrf_meta_content(self) -> None:
        h1 = '<meta name="csrf-token" content="abc123">'
        h2 = '<meta name="csrf-token" content="zyx987">'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_canonical_element_tree_returns_string_for_valid_html(self) -> None:
        # selectolax is permissive enough that we can't reliably force its
        # parser to raise from pytest; the except-path fallback is exercised
        # indirectly by the None/empty-string guards at the top of the function.
        assert extraction_cache._canonical_element_tree("<div>ok</div>") is not None

    def test_preserves_text_content(self) -> None:
        out = extraction_cache._canonical_element_tree('<div id="x-abc123def">hello world</div>')
        assert "hello world" in out

    def test_different_text_produces_different_output(self) -> None:
        out1 = extraction_cache._canonical_element_tree("<div>alpha</div>")
        out2 = extraction_cache._canonical_element_tree("<div>beta</div>")
        assert out1 != out2

    def test_scrubs_csrf_input_case_insensitive(self) -> None:
        """CSRF <input name=...> match must be case-insensitive, matching prior regex behavior."""
        h1 = '<input name="CSRF_TOKEN" value="abc123">'
        h2 = '<input name="CSRF_TOKEN" value="zyx987">'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_scrubs_csrf_meta_case_insensitive(self) -> None:
        """CSRF <meta name=...> match must be case-insensitive, matching prior regex behavior."""
        h1 = '<meta name="CSRF-TOKEN" content="abc123">'
        h2 = '<meta name="CSRF-TOKEN" content="zyx987">'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)

    def test_preserves_semantic_input_name_values(self) -> None:
        """<input name=...> values carry field-name semantics (not transient IDs).
        Two forms with different input names must NOT collapse to the same canonical.
        """
        h1 = '<form><input name="company_name" type="text"><button>Go</button></form>'
        h2 = '<form><input name="contact_phone" type="text"><button>Go</button></form>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_preserves_stable_business_ids_in_suspect_attrs(self) -> None:
        """Semantic identifiers without transient patterns must survive canonicalization.

        Only UUIDs and random-looking hex suffixes are redacted inside suspect
        attributes; stable business IDs like id='submit-button' must differentiate.
        """
        h1 = '<button id="submit-button">go</button>'
        h2 = '<button id="cancel-button">go</button>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_preserves_numeric_only_suffix_in_suspect_attrs(self) -> None:
        """Purely numeric suffixes (e.g. 'order-123456') are stable business IDs; don't collapse them."""
        h1 = '<div id="order-123456">go</div>'
        h2 = '<div id="order-987654">go</div>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_preserves_hex_letter_only_english_words_in_suspect_attrs(self) -> None:
        """Hex-letter-only strings like 'facade' or 'decade' are English words, not random IDs."""
        h1 = '<div id="zone-facade">go</div>'
        h2 = '<div id="zone-decade">go</div>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_preserves_non_v4_uuid_in_suspect_attrs(self) -> None:
        """v1/v3/v5 UUIDs are deterministic / namespace-based and can be stable business keys."""
        # v1 UUID (version nibble = 1) — must NOT be collapsed
        h1 = '<div id="3f8a9b12-1234-1678-9abc-def012345678">x</div>'
        h2 = '<div id="fedcba98-8765-1321-abcd-123456789abc">x</div>'
        assert extraction_cache._canonical_element_tree(h1) != extraction_cache._canonical_element_tree(h2)

    def test_scrubs_attr_case_insensitively(self) -> None:
        """Even if a parser surfaces an uppercase attribute name, it must still match the suspect set."""
        # selectolax 0.3.34 normalizes to lowercase, but we want robustness if that ever changes.
        # Exercise via direct set membership: this test pins the lower() call.
        h1 = '<div ID="3f8a9b12-1234-4678-9abc-def012345678">x</div>'
        h2 = '<div ID="fedcba98-8765-4321-abcd-123456789abc">x</div>'
        assert extraction_cache._canonical_element_tree(h1) == extraction_cache._canonical_element_tree(h2)


# ---------------------------------------------------------------------------
# compute_cache_key — structured-path canonicalization integration
# ---------------------------------------------------------------------------


def test_key_stable_across_nonce_params_in_url() -> None:
    """current_url with different nonce param values should still hit."""
    assert _key(current_url="https://x/y?a=1&_csrf=abc") == _key(current_url="https://x/y?a=1&_csrf=xyz")


def test_key_stable_across_uuid_in_element_tree() -> None:
    """element_tree with different UUIDs in id= attributes should still hit."""
    h1 = '<div id="3f8a9b12-1234-4678-9abc-def012345678">doc</div>'
    h2 = '<div id="fedcba98-8765-4321-abcd-123456789abc">doc</div>'
    assert _key(element_tree=h1) == _key(element_tree=h2)


def test_key_stable_across_csrf_token_in_element_tree() -> None:
    """element_tree with different CSRF tokens should still hit."""
    h1 = '<input name="_csrf" value="abc123">'
    h2 = '<input name="_csrf" value="zyx987">'
    assert _key(element_tree=h1) == _key(element_tree=h2)


def test_key_stable_across_iso_timestamps_in_extracted_text() -> None:
    """extracted_text with same-day ISO timestamps should still hit."""
    t1 = "Report\n2026-04-10T08:30:15.123456\nEnd"
    t2 = "Report\n2026-04-10T23:59:59.999999\nEnd"
    assert _key(extracted_text=t1) == _key(extracted_text=t2)


def test_key_changes_across_different_dates_in_extracted_text() -> None:
    """Midnight crossing in extracted_text must produce a different key."""
    t1 = "Report\n2026-04-10T23:59:59\nEnd"
    t2 = "Report\n2026-04-11T00:00:01\nEnd"
    assert _key(extracted_text=t1) != _key(extracted_text=t2)


def test_call_path_discriminator_isolates_otherwise_identical_keys() -> None:
    """Different call_paths must produce different keys even when every other
    input is identical — guards against silent cross-path cache hits (e.g.
    script path replaying an agent-path extraction result)."""
    assert _key(call_path="handler") != _key(call_path="script")
    assert _key(call_path="handler") != _key(call_path="agent")
    assert _key(call_path="script") != _key(call_path="agent")


def test_key_stable_across_iso_timestamps_in_data_extraction_goal() -> None:
    """Same-day ISO timestamps in the goal (e.g. 'extract updated after <ts>')
    must not cause per-second key churn."""
    g1 = "Extract records updated after\n2026-04-10T08:30:15.123456\nonward"
    g2 = "Extract records updated after\n2026-04-10T23:59:59.999999\nonward"
    assert _key(data_extraction_goal=g1) == _key(data_extraction_goal=g2)
