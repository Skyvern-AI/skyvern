"""Unit tests for the extract-information result cache."""

from __future__ import annotations

from collections.abc import Callable

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


def _canonical_element_tree(html: str | None) -> str | None:
    return extraction_cache._canonical_element_tree(html)


def test_cache_key_changes_for_output_affecting_inputs() -> None:
    cases: tuple[tuple[str, str, str], ...] = (
        ("element-tree", _key(), _key(element_tree="<html><body>different</body></html>")),
        ("schema", _key(), _key(extracted_information_schema={"type": "object", "properties": {}})),
        ("extracted-text", _key(), _key(extracted_text="Something else entirely")),
        ("url", _key(), _key(current_url="https://example.com/other")),
        (
            "error-code-mapping",
            _key(error_code_mapping={"E1": "oops"}),
            _key(error_code_mapping={"E1": "different"}),
        ),
        (
            "previous-extracted-information",
            _key(previous_extracted_information=None),
            _key(previous_extracted_information={"prior": "value"}),
        ),
        ("llm-key", _key(llm_key="gpt-4o"), _key(llm_key="claude-sonnet-4-6")),
        (
            "different-extracted-text-dates",
            _key(extracted_text="Report\n2026-04-10T23:59:59\nEnd"),
            _key(extracted_text="Report\n2026-04-11T00:00:01\nEnd"),
        ),
        ("call-path-handler-script", _key(call_path="handler"), _key(call_path="script")),
        ("call-path-handler-agent", _key(call_path="handler"), _key(call_path="agent")),
        ("call-path-script-agent", _key(call_path="script"), _key(call_path="agent")),
    )

    for case_id, first, second in cases:
        assert first != second, case_id


def test_cache_key_is_stable_for_equivalent_inputs() -> None:
    schema_a = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    schema_b = {"properties": {"b": {"type": "string"}, "a": {"type": "string"}}, "type": "object"}
    uuid_html_a = '<div id="3f8a9b12-1234-4678-9abc-def012345678">doc</div>'
    uuid_html_b = '<div id="fedcba98-8765-4321-abcd-123456789abc">doc</div>'
    csrf_html_a = '<input name="_csrf" value="abc123">'
    csrf_html_b = '<input name="_csrf" value="zyx987">'
    extracted_text_a = "Report\n2026-04-10T08:30:15.123456\nEnd"
    extracted_text_b = "Report\n2026-04-10T23:59:59.999999\nEnd"
    goal_a = "Extract records updated after\n2026-04-10T08:30:15.123456\nonward"
    goal_b = "Extract records updated after\n2026-04-10T23:59:59.999999\nonward"
    cases: tuple[tuple[str, str, str], ...] = (
        ("identical-inputs", _key(), _key()),
        ("schema-order", _key(extracted_information_schema=schema_a), _key(extracted_information_schema=schema_b)),
        (
            "nonce-url-values",
            _key(current_url="https://x/y?a=1&_csrf=abc"),
            _key(current_url="https://x/y?a=1&_csrf=xyz"),
        ),
        ("uuid-element-tree", _key(element_tree=uuid_html_a), _key(element_tree=uuid_html_b)),
        ("csrf-element-tree", _key(element_tree=csrf_html_a), _key(element_tree=csrf_html_b)),
        (
            "same-day-extracted-text-timestamps",
            _key(extracted_text=extracted_text_a),
            _key(extracted_text=extracted_text_b),
        ),
        ("same-day-goal-timestamps", _key(data_extraction_goal=goal_a), _key(data_extraction_goal=goal_b)),
    )

    for case_id, first, second in cases:
        assert first == second, case_id


def test_compute_cache_key_rejects_legacy_local_datetime_kwarg() -> None:
    with pytest.raises(TypeError):
        extraction_cache.compute_cache_key(call_path="test", local_datetime="2026-04-10T00:00:00")  # type: ignore[call-arg]


def test_none_and_empty_string_produce_different_keys() -> None:
    cases: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
        ("extracted-text", {"extracted_text": None}, {"extracted_text": ""}),
        ("current-url", {"current_url": None}, {"current_url": ""}),
        ("data-extraction-goal", {"data_extraction_goal": None}, {"data_extraction_goal": ""}),
    )

    for case_id, none_override, empty_override in cases:
        assert _key(**none_override) != _key(**empty_override), case_id


