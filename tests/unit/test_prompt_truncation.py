"""Tests for prompt truncation helpers (SKY-8920 Phase B + D)."""

from __future__ import annotations


def test_truncate_none_returns_none() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information

    assert truncate_previous_extracted_information(None, max_tokens=1000) is None


def test_truncate_short_string_returns_unchanged() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information

    value = "small tail"
    result = truncate_previous_extracted_information(value, max_tokens=1000)
    assert result == value


def test_truncate_long_string_keeps_tail() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information

    value = "HEAD" + ("x" * 800) + "TAIL"
    result = truncate_previous_extracted_information(value, max_tokens=100)
    assert isinstance(result, str)
    assert result.endswith("TAIL")
    assert "HEAD" not in result


def test_truncate_long_string_respects_exact_token_cap() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information
    from skyvern.utils.token_counter import count_tokens

    value = ("lorem ipsum dolor sit amet " * 20_000) + "UNIQUE_TAIL_MARKER"
    for cap in (50, 500, 5_000):
        result = truncate_previous_extracted_information(value, max_tokens=cap)
        assert isinstance(result, str)
        assert count_tokens(result) <= cap, f"cap={cap} overshot: {count_tokens(result)}"


def test_truncate_long_list_keeps_recent_entries() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information

    value = [{"i": i, "pad": "x" * 1000} for i in range(500)]
    result = truncate_previous_extracted_information(value, max_tokens=500)
    assert isinstance(result, list)
    assert result[-1]["i"] == 499
    assert len(result) < len(value)


def test_truncate_dict_preserves_top_level_keys_and_caps_values() -> None:
    import json

    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information
    from skyvern.utils.token_counter import count_tokens

    value = {"a": "x" * 50_000, "b": "y" * 50_000}
    result = truncate_previous_extracted_information(value, max_tokens=200)
    assert isinstance(result, dict)
    assert set(result.keys()) == {"a", "b"}
    assert count_tokens(json.dumps(result)) <= 400  # small slack for JSON wrapping


def test_truncate_dict_preserves_value_types_when_under_per_key_budget() -> None:
    from skyvern.utils.prompt_truncation import truncate_previous_extracted_information

    value = {
        "small_dict": {"nested": "data", "count": 42},
        "small_list": [1, 2, 3],
        "small_str": "hello",
    }
    result = truncate_previous_extracted_information(value, max_tokens=10_000)
    # Each item is well under the per_key budget; original types should survive,
    # not be coerced to JSON-serialized strings.
    assert result == value
    assert isinstance(result["small_dict"], dict)
    assert isinstance(result["small_list"], list)
    assert isinstance(result["small_str"], str)


def test_truncate_respects_default_budget() -> None:
    from skyvern.utils.prompt_truncation import PREVIOUS_EXTRACTED_INFO_MAX_TOKENS

    assert PREVIOUS_EXTRACTED_INFO_MAX_TOKENS == 20_000


def test_truncate_extraction_schema_none_returns_none() -> None:
    from skyvern.utils.prompt_truncation import truncate_extraction_schema

    assert truncate_extraction_schema(None, max_tokens=1000) is None


def test_truncate_extraction_schema_short_passes_through() -> None:
    from skyvern.utils.prompt_truncation import truncate_extraction_schema

    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    result = truncate_extraction_schema(schema, max_tokens=1000)
    assert result == schema


def test_truncate_extraction_schema_large_returns_summary_placeholder() -> None:
    import json

    from skyvern.utils.prompt_truncation import truncate_extraction_schema
    from skyvern.utils.token_counter import count_tokens

    big_props = {f"field_{i}": {"type": "string", "description": "x" * 200} for i in range(500)}
    schema = {"type": "object", "properties": big_props}
    original_tokens = count_tokens(json.dumps(schema))
    assert original_tokens > 10_000

    result = truncate_extraction_schema(schema, max_tokens=2_000)
    result_tokens = count_tokens(json.dumps(result))

    assert result_tokens <= 2_200
    assert result["type"] == "object"
    assert result.get("_skyvern_schema_truncated") is True


def test_truncate_extraction_schema_default_budget() -> None:
    from skyvern.utils.prompt_truncation import EXTRACTION_SCHEMA_MAX_TOKENS

    assert EXTRACTION_SCHEMA_MAX_TOKENS == 10_000


def test_truncate_extraction_schema_preserves_array_top_level() -> None:
    import json

    from skyvern.utils.prompt_truncation import truncate_extraction_schema
    from skyvern.utils.token_counter import count_tokens

    items = [{"f": f"val_{i}_" + ("lorem ipsum " * 40)} for i in range(1000)]
    schema = {"type": "array", "items": {"type": "object", "properties": {"f": {"type": "string"}}}, "_items": items}
    result = truncate_extraction_schema(schema, max_tokens=2_000)
    assert count_tokens(json.dumps(result)) <= 2_200
    assert result["type"] == "array"


def test_truncate_page_html_short_returns_unchanged() -> None:
    from skyvern.utils.prompt_truncation import truncate_page_html_for_summary

    html = "<html><body>small page</body></html>"
    assert truncate_page_html_for_summary(html, max_chars=10_000) == html


def test_truncate_page_html_empty_returns_unchanged() -> None:
    from skyvern.utils.prompt_truncation import truncate_page_html_for_summary

    assert truncate_page_html_for_summary("", max_chars=10_000) == ""


def test_truncate_page_html_long_is_bounded_and_marked() -> None:
    from skyvern.utils.prompt_truncation import (
        _SUMMARY_HTML_TRUNCATION_MARKER,
        truncate_page_html_for_summary,
    )

    html = "<div>" + ("x" * 1_000_000) + "</div>"
    result = truncate_page_html_for_summary(html, max_chars=120_000)
    assert len(result) == 120_000 + len(_SUMMARY_HTML_TRUNCATION_MARKER)
    assert result.startswith("<div>")
    assert _SUMMARY_HTML_TRUNCATION_MARKER in result


def test_truncate_page_html_keeps_head_and_tail() -> None:
    from skyvern.utils.prompt_truncation import truncate_page_html_for_summary

    html = "HEAD_MARKER" + ("x" * 500_000) + "TAIL_MARKER"
    result = truncate_page_html_for_summary(html, max_chars=50_000)
    assert "HEAD_MARKER" in result  # small head kept for page identity
    assert "TAIL_MARKER" in result  # tail kept — head-only slicing would have dropped this
    assert len(result) < len(html)  # middle dropped


def test_truncate_page_html_tiny_budget_keeps_head_only() -> None:
    from skyvern.utils.prompt_truncation import (
        _SUMMARY_HTML_TRUNCATION_MARKER,
        SUMMARY_HTML_HEAD_CHARS,
        truncate_page_html_for_summary,
    )

    budget = SUMMARY_HTML_HEAD_CHARS // 2  # tail_chars == 0, so the head-only branch runs
    html = "h" * (budget * 4)
    result = truncate_page_html_for_summary(html, max_chars=budget)
    assert result == html[:budget] + _SUMMARY_HTML_TRUNCATION_MARKER
