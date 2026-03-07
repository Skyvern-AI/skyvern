"""Tests for truncate_output."""

from __future__ import annotations

from skyvern.forge.sdk.copilot.output_utils import truncate_output


def test_truncate_output_none() -> None:
    assert truncate_output(None) is None


def test_truncate_output_short_string() -> None:
    assert truncate_output("ok") == "ok"


def test_truncate_output_long_string_truncates() -> None:
    text = "x" * 2100
    result = truncate_output(text, max_chars=2000)

    assert result is not None
    assert result.startswith("x" * 2000)
    assert result.endswith("\n... [truncated]")


def test_truncate_output_serializes_dict() -> None:
    result = truncate_output({"a": 1, "b": True})
    assert result == '{"a": 1, "b": true}'


def test_truncate_output_falls_back_to_str_on_json_error() -> None:
    circular: dict[str, object] = {}
    circular["self"] = circular

    result = truncate_output(circular)
    assert result is not None
    assert "self" in result