def test_lookup_miss_matrix() -> None:
    key = _key()

    first_call = extraction_cache.lookup("wfr_1", key)
    assert first_call.hit is False
    assert first_call.value is None
    assert first_call.age_seconds is None
    assert first_call.fallback_reason == extraction_cache.FALLBACK_FIRST_CALL_IN_RUN
    assert first_call.scope == extraction_cache.SCOPE_RUN

    extraction_cache.store("wfr_1", _key(current_url="https://example.com/A"), {"a": 1})
    missing_key = extraction_cache.lookup("wfr_1", _key(current_url="https://example.com/B"))
    assert missing_key.hit is False
    assert missing_key.value is None
    assert missing_key.fallback_reason == extraction_cache.FALLBACK_KEY_NOT_FOUND

    isolated_run = extraction_cache.lookup("wfr_2", _key(current_url="https://example.com/A"))
    assert isolated_run.hit is False
    assert isolated_run.fallback_reason == extraction_cache.FALLBACK_FIRST_CALL_IN_RUN


@pytest.mark.parametrize(
    ("value", "key_suffix"),
    (
        pytest.param({"docs": ["a.pdf"]}, "dict", id="dict-result"),
        pytest.param([{"doc": "a.pdf"}, {"doc": "b.pdf"}], "list", id="list-result"),
        pytest.param("plain text extraction", "string", id="string-result"),
    ),
)
def test_store_then_lookup_returns_hit_with_supported_result_shapes(value: object, key_suffix: str) -> None:
    key = _key(current_url=f"https://example.com/{key_suffix}")
    extraction_cache.store("wfr_1", key, value)

    result = extraction_cache.lookup("wfr_1", key)
    assert result.hit is True
    assert result.value == value
    assert result.age_seconds is not None
    assert result.age_seconds >= 0.0
    assert result.fallback_reason is None
    assert result.scope == extraction_cache.SCOPE_RUN


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


def test_lookup_age_seconds_is_monotonic_delta(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_invalidate_key_matrix() -> None:
    key_a = _key()
    key_b = _key(current_url="https://example.com/other")
    extraction_cache.store("wfr_1", key_a, {"v": "a"})
    extraction_cache.store("wfr_1", key_b, {"v": "b"})

    removed = extraction_cache.invalidate_key("wfr_1", key_a)
    assert removed is True
    assert extraction_cache.lookup("wfr_1", key_a).hit is False
    hit_b = extraction_cache.lookup("wfr_1", key_b)
    assert hit_b.hit is True
    assert hit_b.value == {"v": "b"}

    assert extraction_cache.invalidate_key("wfr_1", "nonexistent-key") is False
    assert extraction_cache.invalidate_key("wfr_missing", _key()) is False
    assert extraction_cache.invalidate_key(None, _key()) is False
    assert extraction_cache.invalidate_key("", _key()) is False


def test_lookup_refreshes_lru_position() -> None:
    max_runs = extraction_cache._MAX_WORKFLOW_RUNS
    key = _key()

    extraction_cache.store("wfr_oldest", key, {"v": 0})
    for i in range(1, max_runs):
        extraction_cache.store(f"wfr_{i}", key, {"v": i})

    refreshed = extraction_cache.lookup("wfr_oldest", key)
    assert refreshed.hit is True
    assert refreshed.value == {"v": 0}

    extraction_cache.store("wfr_new", key, {"v": 999})

    oldest_after = extraction_cache.lookup("wfr_oldest", key)
    assert oldest_after.hit is True
    assert oldest_after.value == {"v": 0}
    assert extraction_cache.lookup("wfr_1", key).hit is False


@pytest.mark.parametrize(
    "cases",
    (
        pytest.param(
            (
                (None, None),
                ("", ""),
            ),
            id="empty-inputs",
        ),
        pytest.param(
            (
                ("https://example.com/docs", "https://example.com/docs"),
                ("https://x/y.", "https://x/y."),
                ("not a url at all", "not a url at all"),
            ),
            id="simple-urls",
        ),
        pytest.param(
            (
                ("https://x/y?b=2&a=1", "https://x/y?a=1&b=2"),
                ("https://x/y?sort=price&sort=rating", "https://x/y?sort=price&sort=rating"),
            ),
            id="query-order",
        ),
        pytest.param(
            (
                ("https://x/y?_csrf=abc", "https://x/y?_csrf=__NONCE__"),
                ("https://x/y?_csrf=xyz", "https://x/y?_csrf=__NONCE__"),
            ),
            id="nonce-value-equivalence",
        ),
        pytest.param(
            (
                ("https://x/y?_csrf=", "https://x/y?_csrf="),
                ("https://x/y?_csrf=abc", "https://x/y?_csrf=__NONCE__"),
            ),
            id="empty-nonce-value",
        ),
        pytest.param(
            (
                ("https://x/y?flag", "https://x/y?flag"),
                ("https://x/y?flag=", "https://x/y?flag="),
            ),
            id="bare-vs-empty-flag",
        ),
        pytest.param(
            (
                ("https://x/y?a=1#section", "https://x/y?a=1#section"),
                ("https://x/y#/orders/123", "https://x/y#/orders/123"),
                ("https://x/y#/orders/456", "https://x/y#/orders/456"),
            ),
            id="fragments",
        ),
        pytest.param(
            (
                ("https://x/y?a=1&b=2", "https://x/y?a=1&b=2"),
                ("https://x/y?b=2&a=1", "https://x/y?a=1&b=2"),
            ),
            id="already-canonical-vs-unsorted",
        ),
    ),
)
def test_canonical_url_normalization_matrix(cases: tuple[tuple[str | None, str | None], ...]) -> None:
    for raw, expected in cases:
        assert extraction_cache._canonical_url(raw) == expected


def test_canonical_url_redacts_nonce_param_values_and_preserves_keys() -> None:
    out = extraction_cache._canonical_url("https://x/y?a=1&_csrf=abc&b=2")
    assert "_csrf=__NONCE__" in out
    assert "a=1" in out and "b=2" in out


def test_canonical_url_case_insensitive_nonce_match_redacts_value() -> None:
    out = extraction_cache._canonical_url("https://x/y?CSRF=abc&a=1")
    assert "CSRF=__NONCE__" in out
    assert "a=1" in out


ElementTreeCheck = Callable[[], None]


def _same_canonical(h1: str, h2: str) -> ElementTreeCheck:
    def check() -> None:
        assert _canonical_element_tree(h1) == _canonical_element_tree(h2)

    return check


def _different_canonical(h1: str, h2: str) -> ElementTreeCheck:
    def check() -> None:
        assert _canonical_element_tree(h1) != _canonical_element_tree(h2)

    return check


def _contains(html: str, *needles: str) -> ElementTreeCheck:
    def check() -> None:
        out = _canonical_element_tree(html)
        assert out is not None
        for needle in needles:
            assert needle in out

    return check


def _canonical_is(html: str | None, expected: str | None) -> ElementTreeCheck:
    def check() -> None:
        assert _canonical_element_tree(html) == expected

    return check


def _canonical_is_not_none(html: str) -> ElementTreeCheck:
    def check() -> None:
        assert _canonical_element_tree(html) is not None

    return check


@pytest.mark.parametrize(
    "checks",
    (
        pytest.param(
            (
                _canonical_is(None, None),
                _canonical_is("", ""),
                _same_canonical(
                    '<div id="3f8a9b12-1234-4678-9abc-def012345678">x</div>',
                    '<div id="fedcba98-8765-4321-abcd-123456789abc">x</div>',
                ),
                _same_canonical('<div id="row-abc123def">x</div>', '<div id="row-fedcba987">x</div>'),
                _same_canonical(
                    '<button data-testid="btn-1a2b3c4d">go</button>',
                    '<button data-testid="btn-5e6f7a8b">go</button>',
                ),
                _same_canonical(
                    '<div ID="3f8a9b12-1234-4678-9abc-def012345678">x</div>',
                    '<div ID="fedcba98-8765-4321-abcd-123456789abc">x</div>',
                ),
                _same_canonical('<input value="a>b" id="x-3f8a9b12c4">', '<input value="a>b" id="x-fedcba9876">'),
            ),
            id="transient-identity-redaction",
        ),
        pytest.param(
            (
                _canonical_is_not_none("<div>ok</div>"),
                _contains('<div id="x-abc123def">hello world</div>', "hello world"),
                _different_canonical("<div>alpha</div>", "<div>beta</div>"),
                _different_canonical(
                    '<a class="btn primary" href="/docs">go</a>', '<a class="btn danger" href="/docs">go</a>'
                ),
                _different_canonical(
                    '<form><input name="company_name" type="text"><button>Go</button></form>',
                    '<form><input name="contact_phone" type="text"><button>Go</button></form>',
                ),
                _different_canonical(
                    '<button id="submit-button">go</button>', '<button id="cancel-button">go</button>'
                ),
                _different_canonical('<div id="order-123456">go</div>', '<div id="order-987654">go</div>'),
                _different_canonical('<div id="zone-facade">go</div>', '<div id="zone-decade">go</div>'),
                _different_canonical(
                    '<div id="3f8a9b12-1234-1678-9abc-def012345678">x</div>',
                    '<div id="fedcba98-8765-1321-abcd-123456789abc">x</div>',
                ),
                _different_canonical(
                    '<frameset><frame name="criteria" src="criteria.aspx"><input name="txtLastName" value="Lastname1"><input name="txtFirstName" value="Firstname1"></frame></frameset>',
                    '<frameset><frame name="criteria" src="criteria.aspx"><input name="txtLastName" value="Lastname2"><input name="txtFirstName" value="Firstname2"></frame></frameset>',
                ),
                _contains(
                    '<frameset><frame name="criteria" src="criteria.aspx"><input name="txtLastName" value="Lastname1"></frame></frameset>',
                    "Lastname1",
                ),
                _different_canonical(
                    '<input name="memberId" value="200002578">', '<input name="memberId" value="200451314">'
                ),
                _contains('<input name="memberId" value="200002578">', "200002578"),
                _different_canonical(
                    '<select name="ddlDOBYear" selected="1968"><option>1968</option></select>',
                    '<select name="ddlDOBYear" selected="1957"><option>1957</option></select>',
                ),
                _contains("<p>If x < 5 and y > 0 then z = x + y</p>", "If x < 5 and y > 0 then z = x + y"),
                _different_canonical(
                    '<button aria-controls="panel-3f8a9b12c4">Toggle</button>',
                    '<button aria-controls="panel-fedcba9876">Toggle</button>',
                ),
                _different_canonical(
                    "<div onclick=\"fn(id='abc123def')\">click</div>",
                    "<div onclick=\"fn(id='xyz789ghi')\">click</div>",
                ),
                _different_canonical(
                    '<frame src="page.aspx?id=A&action=show">', '<frame src="page.aspx?id=B&action=show">'
                ),
                _contains('<frame src="page.aspx?id=A&action=show">', "id=A"),
                _contains(
                    "<textarea>if x < 5 and y > 0 then z = x + y</textarea>", "if x < 5 and y > 0 then z = x + y"
                ),
                _canonical_is_not_none('<input value="a"b" id="x-3f8a9b12c4">'),
                _different_canonical('<input value="a"b" id="x-3f8a9b12c4">', '<input value="c"d" id="x-fedcba9876">'),
            ),
            id="semantic-identity-preservation",
        ),
    ),
)
def test_canonical_element_tree_identity_matrix(checks: tuple[ElementTreeCheck, ...]) -> None:
    for check in checks:
        check()


def test_canonical_element_tree_scrubs_csrf_input_value() -> None:
    h1 = '<input name="_csrf" value="abc123">'
    h2 = '<input name="_csrf" value="zyx987">'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_meta_content() -> None:
    h1 = '<meta name="csrf-token" content="abc123">'
    h2 = '<meta name="csrf-token" content="zyx987">'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_input_case_insensitive() -> None:
    h1 = '<input name="CSRF_TOKEN" value="abc123">'
    h2 = '<input name="CSRF_TOKEN" value="zyx987">'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_meta_case_insensitive() -> None:
    h1 = '<meta name="CSRF-TOKEN" content="abc123">'
    h2 = '<meta name="CSRF-TOKEN" content="zyx987">'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_with_unquoted_value() -> None:
    h1 = '<input name="_csrf" value=abc123def>'
    h2 = '<input name="_csrf" value=xyz789ghi>'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_with_apostrophe_in_double_quoted_value() -> None:
    h1 = '<input name="_csrf" value="abc\'123">'
    h2 = '<input name="_csrf" value="xyz\'789">'
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)


def test_canonical_element_tree_scrubs_csrf_with_double_quote_in_single_quoted_value() -> None:
    h1 = "<input name='_csrf' value='abc\"123'>"
    h2 = "<input name='_csrf' value='xyz\"789'>"
    assert _canonical_element_tree(h1) == _canonical_element_tree(h2)
